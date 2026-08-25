"""
migrate_to_postgres.py — SQLiteの既存データをPostgresへ移行する(T40)

本番切替の実体はこのスクリプトが担う。db.py/storage.py側の対応
(dialect変換・cur.lastrowidの互換化等)だけでは「新規に空のPostgresで
動く」ところまでしか保証されないため、実際に稼働中のSQLite
(out/companies.db)の中身をPostgresへコピーする専用スクリプトを用意した。

前提:
  - 移行先のDATABASE_URL(postgres://...)を環境変数に設定しておくこと
  - 移行先は空、または再実行して上書きしてよい状態であること
    (デフォルトで対象テーブルをTRUNCATEしてから入れ直す。差分同期ではない)

使い方:
  DATABASE_URL=postgresql://user:pass@host/db python3 migrate_to_postgres.py
  DATABASE_URL=postgresql://user:pass@host/db python3 migrate_to_postgres.py --verify
      # コピーはせず、SQLite側とPostgres側の件数を突き合わせるだけ

移行手順(本番切替時):
  1. python3 backup.py run で移行直前のSQLiteバックアップを取る(念のため)
  2. サーバーを止める、またはKill Switchで送信を止める(移行中に書き込みが
     続くとその分がPostgres側に反映されずに失われるため)
  3. DATABASE_URL=postgresql://... python3 migrate_to_postgres.py
  4. DATABASE_URL=postgresql://... python3 migrate_to_postgres.py --verify で件数確認
  5. .envにDATABASE_URLを設定し、docker compose up -d --build で再起動
  6. Kill Switchを解除する前に、api.py test 相当の簡易疎通確認を行う
"""
import argparse
import os
import sqlite3
import sys

import config as C

# 外部キー制約を満たす順序(参照される側が先)。この配列に無いテーブルは
# 外部キー制約を持たないため、末尾にまとめて追加する(load_all()参照)。
_FK_SAFE_ORDER = [
    "companies", "tenants", "campaigns",
    "offers", "staff",
    "touches", "dormant",
    "target_lists", "target_list_members",
    "email_tracking_tokens",
]

# id列がSERIAL(AUTOINCREMENT)なテーブル。コピー後にシーケンスを
# 最大id+1へ合わせないと、次のINSERTでid重複エラーになる。storage.pyの
# execute()がRETURNING idを自動で足す対象と同じ集合(定義もそちらが正)。
import storage as _storage
_SERIAL_ID_TABLES = _storage.SERIAL_ID_TABLES


def _sqlite_tables(sqlite_con):
    rows = sqlite_con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    return sorted(r[0] for r in rows)


def _ordered_tables(sqlite_con):
    all_tables = set(_sqlite_tables(sqlite_con))
    ordered = [t for t in _FK_SAFE_ORDER if t in all_tables]
    remaining = sorted(all_tables - set(ordered))
    return ordered + remaining


def _row_count(con, table, is_sqlite):
    if is_sqlite:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return row["count"]


def verify(sqlite_con, pg_con):
    print("── SQLite側とPostgres側の件数突合 ──")
    ok = True
    for table in _ordered_tables(sqlite_con):
        n_sqlite = _row_count(sqlite_con, table, is_sqlite=True)
        n_pg = _row_count(pg_con, table, is_sqlite=False)
        match = n_sqlite == n_pg
        ok = ok and match
        mark = "✓" if match else "✗"
        print(f"  {mark} {table:<24} sqlite={n_sqlite:<8} postgres={n_pg}")
    print(f"\n{'すべて一致' if ok else '不一致のテーブルがあります'}")
    return ok


def load_table(sqlite_con, pg_con, table, batch_size=2000):
    cur = sqlite_con.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    col_list = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

    total = 0
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        pg_con.executemany(insert_sql, [tuple(r) for r in rows])
        total += len(rows)
    pg_con.commit()

    if table in _SERIAL_ID_TABLES:
        pg_con.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 1))")
        pg_con.commit()

    print(f"  {table:<24} {total}件")
    return total


def load_all(sqlite_con, pg_con):
    tables = _ordered_tables(sqlite_con)
    # 1回のTRUNCATEで全テーブルを空にする(CASCADEにより外部キーの向きを
    # 気にせず一括で安全に空にできる。テーブルごとに個別TRUNCATEすると、
    # 参照先を先に空にした時点で参照元も連鎖して空になり、
    # 「まだロードしていないテーブルを二重に空にする」だけの無駄が起きるため)
    print("── 移行先を空にしています ──")
    pg_con.execute(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE")
    pg_con.commit()

    print("── コピー中 ──")
    total = 0
    for table in tables:
        total += load_table(sqlite_con, pg_con, table)
    print(f"\n合計 {total}件をコピーしました")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="コピーはせず件数突合のみ行う")
    args = ap.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith(("postgres://", "postgresql://")):
        print("DATABASE_URL に postgres:// / postgresql:// を設定してから実行してください"
              "(移行先を誤って壊さないよう、未設定では動かない仕様にしている)。")
        sys.exit(1)

    if not C.DB_PATH.exists():
        print(f"移行元のSQLiteファイルが見つかりません: {C.DB_PATH}")
        sys.exit(1)

    sqlite_con = sqlite3.connect(str(C.DB_PATH))
    sqlite_con.row_factory = sqlite3.Row

    import db
    pg_con = db.connect()  # DATABASE_URL設定済みなのでPostgres接続になる
    db.migrate(pg_con)     # 移行先にスキーマが無ければ先に作る

    if args.verify:
        ok = verify(sqlite_con, pg_con)
        sys.exit(0 if ok else 1)

    load_all(sqlite_con, pg_con)
    verify(sqlite_con, pg_con)


if __name__ == "__main__":
    main()
