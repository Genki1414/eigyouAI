"""
compare_models.py — Sonnet vs Haiku 比較検証
enrich.pyのAI呼び出し本体(enrich_one)を使って、同一の会社群をSonnet 5とHaiku 4.5の
両方で処理し、is_target_business/hiring_nowの一致率を出す。
「Haikuで全体を回し、Sランク候補のみSonnetで再確認する」運用に切り替えてよいかの
判断材料を提示するのが目的。採用可否の判断は人間が行う。

companiesテーブルは更新しない(enrich.pyの本番実行/再開ロジックと衝突しないように、
結果は out/model_compare.json にのみ書き出す)。

使い方:
  python3 compare_models.py --limit 30
"""
import argparse, json, os
from pathlib import Path

import db
import resilience as R
from enrich import enrich_one, MODEL

OUT = Path(__file__).parent / "out" / "model_compare.json"
HAIKU = "claude-haiku-4-5-20251001"
COMPARE_FIELDS = ("is_target_business", "hiring_now")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--pref", default=None)
    ap.add_argument("--haiku-model", default=HAIKU)
    args = ap.parse_args()

    import anthropic  # pip install anthropic （本番のみ必要）
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    con = db.connect()
    db.migrate(con)

    # companiesは更新しないので既エンリッチ/未エンリッチを問わず対象にできるが、
    # 再現性のためidで固定順に選ぶ(RANDOM()だと毎回違う会社になり比較にならない)
    q = "SELECT * FROM companies WHERE dedup_of IS NULL AND capital BETWEEN 3000 AND 100000"
    p = []
    if args.pref:
        q += " AND pref=?"; p.append(args.pref)
    q += " ORDER BY id LIMIT ?"; p.append(args.limit)
    rows = con.execute(q, p).fetchall()

    rl = R.limiter_for("anthropic")
    results = []
    for i, row in enumerate(rows):
        rec = {"id": row["id"], "name": row["name"]}
        for label, model in (("sonnet", MODEL), ("haiku", args.haiku_model)):
            try:
                rl.acquire()
                d = R.retry(lambda m=model: enrich_one(client, row, model=m),
                            job=f"compare:{label}:{row['name']}")
                d.pop("_usage", None)
                rec[label] = d
            except Exception as e:
                rec[label] = {"error": str(e)[:200]}
        results.append(rec)
        print(f"  {i + 1}/{len(rows)}: {row['name']}")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\n比較完了({len(results)}社) → {OUT}")

    for field in COMPARE_FIELDS:
        ok = n = 0
        for r in results:
            s, h = r.get("sonnet", {}), r.get("haiku", {})
            if field in s and field in h:
                n += 1
                ok += int(s[field] == h[field])
        pct = ok / n * 100 if n else 0.0
        print(f"  {field} 一致率: {ok}/{n} ({pct:.1f}%){'  ← 8割未満' if n and pct < 80 else ''}")


if __name__ == "__main__":
    main()
