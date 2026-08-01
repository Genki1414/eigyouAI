"""
scoring.py — リードスコアリング V1 (ルールベース・100点満点)
設計思想: 「導入しそうな会社」ではなく「AI積算ツールの無料オファーに反応しそうな会社」を上に。

4軸 × 25点:
  1. 規模適合   … 大きすぎず小さすぎない(5-50人が甘い層)。資本金・従業員。
  2. デジタル度 … HPがあり更新されている=メール/Web導線が通る。
  3. 成長シグナル … 求人出稿中=案件が増えて人が足りない=積算も回らない。
  4. 商流適合   … とび・土工が主業種 / 元請比率が高い=積算の当事者。

閾値: S>=75 / A>=60 / B>=45 / C<45
実測の反応率が出たらこの重みをロジスティック回帰に置換する(V2)。

使い方: python3 scoring.py  → companies.db を更新し out/scored.json を書き出し
"""
import json, sqlite3
from pathlib import Path

DB = Path(__file__).parent / "out" / "companies.db"
OUT = Path(__file__).parent / "out" / "scored.json"

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def score_row(r):
    d = {}
    # 1. 規模適合 (25) — 5-50人がスイートスポット
    emp = r["est_employees"] or max(3, (r["capital"] or 0) // 3000)
    if 5 <= emp <= 50:
        d["size"] = 25
    elif emp < 5:
        d["size"] = 10
    else:
        d["size"] = clamp(25 - (emp - 50) * 0.3, 5, 20)
    # 2. デジタル度 (25)
    d["digital"] = {0: 4, 1: 10, 2: 18, 3: 25}.get(r["website_quality"] or 0, 4)
    if not r["has_website"]:
        d["digital"] = 2  # FAX/郵送チャネル行き
    # 3. 成長シグナル (25)
    # founded_year(許可年月日からの推定)は5年ごとの許可更新で値が動くため
    # 実データでは社歴の代理変数にならず、判定に使わない(learn.pyのlicense_seq_pct参照)。
    # google_reviewsは実データでAIが実際の件数を取得できておらず(ほぼ全件0)信頼できないため
    # 使わない。求人出稿シグナル(hiring_now)に全25点を寄せた。
    d["growth"] = 25 if r["hiring_now"] else 0
    # 4. 商流適合 (25)
    s = 0
    trades = (r["trades"] or "").split(",")
    if "tobi" in trades:
        s += 12
    if "kaitai" in trades or "tosou" in trades:
        s += 4
    s += clamp((r["prime_ratio"] or 0.3) * 12, 0, 12)
    d["fit"] = clamp(s, 0, 25)

    total = round(sum(d.values()), 1)
    rank = "S" if total >= 75 else "A" if total >= 60 else "B" if total >= 45 else "C"
    return total, rank, d

def recommend_channel(r, rank):
    if not r["has_website"]:
        return "FAX+郵送DM"
    if r["hiring_now"]:
        return "求人文脈メール"
    if rank in ("S", "A"):
        return "パーソナライズDM+架電"
    return "一斉メール"

def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM companies").fetchall()
    out = []
    excluded = 0
    for r in rows:
        # is_target_business=0(施工実態なしとAIが判定)はスコアリング対象外。
        # dedup_ofとは別軸のフラグなので、代表社への集約とは独立に除外する。
        if r["is_target_business"] == 0:
            excluded += 1
            con.execute("UPDATE companies SET score=NULL, rank=NULL, score_detail=NULL WHERE id=?",
                        (r["id"],))
            continue
        total, rank, detail = score_row(r)
        con.execute("UPDATE companies SET score=?, rank=?, score_detail=? WHERE id=?",
                    (total, rank, json.dumps(detail), r["id"]))
        rec = dict(r)
        rec.update(score=total, rank=rank, detail=detail,
                   channel=recommend_channel(r, rank))
        out.append(rec)
    con.commit()
    out.sort(key=lambda x: -x["score"])
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    dist = {}
    for o in out:
        dist[o["rank"]] = dist.get(o["rank"], 0) + 1
    print(f"スコアリング完了: {len(out)}社 / 分布 {dist} "
          f"(施工実態なしで除外 {excluded}社) → {OUT}")

if __name__ == "__main__":
    main()
