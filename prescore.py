"""
prescore.py — AIを使わない事前絞り込み
enrich.py(AIエンリッチメント)は実測1社あたり30〜40円かかることが判明した
(旧見積もりの「2〜4円」から10倍以上乖離)。14,688社全件を回すと数千ドル規模になるため、
AIを一切使わず、既に持っているデータ(資本金・許可業種・許可番号連番)だけで
本当にエンリッチメントする価値がありそうな会社に事前に絞り込む。

絞り込み基準:
  1. 資本金300万〜1億円(5〜50人規模の代理指標。scoring.pyのスイートスポットと一致)
     ここで対象外になった会社(巨大企業・実質休眠に近い極小資本金)は除外する
  2. とび・土工(tobi)保有を優先(scoring.pyの商流適合でtobiが最も重い配点なのと同じ考え方)
  3. 上記の中でlicense_seq_pct(許可番号連番の都道府県内パーセンタイル。値が小さいほど
     古参=社歴が長い代理指標。db.compute_license_seq_pct参照)が小さい会社を優先

判断(何%まで絞るか・本当にこの3基準でよいか)は人間が行う。ここではdb.companies.
prescore_selectedへの書き込みと一覧の提示のみを行う。

使い方:
  python3 prescore.py                    # 全国、目標3,000社
  python3 prescore.py --pref 東京都 --target 500
"""
import argparse, json
from pathlib import Path

import db

OUT = Path(__file__).parent / "out" / "prescore.json"

CAPITAL_LO, CAPITAL_HI = 3000, 100000  # 千円単位。300万〜1億円


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=3000)
    ap.add_argument("--pref", default=None)
    args = ap.parse_args()

    con = db.connect()
    db.migrate(con)

    q = """SELECT id, name, pref, trades, capital, license_no FROM companies
           WHERE dedup_of IS NULL AND capital BETWEEN ? AND ?"""
    p = [CAPITAL_LO, CAPITAL_HI]
    if args.pref:
        q += " AND pref=?"; p.append(args.pref)
    rows = con.execute(q, p).fetchall()

    seq_pct = db.compute_license_seq_pct(con)

    def is_tobi(r):
        return "tobi" in (r["trades"] or "").split(",")

    # とび・土工保有を優先し、その中では許可番号連番が若い(古参)会社を優先する。
    # tobiを持たない会社(kaitai/tosouのみ)は、tobi群だけで目標数に届かない場合の
    # 補充として同じ並び順で追加する。
    tobi_group = sorted((r for r in rows if is_tobi(r)), key=lambda r: seq_pct.get(r["id"], 0.5))
    other_group = sorted((r for r in rows if not is_tobi(r)), key=lambda r: seq_pct.get(r["id"], 0.5))
    selected = (tobi_group + other_group)[:args.target]

    con.execute("UPDATE companies SET prescore_selected=0")
    con.executemany("UPDATE companies SET prescore_selected=1 WHERE id=?",
                     [(r["id"],) for r in selected])
    con.commit()

    out = [{"id": r["id"], "name": r["name"], "pref": r["pref"], "trades": r["trades"],
            "capital": r["capital"], "license_seq_pct": round(seq_pct.get(r["id"], 0.5), 3),
            "is_tobi": is_tobi(r)} for r in selected]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))

    n_tobi_selected = sum(1 for r in selected if is_tobi(r))
    print(f"候補(資本金300万〜1億円): {len(rows)}社 "
          f"(うちtobi保有 {len(tobi_group)}社 / それ以外 {len(other_group)}社)")
    print(f"選出: {len(selected)}社 (うちtobi保有 {n_tobi_selected}社 / "
          f"tobiなしの補充 {len(selected) - n_tobi_selected}社)")
    print(f"→ companies.prescore_selected を更新 / 一覧を {OUT} に書き出し")
    print("enrich.py --prescored-only でこの選出結果のみを対象にエンリッチできます。")


if __name__ == "__main__":
    main()
