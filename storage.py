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

# 一意制約違反(=想定内の重複。冪等性チェック等がexcept節で捕まえる)を、
# バックエンドを問わず同じ書き方で検出できるようにする。
# psycopgはPostgres移行時のみ必須(requirements.txt参照)なので、未インストール
# 環境(SQLiteのみで動かしている場合)でもstorage.py自体のimportは壊さない。
try:
    import psycopg as _psycopg
    IntegrityError = (sqlite3.IntegrityError, _psycopg.errors.IntegrityError)
except ImportError:
    IntegrityError = (sqlite3.IntegrityError,)


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
    # プレースホルダ ? → %s（文字列リテラル内の ? は変換しない）。
    # psycopgはpyformat方式("%s")でパラメータを渡すため、クエリ文字列中の
    # リテラルな"%"(LIKE 'provider_id=mock_%'のような)もすべて"%%"に
    # エスケープしないと、'%p'のような並びをプレースホルダと誤認して
    # ProgrammingErrorになる(SQLの文字列リテラルの中かどうかは無関係に、
    # psycopg側は生のテキストとして%記法をスキャンするため)。
    out, in_str, quote = [], False, ""
    for ch in s:
        if ch == "%":
            out.append("%%")
        elif in_str:
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
    s = "".join(out)
    # プレースホルダ単体のIS NULL判定(例: `pref=? OR ? IS NULL`)は、その値が
    # 他の場所で列と比較されず型のヒントが無いため、psycopgが
    # 「IndeterminateDatatype」で型を推論できずエラーになる。IS NULLは
    # 値そのものの型を問わない(Noneかどうかしか見ない)ので、textへ明示
    # キャストしても意味は変わらない——ここで一律キャストして解決する。
    s = re.sub(r"%s(\s+IS(?:\s+NOT)?\s+NULL)", r"%s::text\1", s, flags=re.I)
    return s


_INSERT_INTO_RE = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.I)


class _PgRow:
    """psycopgの1行分の結果を、sqlite3.Row(row_factory=sqlite3.Row)と同じ感覚で
    使えるようにする(位置アクセスrow[0]・列名アクセスrow["col"]・dict(row)への
    変換、の3通りすべてに対応する)。psycopg標準のrow_factoryは
    tuple_row(位置のみ)かdict_row(列名のみ)のどちらか一方しか満たさず、
    このコードベースはsqlite3時代からの書き方でその両方を使っているため、
    どちらのアクセス方法で書かれた既存コードも変更せずに動かすには
    このハイブリッド型が要る。"""
    __slots__ = ("_values", "_index")

    def __init__(self, values, index):
        self._values = values
        self._index = index

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def keys(self):
        return self._index.keys()

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        if isinstance(other, _PgRow):
            return self._values == other._values and self._index == other._index
        return NotImplemented

    def __repr__(self):
        return repr(dict(zip(self._index, self._values)))


def _hybrid_row_factory(cursor):
    """psycopgのrow_factoryプロトコル。cursor.descriptionが確定した時点
    (実行直後)で1回呼ばれ、以後1行受け取るたびにここで返した関数が呼ばれる。"""
    index = {d.name: i for i, d in enumerate(cursor.description)} if cursor.description else {}

    def make_row(values):
        return _PgRow(values, index)
    return make_row


# id列がSERIAL(SQLite側はINTEGER PRIMARY KEY AUTOINCREMENT)のテーブル一覧。
# execute()がRETURNING idを自動で足せる/足してよい対象を安全に絞り込むために使う
# (このコードベースの主キー名は"id"で統一されていないテーブルが複数ある。
# 例: suppression/dormantはcompany_id、idempotency/alert_stateはkey、
# tenant_kill_switch/autofill_queueはtenant_id、target_list_members/checkpoints/
# tenant_exclusionsは複合キー。それらに無条件で"RETURNING id"を足すとPostgres側で
# 「column "id" does not exist」エラーになるため、決め打ちのホワイトリストにする)。
# migrate_to_postgres.pyのシーケンス再設定対象とも同じ集合なので、そちらからも
# この定数を参照する。
SERIAL_ID_TABLES = {
    "companies", "campaigns", "touches", "message_templates", "sender_templates",
    "announcements", "scheduled_sends", "run_log", "form_send_log",
    "tenants", "offers", "staff", "target_lists", "search_log",
}


class _PgCursorWrapper:
    """psycopg.Cursorをsqlite3.Cursorと同じ形で使えるようにする薄いラッパ。
    最大の差分はcur.lastrowid: sqlite3は標準で持つがpsycopgには無い
    (Postgresの流儀はRETURNINGで返り値を取ること)。db.py/api.py等が
    「cur = con.execute('INSERT ...'); id = cur.lastrowid」という書き方を
    多用しているため、ここでSQLに自動でRETURNING idを足し、結果を
    lastrowidとして先読みしておくことで、呼び出し側を一切変えずに動かす。"""

    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def fetchmany(self, size=None):
        return self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def __iter__(self):
        return iter(self._cur)


class PgConnection:
    """sqlite3.Connection と同じ形で使えるようにする薄いラッパ。
    con.execute(sql, params).fetchone() / .fetchall() / con.commit() が同じ書き方で通る。"""

    def __init__(self, dsn):
        import psycopg
        self._conn = psycopg.connect(dsn, row_factory=_hybrid_row_factory, autocommit=False)

    def execute(self, sql, params=()):
        pg_sql = to_pg_sql(sql)
        cur = self._conn.cursor()
        # SERIAL_ID_TABLES(=id列を持つテーブル)への単純なINSERT(RETURNING未指定・
        # ON CONFLICT無し)にはRETURNING idを自動で足し、cur.lastrowidとして
        # 先読みする(呼び出し側の29箇所がこれに依存している)。それ以外の
        # テーブル(meta/idempotency/suppression等、主キー列名が"id"でない、
        # または複合主キー)は対象外——無条件に足すとPostgres側で
        # 「column "id" does not exist」エラーになる。
        m = _INSERT_INTO_RE.match(pg_sql)
        target_table = m.group(1) if m else None
        wants_returning = (target_table in SERIAL_ID_TABLES
                           and "RETURNING" not in pg_sql.upper()
                           and "ON CONFLICT" not in pg_sql.upper())
        if wants_returning:
            pg_sql = pg_sql.rstrip().rstrip(";") + " RETURNING id"
        cur.execute(pg_sql, tuple(params) if params else None)
        lastrowid = None
        if wants_returning:
            row = cur.fetchone()
            lastrowid = row["id"] if row else None
        return _PgCursorWrapper(cur, lastrowid=lastrowid)

    def executemany(self, sql, params_seq):
        cur = self._conn.cursor()
        cur.executemany(to_pg_sql(sql), [tuple(p) for p in params_seq])
        return _PgCursorWrapper(cur)

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


def table_columns(con, table):
    """テーブルの列名集合を返す(バックエンド差分をここで吸収する)。
    db.migrate()の「無ければ列を足す」ロジック(PRAGMA table_info相当)が
    両バックエンドで動くようにするために存在する。"""
    if backend() == "postgres":
        rows = con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=?",
            (table,)).fetchall()
        return {r["column_name"] for r in rows}
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def table_exists(con, table):
    """テーブルの存在確認(sqlite_master/information_schemaの差分をここで吸収する)。
    run.pyのSTEPS完了判定など「テーブルが作られたか」を見たい箇所向け。"""
    return bool(table_columns(con, table))


def connect(timeout=30.0):
    """バックエンドに応じた接続を返す。呼び出し側はどちらか知らなくてよい。"""
    if backend() == "postgres":
        return PgConnection(os.environ["DATABASE_URL"])

    # out/はgitignore対象のため、真っさらなcheckout(CI・初回デプロイ)には
    # 存在しない。無いとsqlite3.connect()が"unable to open database file"で
    # 即座に落ちる(2026-08-26、T46のデプロイでCIのtestジョブが毎回この
    # エラーで失敗し続け、T38以降の全pushが本番へデプロイされていなかった
    # ことが判明して発覚)。
    C.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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
            # psycopgはpyformat("%s")でパラメータを渡すため、SQL文中のリテラルな
            # "%"(LIKEパターン等)は"%%"にエスケープしないとプレースホルダと
            # 誤認される。'?'は文字列リテラル内なのでプレースホルダ変換対象外のまま。
            ("SELECT * FROM t WHERE a=? AND b LIKE '%?%'",
             "SELECT * FROM t WHERE a=%s AND b LIKE '%%?%%'"),
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
