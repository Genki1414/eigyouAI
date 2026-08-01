"""
storage.py — SQLite / Postgres の差分吸収層
本番でPostgresへ移す際に触る箇所を、このファイル1枚に閉じ込める。
他のスクリプトは storage.connect() を呼ぶだけで、どちらで動いているかを知らなくていい。

なぜPostgresが要るか:
  SQLiteは書き込みが同時1本。エンリッチメントを並列8ワーカーで回す、
  APIサーバとバッチが同時に書く、といった段階で頭打ちになる。
  読み中心のうちはSQLiteで足りるので、切り替えは環境変数だけで済むようにしておく。

  DATABASE_URL 未設定           → SQLite（開発・単一プロセス）
  DATABASE_URL=postgres://...  → Postgres（本番・並列）

方言の差分（ここで吸収するもの）:
  | 用途           | SQLite                  | Postgres                      |
  |----------------|-------------------------|-------------------------------|
  | 連番主キー     | INTEGER PK AUTOINCREMENT| SERIAL PRIMARY KEY            |
  | 重複無視挿入   | INSERT OR IGNORE        | INSERT ... ON CONFLICT DO NOTHING |
  | 上書き挿入     | INSERT OR REPLACE       | INSERT ... ON CONFLICT DO UPDATE  |
  | プレースホルダ | ?                       | %s                            |
  | 現在時刻       | datetime('now')         | now()                         |
  | 真偽値         | INTEGER 0/1             | BOOLEAN（0/1でも通る）        |
  | 同時実行設定   | PRAGMA journal_mode=WAL | 不要（MVCC）                  |

  python3 storage.py check     # 現在のバックエンドと接続確認
  python3 storage.py ddl       # 選択中のバックエンド向けDDLを出力
"""
import os
import re
import sqlite3
import sys

import config as C


def backend():
    url = os.environ.get("DATABASE_URL", "")
    return "postgres" if url.startswith(("postgres://", "postgresql://")) else "sqlite"


# ── 方言変換 ────────────────────────────────
def to_pg_ddl(ddl: str) -> str:
    """SQLite向けDDLをPostgres向けに変換する"""
    s = ddl
    s = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"\bdatetime\('now'\)", "now()", s, flags=re.I)
    s = re.sub(r"^\s*PRAGMA[^;]*;\s*$", "", s, flags=re.I | re.M)
    return s


def to_pg_sql(sql: str) -> str:
    """実行時SQLをPostgres向けに変換する"""
    s = sql
    # INSERT OR IGNORE → ON CONFLICT DO NOTHING
    if re.search(r"INSERT\s+OR\s+IGNORE", s, re.I):
        s = re.sub(r"INSERT\s+OR\s+IGNORE", "INSERT", s, flags=re.I)
        if "ON CONFLICT" not in s.upper():
            s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # INSERT OR REPLACE は主キーが分からないと変換できないので、呼び出し側で
    # 明示的な ON CONFLICT ... DO UPDATE を書くこと（db.py は既にその形）
    if re.search(r"INSERT\s+OR\s+REPLACE", s, re.I):
        raise ValueError("INSERT OR REPLACE は Postgres 非対応。"
                         "ON CONFLICT (key) DO UPDATE を明示してください: " + s[:80])
    s = re.sub(r"\bdatetime\('now'\)", "now()", s, flags=re.I)
    # プレースホルダ ? → %s（文字列リテラル内の ? は変換しない）
    out, in_str, quote = [], False, ""
    for ch in s:
        if in_str:
            out.append(ch)
            if ch == quote:
                in_str = False
        elif ch in "'\"":
            in_str, quote = True, ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


class PgConnection:
    """sqlite3.Connection と同じ形で使えるようにする薄いラッパ。
    con.execute(sql, params).fetchone() / .fetchall() / con.commit() が同じ書き方で通る。"""

    def __init__(self, dsn):
        import psycopg
        from psycopg.rows import dict_row
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(to_pg_sql(sql), tuple(params) if params else None)
        return cur

    def executescript(self, script):
        cur = self._conn.cursor()
        for stmt in [x.strip() for x in to_pg_ddl(script).split(";") if x.strip()]:
            cur.execute(stmt)
        self._conn.commit()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def cursor(self):
        return self._conn.cursor()


def connect(timeout=30.0):
    """バックエンドに応じた接続を返す。呼び出し側はどちらか知らなくてよい。"""
    if backend() == "postgres":
        return PgConnection(os.environ["DATABASE_URL"])

    con = sqlite3.connect(C.DB_PATH, timeout=timeout, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA busy_timeout = 30000;
        PRAGMA synchronous  = NORMAL;
        PRAGMA foreign_keys = ON;
        PRAGMA cache_size   = -64000;
    """)
    return con


def notes():
    return {
        "sqlite": [
            "書き込みは同時1本。並列ワーカーは実質1つ",
            "WALで読みと書きは競合しない",
            "ファイル1つで完結。バックアップはコピーするだけ",
            "適する規模: 数万社・単一プロセス・読み中心",
        ],
        "postgres": [
            "MVCCで並列書き込み可。エンリッチメントを8並列で回せる",
            "APIサーバとバッチが同時に書いても詰まらない",
            "接続プール(pgbouncer)と定期バックアップの運用が要る",
            "適する規模: 数十万社・並列処理・複数プロセス",
        ],
    }[backend()]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        b = backend()
        print(f"バックエンド: {b}")
        for n in notes():
            print(f"  - {n}")
        try:
            con = connect()
            print("\n接続: OK")
            if b == "sqlite":
                print(f"  journal_mode = {con.execute('PRAGMA journal_mode').fetchone()[0]}")
        except Exception as e:  # noqa: BLE001
            print(f"\n接続: 失敗 — {e}")
    elif cmd == "ddl":
        import db
        ddl = db.SCHEMA + db.INDEXES
        print(to_pg_ddl(ddl) if backend() == "postgres" else ddl)
    elif cmd == "test":
        # 方言変換の検証（Postgresが無くても動く）
        cases = [
            ("INSERT OR IGNORE INTO t (a,b) VALUES (?,?)",
             "INSERT INTO t (a,b) VALUES (%s,%s) ON CONFLICT DO NOTHING"),
            ("SELECT * FROM t WHERE a=? AND b LIKE '%?%'",
             "SELECT * FROM t WHERE a=%s AND b LIKE '%?%'"),
            ("UPDATE t SET x=datetime('now') WHERE id=?",
             "UPDATE t SET x=now() WHERE id=%s"),
        ]
        ok = 0
        for src, want in cases:
            got = to_pg_sql(src)
            hit = got == want
            ok += hit
            print(f"  {'✓' if hit else '✗'} {src[:44]}")
            if not hit:
                print(f"      期待: {want}\n      実際: {got}")
        ddl = to_pg_ddl("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT);")
        hit = "SERIAL PRIMARY KEY" in ddl
        ok += hit
        print(f"  {'✓' if hit else '✗'} AUTOINCREMENT → SERIAL")
        try:
            to_pg_sql("INSERT OR REPLACE INTO t VALUES (?)")
            print("  ✗ INSERT OR REPLACE を検出できていない")
        except ValueError:
            ok += 1
            print("  ✓ INSERT OR REPLACE を検出して警告する")
        print(f"\n  成功 {ok}/{len(cases)+2}")
