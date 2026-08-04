"""
prescore.py — AIを使わない事前絞り込み
enrich.py(AIエンリッチメント)は実測1社あたり10.8〜14.6円かかる。14,688社全件を回すと
数万円規模になるため、AIを一切使わず、既に持っているデータ(資本金・許可業種・
許可番号連番)だけで本当にエンリッチメントする価値がありそうな会社に事前に絞り込む。

対象: 5〜20人が本命(honmei)、1〜4人も無料ツールの入口として対象(control)。
※ 一般/特定建設業の区分による絞込は現時点では未対応(下記「未対応の条件」参照)。

絞り込み基準:
  1. 資本金3,000万円以下(下限なし。0円=一人親方層も含む。5〜20人規模の代理指標)
  2. とび・土工(tobi)保有を優先(scoring.pyの商流適合でtobiが最も重い配点なのと同じ考え方)
  3. 上記の中でlicense_seq_pct(許可番号連番の都道府県内パーセンタイル。値が小さいほど
     古参=社歴が長い代理指標。db.compute_license_seq_pct参照)が小さい会社を優先

層構成(companies.stratumに記録):
  honmei  … 上記基準の優先順位で上位2,000社(本命層)
  control … honmei以外の候補プールから無作為に1,000社(対照層。優先順位づけの
            バイアスを検証するための比較対象)

未対応の条件(2026-08時点):
  一般建設業(1)のみを対象にし特定建設業(2)を除外する、という条件が依頼されたが、
  現在のcompanies.license_typeは許可行政庁(知事/大臣)の区分であり、一般/特定とは
  別物(実データは全件「知事」で一般/特定の情報を持たない)。一般/特定は業種ごとの
  横持ちフラグとして元Excel側にはあるが、parsers/tokyo.pyのis_licensed_flag()で
  有無の2値に丸めてしまっておりDBには保存されていない。元Excelファイルが無いと
  復元できないため、この条件は未実装(全件を対象に含めている)。

判断(何%まで絞るか・本当にこの基準でよいか)は人間が行う。ここではdb.companies.
prescore_selected/stratumへの書き込みと一覧の提示のみを行う。

使い方:
  python3 prescore.py                              # 全国、本命2,000+対照1,000
  python3 prescore.py --pref 東京都 --honmei 500 --control 250
"""
import argparse, json
from pathlib import Path

import db

OUT = Path(__file__).parent / "out" / "prescore.json"

CAPITAL_HI = 30000  # 千円単位。3,000万円以下(下限なし)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--honmei", type=int, default=2000, help="本命層(優先順位上位)の目標社数")
    ap.add_argument("--control", type=int, default=1000, help="対照層(無作為抽出)の目標社数")
    ap.add_argument("--pref", default=None)
    ap.add_argument("--seed", type=int, default=None, help="対照層抽出の乱数シード(再現性が要る場合)")
    args = ap.parse_args()

    import random
    rng = random.Random(args.seed)

    con = db.connect()
    db.migrate(con)

    # capital IS NULLも候補に含める(mikomeru由来の自由記述資本金がパース不能だった分。
    # 不明を「対象外」ではなく「除外しない」扱いにする。0円=一人親方層と同じ考え方)
    q = ("SELECT id, name, pref, trades, capital, license_no FROM companies "
         "WHERE dedup_of IS NULL AND (capital <= ? OR capital IS NULL)")
    p = [CAPITAL_HI]
    if args.pref:
        q += " AND pref=?"; p.append(args.pref)
    rows = con.execute(q, p).fetchall()

    seq_pct = db.compute_license_seq_pct(con)

    def is_tobi(r):
        return "tobi" in (r["trades"] or "").split(",")

    # とび・土工保有を優先し、その中では許可番号連番が若い(古参)会社を優先する。
    # tobiを持たない会社(kaitai/tosouのみ)はその後に同じ並び順で続く。
    tobi_group = sorted((r for r in rows if is_tobi(r)), key=lambda r: seq_pct.get(r["id"], 0.5))
    other_group = sorted((r for r in rows if not is_tobi(r)), key=lambda r: seq_pct.get(r["id"], 0.5))
    ranked = tobi_group + other_group

    honmei = ranked[:args.honmei]
    honmei_ids = {r["id"] for r in honmei}
    remaining_pool = [r for r in ranked if r["id"] not in honmei_ids]
    control = rng.sample(remaining_pool, min(args.control, len(remaining_pool)))

    con.execute("UPDATE companies SET prescore_selected=0, stratum=NULL")
    con.executemany("UPDATE companies SET prescore_selected=1, stratum='honmei' WHERE id=?",
                     [(r["id"],) for r in honmei])
    con.executemany("UPDATE companies SET prescore_selected=1, stratum='control' WHERE id=?",
                     [(r["id"],) for r in control])
    con.commit()

    def to_out(r, stratum):
        return {"id": r["id"], "name": r["name"], "pref": r["pref"], "trades": r["trades"],
                "capital": r["capital"], "license_seq_pct": round(seq_pct.get(r["id"], 0.5), 3),
                "is_tobi": is_tobi(r), "stratum": stratum}

    out = [to_out(r, "honmei") for r in honmei] + [to_out(r, "control") for r in control]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))

    n_capital0 = sum(1 for r in rows if r["capital"] == 0)
    n_capital_null = sum(1 for r in rows if r["capital"] is None)
    n_honmei_tobi = sum(1 for r in honmei if is_tobi(r))
    n_control_tobi = sum(1 for r in control if is_tobi(r))
    print(f"候補(資本金3,000万円以下・下限なし・不明も含む): {len(rows)}社 "
          f"(うちtobi保有 {len(tobi_group)}社 / それ以外 {len(other_group)}社 / "
          f"資本金0円 {n_capital0}社 / 資本金不明 {n_capital_null}社)")
    print(f"本命層(honmei): {len(honmei)}社 (うちtobi保有 {n_honmei_tobi}社)")
    print(f"対照層(control): {len(control)}社 (うちtobi保有 {n_control_tobi}社、無作為抽出)")
    print(f"合計選出: {len(honmei) + len(control)}社")
    print(f"→ companies.prescore_selected / stratum を更新 / 一覧を {OUT} に書き出し")
    print("※ 一般/特定建設業による絞込は未対応(モジュールdocstring参照)")


if __name__ == "__main__":
    main()
