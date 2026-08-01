"""
ingest.py — 都道府県別「建設業許可業者名簿」Excelの取込
国交省の企業情報検索システム(etsuran2.mlit.go.jp)には一括CSVダウンロードが無いため、
実データは都道府県ごとに公開されている名簿Excel（例: 東京都都市整備局が
建設業情報管理センター登録情報から月1回公開しているファイル）を使う。

県ごとのヘッダ位置・業種表記(コード/業種名/1・2フラグ)の差は parsers/<pref>.py に
閉じ込め、ここでは共通スキーマ(companies列)に変換済みの行を受け取って
DBへ書くだけにする。新しい県を追加するときは parsers/ にモジュールを足すだけでよい。

大臣許可業者（本店・支店が複数都道府県にまたがる業者）は各parser側で
スキープ外として除外している（知事許可が全体の9割以上のため、当面は対象外）。

使い方:
  python3 ingest.py 東京都 data/tokyo_kensetsu_meibo.xlsx
→ SQLite (out/companies.db) に正規化して格納

対応都道府県: parsers.REGISTRY を参照。まず東京都のみで通し、動作確認できてから
他県のparserを追加していく。
"""
import sys
from pathlib import Path

import parsers

DB = Path(__file__).parent / "out" / "companies.db"


def main(pref_name: str, path: str):
    DB.parent.mkdir(exist_ok=True)
    import db
    con = db.connect()
    db.migrate(con)

    parse = parsers.get_parser(pref_name)
    n = 0
    for row in parse(path):
        con.execute(
            """INSERT OR IGNORE INTO companies
               (license_no,name,pref,city,address,phone,fax,license_type,trades,capital,founded_year)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (row["license_no"], row["name"], row["pref"], row["city"], row["address"],
             row["phone"], row["fax"], row["license_type"], row["trades"],
             row["capital"], row["founded_year"]))
        n += 1
    con.commit()
    print(f"取込完了: {n}社 → {DB}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("使い方: python3 ingest.py <都道府県名> <Excelファイルパス>")
        print(f"  対応都道府県: {', '.join(parsers.REGISTRY)}")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
