"""
db.py — スキーマ / マイグレーション / 接触ガード
これまで各スクリプトがバラバラにテーブルを作っていたのを一本化する。
特に重要なのが can_contact(): 配信停止・接触上限・間隔を一箇所で判定する関門。
どのスクリプトからも必ずここを通す設計にして、「うっかり送ってしまった」を構造的に防ぐ。
"""
import re
import sqlite3
import time
from datetime import datetime, timedelta

import config as C

SCHEMA_VERSION = 3

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_rank ON companies(rank);
CREATE INDEX IF NOT EXISTS idx_pref ON companies(pref);
CREATE INDEX IF NOT EXISTS idx_namenorm ON companies(name_norm, pref);
CREATE INDEX IF NOT EXISTS idx_touch_camp ON touches(campaign_id);
CREATE INDEX IF NOT EXISTS idx_touch_step ON touches(campaign_id, step);
CREATE INDEX IF NOT EXISTS idx_touch_co   ON touches(company_id);
CREATE INDEX IF NOT EXISTS idx_formlog_co ON form_send_log(company_id);
CREATE INDEX IF NOT EXISTS idx_formlog_status ON form_send_log(status);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  license_no TEXT UNIQUE, name TEXT NOT NULL, name_norm TEXT,
  pref TEXT, city TEXT, address TEXT, phone TEXT, fax TEXT, email TEXT,
  license_type TEXT, trades TEXT, capital INTEGER,
  founded_year INTEGER,  -- 許可年月日からの推定(5年ごとの許可更新で動くため実際の設立年ではない。
                          -- scoring.py/learn.pyの社歴判定には使わない。将来より正確な設立年データが
                          -- 得られたら差し替える前提でこの列は残している)
  has_website INTEGER, website_url TEXT, website_quality INTEGER,
  hiring_now INTEGER, hiring_source TEXT, est_employees INTEGER,
  google_reviews INTEGER,  -- 未使用(スコアリング対象外。理由はscoring.py参照)。過去分の参考値として列は残す
  is_target_business INTEGER,  -- 施工実態の有無(0/1)。0はスコアリング対象外
  prime_ratio REAL, enrich_note TEXT, enriched_at TEXT,
  score REAL, rank TEXT, score_detail TEXT, score_v2 REAL,
  dedup_of INTEGER
);

CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, started_at TEXT, target_rule TEXT, cost_yen INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS touches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL, company_id INTEGER NOT NULL,
  channel TEXT NOT NULL, variant TEXT, step INTEGER DEFAULT 1,
  subject TEXT, body TEXT, sent_at TEXT, unit_cost_yen INTEGER DEFAULT 0,
  delivered INTEGER DEFAULT 1, opened INTEGER DEFAULT 0, responded INTEGER DEFAULT 0,
  signed_up INTEGER DEFAULT 0, activated INTEGER DEFAULT 0, paid INTEGER DEFAULT 0,
  responded_at TEXT, paid_at TEXT, mrr_yen INTEGER DEFAULT 0, note TEXT,
  UNIQUE(campaign_id, company_id, step),
  FOREIGN KEY(campaign_id) REFERENCES campaigns(id),
  FOREIGN KEY(company_id)  REFERENCES companies(id)
);

CREATE TABLE IF NOT EXISTS dormant (
  company_id INTEGER PRIMARY KEY, from_campaign INTEGER, entered_at TEXT,
  cycles INTEGER DEFAULT 1, next_eligible_at TEXT, revive_signal TEXT
);

-- 配信停止(サプレッション): ここに入った会社には二度と送らない。
-- 理由と受付日を必ず残す。法令対応と、後で「なぜ送っていないのか」を説明できる状態のため。
CREATE TABLE IF NOT EXISTS suppression (
  company_id INTEGER PRIMARY KEY,
  reason TEXT NOT NULL,        -- optout / complaint / bounce_hard / manual / competitor
  source TEXT,                 -- どこで受けたか(LP/メール返信/電話 等)
  note TEXT,
  created_at TEXT NOT NULL
);

-- 監査ログ: 誰がいつ何を実行したか。デューデリで運用実態を示す材料になる。
CREATE TABLE IF NOT EXISTS run_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  step TEXT NOT NULL, status TEXT NOT NULL,
  detail TEXT, started_at TEXT, finished_at TEXT
);

-- フォーム自動送信(FormSender)の詳細ログ。touchesは1社1件だが、こちらは
-- 試行のたびに1行残す(同じ会社に複数回トライしうるため1:多)。
-- SUCCESS/SKIP/FAILEDの内訳を後から集計できるようにするための表。
-- 個人情報配慮のため、本文そのものは保存しない(検出結果と成功根拠のみ)。
CREATE TABLE IF NOT EXISTS form_send_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL,
  tenant_id INTEGER,
  offer_id INTEGER,
  target_url TEXT,              -- 開始URL(companies.website_url等)
  contact_url TEXT,             -- 実際にフォームが見つかったページ
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,         -- SUCCESS / SKIP_* / FAILED_RETRYABLE / FAILED_UNSUPPORTED
  reason_code TEXT,
  detected_fields TEXT,         -- JSON: {"email":1,"message":1,...}
  filled_fields TEXT,           -- JSON: ["email","message",...]
  submit_attempted INTEGER DEFAULT 0,
  success_evidence TEXT,
  error_message TEXT,
  retryable INTEGER DEFAULT 0,
  playwright_run_id TEXT,
  final_url TEXT,               -- 開発・検証中のみ埋める想定
  page_title TEXT
);
"""


def connect(timeout=30.0):
    """接続は storage.py に委譲する（SQLite/Postgresの切替はそちらで判定）。

    以下はSQLite時の設定内容:

    WAL:          読みと書きが互いをブロックしない（既定のjournalは全体ロック）
    busy_timeout: 書き込み競合時に即エラーではなく待つ
    synchronous:  NORMALでWALと組むと、クラッシュ耐性を保ったまま大幅に速い

    ※ SQLiteの限界: 書き込みは同時1本。並列ワーカーを増やす段階に来たら
      DATABASE_URL を見て Postgres へ切り替える（下の resolve_backend 参照）。
    """
    import storage
    return storage.connect(timeout)


def resolve_backend():
    import storage
    return storage.backend()


def _resolve_backend_notes():
    """本番でPostgresへ移す際の切替点。
    環境変数 DATABASE_URL があればPostgresを使う想定で、
    SQL方言の差分（AUTOINCREMENT / INSERT OR IGNORE / ON CONFLICT）はここで吸収する。
    現時点ではSQLiteのみ実装し、移行時に触る箇所を1ファイルに閉じ込めておく。"""
    import os
    url = os.environ.get("DATABASE_URL")
    if url and url.startswith("postgres"):
        raise NotImplementedError(
            "Postgresバックエンドは本番移行時に実装する。"
            "差分: AUTOINCREMENT→SERIAL / INSERT OR IGNORE→ON CONFLICT DO NOTHING / "
            "PRAGMA不要 / 同時書き込み可のためRateLimiterの制約を緩められる")
    return "sqlite"


def write_lock(con):
    """複数プロセスから書く処理を直列化する。SQLiteの書き込みは同時1本のため、
    BEGIN IMMEDIATE で先にロックを取り、途中でのSQLITE_BUSYを避ける。"""
    class _Ctx:
        def __enter__(self):
            for i in range(6):
                try:
                    con.execute("BEGIN IMMEDIATE")
                    return con
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e) and "busy" not in str(e):
                        raise
                    time.sleep(0.5 * (2 ** i))
            raise TimeoutError("書き込みロックを取得できませんでした")

        def __exit__(self, exc_type, *_):
            con.execute("ROLLBACK" if exc_type else "COMMIT")
            return False
    return _Ctx()


def migrate(con):
    """既存DBを壊さずに最新スキーマへ寄せる。何度実行しても安全(冪等)。"""
    C.OUT_DIR.mkdir(exist_ok=True)
    con.executescript(SCHEMA)
    # 旧バージョンで欠けている列を後付け
    for table, col, ddl in [
        ("companies", "name_norm", "TEXT"), ("companies", "score_v2", "REAL"),
        ("companies", "dedup_of", "INTEGER"), ("companies", "enriched_at", "TEXT"),
        ("companies", "email", "TEXT"),
        ("companies", "hiring_source", "TEXT"), ("companies", "is_target_business", "INTEGER"),
        ("companies", "prescore_selected", "INTEGER"),  # prescore.pyの選出結果(0/1)
        ("companies", "stratum", "TEXT"),  # prescore.pyの層("honmei"=本命/"control"=対照)
        ("companies", "contact_url", "TEXT"),  # 問い合わせフォームURL(mikomeru由来)
        ("companies", "has_contact_form", "INTEGER"),  # 問い合わせフォーム有無(mikomeru由来)
        ("companies", "corporate_no", "TEXT"),  # 法人番号(国税庁13桁)。mikomeru取込で判明した分のみ
        ("companies", "data_source", "TEXT"),  # NULL=国交省名簿(既定) / "mikomeru"=mikomeru由来の新規追加
        ("touches", "step", "INTEGER DEFAULT 1"),
    ]:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # 耐障害化・マルチオファーのテーブルも常に作る（実行順序に依存させない）
    import resilience, offers as _offers
    con.executescript(resilience.SCHEMA)
    con.executescript(_offers.SCHEMA)
    con.executescript(INDEXES)   # 列追加のあとにインデックスを張る
    con.execute("""INSERT INTO meta (key, value) VALUES ('schema_version', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(SCHEMA_VERSION),))
    con.commit()
    return SCHEMA_VERSION


# ── 名寄せ ────────────────────────────────
_STRIP = ["株式会社", "有限会社", "合同会社", "(株)", "(有)", "(合)",
          "（株）", "（有）", "（合）", "㈱", "㈲", " ", "　"]

def normalize_name(name: str) -> str:
    """商号の表記ゆれを吸収。同一社が複数の許可番号で登録されているケースを潰す。"""
    s = (name or "").strip()
    for t in _STRIP:
        s = s.replace(t, "")
    return s.translate(str.maketrans("ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ０１２３４５６７８９",
                                     "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")).lower()


def dedup(con):
    """同一都道府県・同一正規化商号を重複とみなし、代表1件に寄せる。
    name_normは毎回フル再計算する(NULLのみ埋める方式だとnormalize_name()の
    ロジック変更が既存行のキャッシュ値に反映されず、古い基準のまま重複判定して
    しまう事故が起きるため)。"""
    for r in con.execute("SELECT id, name FROM companies").fetchall():
        con.execute("UPDATE companies SET name_norm=? WHERE id=?", (normalize_name(r["name"]), r["id"]))
    con.commit()
    dupes = con.execute("""
        SELECT MIN(id) keep, COUNT(*) n, name_norm, pref FROM companies
        WHERE dedup_of IS NULL AND name_norm IS NOT NULL
        GROUP BY name_norm, pref HAVING n > 1""").fetchall()
    merged = 0
    for d in dupes:
        con.execute("""UPDATE companies SET dedup_of=? WHERE name_norm=? AND pref=? AND id<>?""",
                    (d["keep"], d["name_norm"], d["pref"], d["keep"]))
        merged += d["n"] - 1
    con.commit()
    return merged


# ── 接触ガード（全送信がここを通る） ──────────
def suppress(con, company_id, reason, source=None, note=None):
    con.execute("""INSERT INTO suppression (company_id, reason, source, note, created_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(company_id) DO UPDATE SET
          reason=excluded.reason, source=excluded.source, note=excluded.note""",
        (company_id, reason, source, note, datetime.now().isoformat(timespec="seconds")))
    con.commit()


def can_contact(con, company_id, allow_warm=False):
    """接触してよいか判定。(可否, 理由) を返す。
    ここを通らない送信経路を作らないこと。

    allow_warm=True にすると「反応済み」チェックだけを外す。
    既存顧客への別オファー案内など、意図的な再接触の時だけ使うこと。"""
    if con.execute("SELECT 1 FROM suppression WHERE company_id=?", (company_id,)).fetchone():
        return False, "配信停止リスト"
    if con.execute("SELECT 1 FROM companies WHERE id=? AND dedup_of IS NOT NULL",
                   (company_id,)).fetchone():
        return False, "重複レコード(代表社へ集約済)"
    # 反応済み＝もう営業対象ではなく商談対象。新規の売り込みを重ねない。
    # （campaign.py が反応済みの会社にも新規キャンペーンを組んでいたのをテストが検出）
    if not allow_warm:
        warm = con.execute("""SELECT responded, paid FROM touches
                              WHERE company_id=? AND (responded=1 OR paid=1) LIMIT 1""",
                           (company_id,)).fetchone()
        if warm:
            return False, "反応済み(商談対象)" if not warm[1] else "既存顧客"

    n = con.execute("SELECT COUNT(*) FROM touches WHERE company_id=? AND sent_at IS NOT NULL",
                    (company_id,)).fetchone()[0]
    if n >= C.MAX_LIFETIME_TOUCHES:
        return False, f"生涯接触上限({C.MAX_LIFETIME_TOUCHES}回)到達"
    last = con.execute("""SELECT MAX(sent_at) FROM touches
                          WHERE company_id=? AND sent_at IS NOT NULL""", (company_id,)).fetchone()[0]
    if last:
        try:
            gap = (datetime.now() - datetime.fromisoformat(last)).days
            if gap < C.MIN_TOUCH_INTERVAL_DAYS:
                return False, f"最短間隔未満(前回{gap}日前 / 下限{C.MIN_TOUCH_INTERVAL_DAYS}日)"
        except ValueError:
            pass
    return True, "OK"


def contactable_ids(con, ids, allow_warm=False):
    """複数IDを一括判定。除外された理由の内訳も返す。"""
    ok, blocked = [], {}
    for i in ids:
        allowed, why = can_contact(con, i, allow_warm=allow_warm)
        if allowed:
            ok.append(i)
        else:
            blocked[why] = blocked.get(why, 0) + 1
    return ok, blocked


# ── 許可番号の連番(社歴の代理変数) ──────────
# learn.py(V2学習)とprescore.py(AI不要の事前絞込)の両方が使うため、
# 重い依存(numpy/scikit-learn)を持つlearn.pyではなくここに置く。
def _license_seq(license_no):
    """許可番号から連番部分を抜き出す。"第10000号"形式・末尾が素の数字の
    形式のどちらにも対応。パース不能ならNone。"""
    if not license_no:
        return None
    m = re.search(r"第(\d+)号", license_no)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*$", license_no)
    if m:
        return int(m.group(1))
    nums = re.findall(r"\d+", license_no)
    return int(nums[-1]) if nums else None


def compute_license_seq_pct(con):
    """許可番号の連番を都道府県内でパーセンタイル化する({company_id: 0〜1})。
    許可番号は初回付与後、更新しても変わらない番号のため、5年ごとに更新される
    founded_year(許可年月日)と違って社歴の代理変数として使える
    (連番が小さい=古くから許可を持つ=社歴が長い、値が小さいほど社歴が長い)。
    実データ(東京都名簿, n=13,897)で連番と資本金の間にSpearman rho=-0.35
    (p≈0)の負の相関を確認済み。パース不能な許可番号は中央値0.5として扱う。"""
    rows = con.execute("SELECT id, pref, license_no FROM companies").fetchall()
    by_pref = {}
    for r in rows:
        by_pref.setdefault(r["pref"], []).append((r["id"], _license_seq(r["license_no"])))
    pct = {}
    for items in by_pref.values():
        known = sorted((cid, seq) for cid, seq in items if seq is not None)
        n = len(known)
        for rank, (cid, _seq) in enumerate(known):
            pct[cid] = rank / (n - 1) if n > 1 else 0.5
        for cid, seq in items:
            if seq is None:
                pct[cid] = 0.5
    return pct


def log_run(con, step, status, detail=None, started=None):
    con.execute("""INSERT INTO run_log (step, status, detail, started_at, finished_at)
        VALUES (?,?,?,?,?)""",
        (step, status, detail, started or datetime.now().isoformat(timespec="seconds"),
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
