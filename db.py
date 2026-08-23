"""
db.py — スキーマ / マイグレーション / 接触ガード
これまで各スクリプトがバラバラにテーブルを作っていたのを一本化する。
特に重要なのが can_contact(): 配信停止・接触上限・間隔を一箇所で判定する関門。
どのスクリプトからも必ずここを通す設計にして、「うっかり送ってしまった」を構造的に防ぐ。
"""
import json
import re
import secrets
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
CREATE INDEX IF NOT EXISTS idx_companies_owner ON companies(owner_tenant_id);
CREATE INDEX IF NOT EXISTS idx_tlist_tenant ON target_lists(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tlm_list ON target_list_members(list_id);
CREATE INDEX IF NOT EXISTS idx_msgtmpl_tenant ON message_templates(tenant_id);
CREATE INDEX IF NOT EXISTS idx_sendtmpl_tenant ON sender_templates(tenant_id);
CREATE INDEX IF NOT EXISTS idx_staff_tenant ON staff(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tlm_status ON target_list_members(send_status);
CREATE INDEX IF NOT EXISTS idx_formlog_list ON form_send_log(list_id);
CREATE INDEX IF NOT EXISTS idx_emailtok_touch ON email_tracking_tokens(touch_id);
CREATE INDEX IF NOT EXISTS idx_scheduled_sends_due ON scheduled_sends(status, scheduled_at);
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

-- テナント別の送信除外: suppression(法令対応・全テナット共通)とは別に、
-- 「このテナントだけは送りたくない」(競合他社等)という経営判断の除外を持つ。
-- 他テナントの送信には影響しない。
CREATE TABLE IF NOT EXISTS tenant_exclusions (
  tenant_id INTEGER NOT NULL,
  company_id INTEGER NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, company_id)
);

-- テナントが保存する送信文章テンプレート(件名・本文の定型文)。
-- 送信自体は既存のtarget_lists.send_list()がそのまま担う。ここは
-- フォームへ件名・本文を自動入力するための入れ物でしかない。
CREATE TABLE IF NOT EXISTS message_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- テナントが登録できる送信元(送信者表示名・返信先・住所・配信停止URL)の
-- 複数パターン。「有効にする」を押すと tenants.sender_* を上書きする形で
-- 反映する(senders.send_campaign()側のテナント解決ロジックは変更しない)。
CREATE TABLE IF NOT EXISTS sender_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  sender_name TEXT NOT NULL,
  sender_email TEXT NOT NULL,
  sender_address TEXT,
  optout_url TEXT,
  created_at TEXT NOT NULL
);

-- お知らせ(全テナント共通の告知)。運用側(HQ)がannouncements_cli.pyで
-- 投稿する。他のテナント別テーブルと違いtenant_idを持たない=全員に見える。
CREATE TABLE IF NOT EXISTS announcements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  published INTEGER DEFAULT 1,
  created_at TEXT NOT NULL
);

-- Kill Switch(全体停止)。異常検知時に管理者が全送信を即時停止するための1行だけの
-- テーブル(id=1固定)。senders.send_campaign()が全送信経路(手動送信/cron/
-- Stock Factory運用API)の唯一の合流点なので、そこ1箇所でこのフラグを見れば
-- 全経路に効く。dry_runの送信はここでブロックしない(実サイトへ触れないため)。
CREATE TABLE IF NOT EXISTS kill_switch (
  id INTEGER PRIMARY KEY CHECK (id=1),
  stopped INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  updated_at TEXT NOT NULL,
  updated_by TEXT
);

-- Kill Switch(テナント別停止)。特定テナントだけ送信を止めたい場合。行が
-- 存在する=停止中(tenant_exclusionsと同じ「存在=適用」パターン)。
CREATE TABLE IF NOT EXISTS tenant_kill_switch (
  tenant_id INTEGER PRIMARY KEY,
  reason TEXT,
  updated_at TEXT NOT NULL,
  updated_by TEXT
);

-- URLクリック計測用のトラッキングトークン(MIKOMERUの「URLアクセスの記録」相当。
-- T16でkind='click'側を実装。kind='open'<メール開封計測>は依然データ構造のみで、
-- メール送信機能自体(T2)が無いため未実装)。
-- token単体からtenant/campaign/company/recipientを推測できないよう、
-- 十分に推測困難なランダム値にする(secrets.token_urlsafe()で生成)。
-- GET /track/click/{token} → touches.email_clicked_at等を更新して本来のURLへ
-- 302リダイレクトする(api.py h_track_click()参照)。
CREATE TABLE IF NOT EXISTS email_tracking_tokens (
  token TEXT PRIMARY KEY,
  touch_id INTEGER NOT NULL,
  kind TEXT NOT NULL,  -- 'open' | 'click'
  target_url TEXT,     -- kind='click'の場合の本来のリダイレクト先
  created_at TEXT NOT NULL,
  FOREIGN KEY(touch_id) REFERENCES touches(id)
);

-- 自動送信で失敗した企業への「自動入力(手動送信サポート)」機能(MIKOMERU同等)。
-- list_builder.htmlの「自動入力」ボタン押下時、送信予定の値を1件だけここに置き、
-- ブックマークレット(送信先企業のドメイン上で動く別オリジンのJS)が
-- GET /api/tenant/autofill/pendingで取りに来て、目の前のフォームへ入力する。
-- テナントごとに常に最新の1件だけを保持すれば足りる(一度に1社ずつ手作業する運用のため)。
CREATE TABLE IF NOT EXISTS autofill_queue (
  tenant_id INTEGER PRIMARY KEY,
  url TEXT NOT NULL,
  values_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 予約送信(MIKOMERU同等の「送信開始日時を指定する」機能)。指定日時になるまでは
-- 何も送らない。実行自体は既存のtarget_lists.send_list()にそのまま委譲するため、
-- can_contact()・Kill Switch・冪等性等のガードは変更なしでそのまま効く
-- (scheduled_send_cli.pyがcronから定期的にPENDINGかつ期限到来分を拾って
-- send_list()を呼ぶだけで、新しい送信経路を作らない)。
CREATE TABLE IF NOT EXISTS scheduled_sends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  list_id INTEGER NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 0,
  scheduled_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING / DONE / FAILED / CANCELLED
  result_json TEXT,
  created_at TEXT NOT NULL,
  executed_at TEXT
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
  page_title TEXT,
  page_text_snippet TEXT        -- 開発・検証中のみ埋める想定(成功判定できなかった原因調査用)
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
    # 耐障害化・マルチオファー・送信先リストのテーブルも常に作る(実行順序に依存させない)。
    # 下のALTER列(tenants.api_key等)より前に置くこと — でないと対象テーブルが
    # まだ存在せずALTERが失敗する
    import resilience, offers as _offers, target_lists as _tl
    con.executescript(resilience.SCHEMA)
    con.executescript(_offers.SCHEMA)
    con.executescript(_tl.SCHEMA)
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
        ("companies", "owner_tenant_id", "INTEGER"),  # NULL=全テナント共有マスタ / 値あり=そのテナント専用(CSV取込)
        ("touches", "step", "INTEGER DEFAULT 1"),
        ("form_send_log", "page_text_snippet", "TEXT"),  # 成功判定できなかった原因調査用
        ("campaigns", "offer_id", "INTEGER"),  # compose.pyで確定したオファー。送信時のテナント解決に使う
        ("tenants", "api_key", "TEXT"),  # target_listsのAPI認証キー(SaaS販売用テナントに発行)
        ("target_lists", "campaign_id", "INTEGER"),  # send_list()で一度送信すると紐づく(二重送信防止)
        # 企業1社×1リストの「現在の送信状態」。履歴(何度目のどの結果か)は
        # form_send_log側が持つので、ここは最新状態のスナップショットに徹する。
        ("target_list_members", "send_status", "TEXT DEFAULT 'PENDING'"),
        ("target_list_members", "reason_code", "TEXT"),
        ("target_list_members", "retry_count", "INTEGER DEFAULT 0"),
        ("target_list_members", "last_error", "TEXT"),
        ("target_list_members", "latest_result", "TEXT"),
        ("target_list_members", "started_at", "TEXT"),
        ("target_list_members", "submitted_at", "TEXT"),
        ("target_list_members", "completed_at", "TEXT"),
        ("target_list_members", "contacted_at", "TEXT"),
        ("target_list_members", "next_retry_at", "TEXT"),
        ("target_list_members", "created_at", "TEXT"),
        ("target_list_members", "updated_at", "TEXT"),
        # 返信・商談化・受注は自動取得せず、担当者がlist_builder.htmlから手動記録する(β版)
        ("target_list_members", "replied", "INTEGER DEFAULT 0"),
        ("target_list_members", "replied_at", "TEXT"),
        ("target_list_members", "deal", "INTEGER DEFAULT 0"),
        ("target_list_members", "deal_at", "TEXT"),
        ("target_list_members", "won", "INTEGER DEFAULT 0"),
        ("target_list_members", "won_at", "TEXT"),
        ("target_list_members", "memo", "TEXT"),
        # 原価計測。AIを使わない処理は0のままでよい(将来compose.py等のAI利用に接続する)
        ("form_send_log", "list_id", "INTEGER"),
        ("form_send_log", "retry_count", "INTEGER DEFAULT 0"),
        ("form_send_log", "execution_seconds", "REAL"),
        ("form_send_log", "ai_tokens_input", "INTEGER DEFAULT 0"),
        ("form_send_log", "ai_tokens_output", "INTEGER DEFAULT 0"),
        ("form_send_log", "ai_cost_yen", "REAL DEFAULT 0"),
        ("form_send_log", "external_api_cost_yen", "REAL DEFAULT 0"),
        ("form_send_log", "estimated_server_cost_yen", "REAL DEFAULT 0"),
        ("form_send_log", "total_estimated_cost_yen", "REAL DEFAULT 0"),
        # メール開封・クリック計測(P2: データ構造のみ。メール送信機能自体は未実装)。
        # 開封検知は実際に読んだことの証明にはならない(Apple Mail Privacy
        # Protection・画像自動読込等の影響を受ける)ため、UIでは「開封検知」
        # 「推定開封」等の表現にとどめ、返信>クリック>開封の順で信頼する設計とする。
        ("touches", "email_sent_at", "TEXT"),
        ("touches", "email_delivered_at", "TEXT"),
        ("touches", "email_opened_at", "TEXT"),
        ("touches", "email_first_opened_at", "TEXT"),
        ("touches", "email_last_opened_at", "TEXT"),
        ("touches", "email_open_count", "INTEGER DEFAULT 0"),
        ("touches", "email_clicked_at", "TEXT"),
        ("touches", "email_click_count", "INTEGER DEFAULT 0"),
        ("touches", "email_bounced_at", "TEXT"),
        ("touches", "email_bounce_type", "TEXT"),
        ("touches", "email_unsubscribed_at", "TEXT"),
        # 送信前後のスクリーンショット(MIKOMERU同等の目視確認機能)。
        # ファイル自体はout/form_screenshots/配下に保存し、ここにはパスのみ持つ。
        ("form_send_log", "screenshot_before_path", "TEXT"),
        ("form_send_log", "screenshot_after_path", "TEXT"),
        # 送信元の姓・名・フリガナ・郵便番号(MIKOMERU同等。姓/名/カナ/郵便番号が
        # 別欄になっている問い合わせフォームに対応するため)。任意項目のため
        # 未設定なら空のまま(senders.py側でsender_name等からの妥当な既定値へ
        # フォールバックする。以前は姓欄・名欄の両方に会社名をそのまま入れ、
        # フリガナ欄は常に固定文字列"アシベース"を入れていたが、これは
        # カスタムの送信者名を設定したテナントでは明らかに誤った内容になるため、
        # 未設定なら空欄のままにする方針に変更する)
        ("tenants", "sender_last_name", "TEXT"), ("tenants", "sender_first_name", "TEXT"),
        ("tenants", "sender_last_name_kana", "TEXT"), ("tenants", "sender_first_name_kana", "TEXT"),
        ("tenants", "sender_postal_code", "TEXT"),
        ("sender_templates", "sender_last_name", "TEXT"),
        ("sender_templates", "sender_first_name", "TEXT"),
        ("sender_templates", "sender_last_name_kana", "TEXT"),
        ("sender_templates", "sender_first_name_kana", "TEXT"),
        ("sender_templates", "sender_postal_code", "TEXT"),
        # 送信元の都道府県・市区町村・丁目番地・ビル名/部屋番号・電話番号(MIKOMERU同等)。
        # 従来はsender_address 1本のfree textだったが、住所が郵便番号/都道府県/市区町村/
        # 丁目番地/建物名で別々の入力欄になっている問い合わせフォームには埋められなかった。
        # sender_addressは後方互換のため残し、フォーム入力の実体はこちらの構造化項目を優先する
        # (form_navigator.pyの新設kind: prefecture/city/block/building)。
        ("tenants", "sender_prefecture", "TEXT"), ("tenants", "sender_city", "TEXT"),
        ("tenants", "sender_block", "TEXT"), ("tenants", "sender_building", "TEXT"),
        ("tenants", "sender_phone", "TEXT"),
        ("sender_templates", "sender_prefecture", "TEXT"), ("sender_templates", "sender_city", "TEXT"),
        ("sender_templates", "sender_block", "TEXT"), ("sender_templates", "sender_building", "TEXT"),
        ("sender_templates", "sender_phone", "TEXT"),
        # 送信結果ごとの備考(MIKOMERUの「架電済み」等の営業メモ)と、自動入力アシスト後に
        # 人が手動でフォームを完了させたことを示すフラグ(MIKOMERUの「手動送信済み」相当)。
        ("form_send_log", "note", "TEXT"),
        ("form_send_log", "manual_sent_at", "TEXT"),
        # 「URLアクセスの記録」(MIKOMERU同等)。予約送信は実行時点(cron)まで
        # このフラグを保持しておく必要があるためscheduled_sendsにも持たせる
        # (即時送信はAPIリクエストの引数としてsend_campaign()まで直接受け渡すのみで、
        # DBへは保存しない=campaigns/touchesは再送で使い回されるため、そこに保存すると
        # 別の送信操作の設定が漏れて残ってしまう)。
        ("scheduled_sends", "track_clicks", "INTEGER DEFAULT 0"),
    ]:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    con.executescript(INDEXES)   # 列追加のあとにインデックスを張る
    con.execute("""INSERT INTO meta (key, value) VALUES ('schema_version', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(SCHEMA_VERSION),))
    # Kill Switchの初期値は「停止中」(安全側)。テーブルがまだ無い(=初回起動)場合のみ
    # 作る。本番送信を許可するには、必ず人間が明示的にkill_switch_cli.py resumeで
    # 解除する必要がある(黙って有効化されることはない)。
    con.execute("""INSERT OR IGNORE INTO kill_switch (id, stopped, reason, updated_at, updated_by)
        VALUES (1, 1, '初期値:人間による解除待ち(kill_switch_cli.py resume)', ?, 'system')""",
        (datetime.now().isoformat(timespec="seconds"),))
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


def can_contact(con, company_id, tenant_id=None, allow_warm=False):
    """接触してよいか判定。(可否, 理由) を返す。
    ここを通らない送信経路を作らないこと。

    tenant_idを渡すと、そのテナントのtenant_exclusions(経営判断の除外。
    suppressionと違い他テナントには影響しない)も合わせて確認する。
    allow_warm=True にすると「反応済み」チェックだけを外す。
    既存顧客への別オファー案内など、意図的な再接触の時だけ使うこと。"""
    if con.execute("SELECT 1 FROM suppression WHERE company_id=?", (company_id,)).fetchone():
        return False, "配信停止リスト"
    if tenant_id is not None and con.execute(
            "SELECT 1 FROM tenant_exclusions WHERE tenant_id=? AND company_id=?",
            (tenant_id, company_id)).fetchone():
        return False, "テナント除外設定"
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

    # ドライラン(dry_run)は実サイトへ何も送っていないため、生涯接触上限・最短間隔の
    # どちらにもカウントしない(sent_atはドライランでも本番と同じ形で立つため、
    # provider_id=mock_で始まるnoteをドライラン分として除外する。他の判定
    # <sync_target_list_member_status()等>と同じ判別方法)。
    n = con.execute("""SELECT COUNT(*) FROM touches WHERE company_id=? AND sent_at IS NOT NULL
                       AND instr(COALESCE(note,''), 'provider_id=mock_') = 0""",
                    (company_id,)).fetchone()[0]
    if n >= C.MAX_LIFETIME_TOUCHES:
        return False, f"生涯接触上限({C.MAX_LIFETIME_TOUCHES}回)到達"
    last = con.execute("""SELECT MAX(sent_at) FROM touches
                          WHERE company_id=? AND sent_at IS NOT NULL
                          AND instr(COALESCE(note,''), 'provider_id=mock_') = 0""",
                       (company_id,)).fetchone()[0]
    if last:
        try:
            gap = (datetime.now() - datetime.fromisoformat(last)).days
            if gap < C.MIN_TOUCH_INTERVAL_DAYS:
                return False, f"最短間隔未満(前回{gap}日前 / 下限{C.MIN_TOUCH_INTERVAL_DAYS}日)"
        except ValueError:
            pass
    return True, "OK"


def contactable_ids(con, ids, tenant_id=None, allow_warm=False):
    """複数IDを一括判定。除外された理由の内訳も返す。"""
    ok, blocked = [], {}
    for i in ids:
        allowed, why = can_contact(con, i, tenant_id=tenant_id, allow_warm=allow_warm)
        if allowed:
            ok.append(i)
        else:
            blocked[why] = blocked.get(why, 0) + 1
    return ok, blocked


# ── テナント別送信除外(送信除外設定画面が使う) ──
def exclude_for_tenant(con, tenant_id, company_id, reason=None):
    con.execute("""INSERT INTO tenant_exclusions (tenant_id, company_id, reason, created_at)
        VALUES (?,?,?,?)
        ON CONFLICT(tenant_id, company_id) DO UPDATE SET reason=excluded.reason""",
        (tenant_id, company_id, reason, datetime.now().isoformat(timespec="seconds")))
    con.commit()


def unexclude_for_tenant(con, tenant_id, company_id):
    con.execute("DELETE FROM tenant_exclusions WHERE tenant_id=? AND company_id=?",
                (tenant_id, company_id))
    con.commit()


def list_tenant_exclusions(con, tenant_id):
    rows = con.execute("""SELECT e.company_id, e.reason, e.created_at, c.name, c.pref
        FROM tenant_exclusions e LEFT JOIN companies c ON c.id = e.company_id
        WHERE e.tenant_id=? ORDER BY e.created_at DESC""", (tenant_id,)).fetchall()
    return [dict(r) for r in rows]


# ── 送信文章テンプレート(送信フォームの件名・本文を自動入力するための保存領域) ──
def add_message_template(con, tenant_id, name, subject, body):
    cur = con.execute("""INSERT INTO message_templates (tenant_id, name, subject, body, created_at)
        VALUES (?,?,?,?,?)""",
        (tenant_id, name, subject, body, datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return cur.lastrowid


def list_message_templates(con, tenant_id):
    rows = con.execute("""SELECT id, name, subject, body, created_at FROM message_templates
        WHERE tenant_id=? ORDER BY created_at DESC""", (tenant_id,)).fetchall()
    return [dict(r) for r in rows]


def delete_message_template(con, tenant_id, template_id):
    cur = con.execute("DELETE FROM message_templates WHERE id=? AND tenant_id=?",
                       (template_id, tenant_id))
    con.commit()
    return cur.rowcount > 0


# ── 送信元テンプレート(送信者名・返信先等のパターン登録) ──
def add_sender_template(con, tenant_id, name, sender_name, sender_email,
                         sender_address="", optout_url=None, last_name=None, first_name=None,
                         last_name_kana=None, first_name_kana=None, postal_code=None,
                         prefecture=None, city=None, block=None, building=None, phone=None):
    """last_name〜phoneはすべて任意項目。姓・名・フリガナ・郵便番号・住所(都道府県/
    市区町村/丁目番地/建物名)・電話番号が別欄の問い合わせフォーム向け(MIKOMERU同等)。
    未指定なら空のまま保存し、送信時にsenders.py側で妥当な既定値へフォールバックする。"""
    cur = con.execute("""INSERT INTO sender_templates
        (tenant_id, name, sender_name, sender_email, sender_address, optout_url,
         sender_last_name, sender_first_name, sender_last_name_kana, sender_first_name_kana,
         sender_postal_code, sender_prefecture, sender_city, sender_block, sender_building,
         sender_phone, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tenant_id, name, sender_name, sender_email, sender_address, optout_url,
         last_name, first_name, last_name_kana, first_name_kana, postal_code,
         prefecture, city, block, building, phone,
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return cur.lastrowid


def list_sender_templates(con, tenant_id):
    rows = con.execute("""SELECT id, name, sender_name, sender_email, sender_address,
        optout_url, sender_last_name, sender_first_name, sender_last_name_kana,
        sender_first_name_kana, sender_postal_code, sender_prefecture, sender_city,
        sender_block, sender_building, sender_phone, created_at FROM sender_templates
        WHERE tenant_id=? ORDER BY created_at DESC""", (tenant_id,)).fetchall()
    return [dict(r) for r in rows]


def delete_sender_template(con, tenant_id, template_id):
    cur = con.execute("DELETE FROM sender_templates WHERE id=? AND tenant_id=?",
                       (template_id, tenant_id))
    con.commit()
    return cur.rowcount > 0


def activate_sender_template(con, tenant_id, template_id):
    """指定テンプレートの内容をtenants.sender_*へ反映する。
    送信時の送信者解決はsenders.send_campaign()がtenantsから読むだけなので、
    送信ロジック側には一切手を入れずに反映できる。"""
    row = con.execute("""SELECT sender_name, sender_email, sender_address, optout_url,
        sender_last_name, sender_first_name, sender_last_name_kana, sender_first_name_kana,
        sender_postal_code, sender_prefecture, sender_city, sender_block, sender_building,
        sender_phone
        FROM sender_templates WHERE id=? AND tenant_id=?""", (template_id, tenant_id)).fetchone()
    if not row:
        return False
    con.execute("""UPDATE tenants SET sender_name=?, sender_email=?, sender_address=?,
        optout_url=?, sender_last_name=?, sender_first_name=?, sender_last_name_kana=?,
        sender_first_name_kana=?, sender_postal_code=?, sender_prefecture=?, sender_city=?,
        sender_block=?, sender_building=?, sender_phone=? WHERE id=?""",
        (row["sender_name"], row["sender_email"], row["sender_address"], row["optout_url"],
         row["sender_last_name"], row["sender_first_name"], row["sender_last_name_kana"],
         row["sender_first_name_kana"], row["sender_postal_code"], row["sender_prefecture"],
         row["sender_city"], row["sender_block"], row["sender_building"], row["sender_phone"],
         tenant_id))
    con.commit()
    return True


# ── 送信ログの備考・手動送信済みフラグ(MIKOMERU同等) ──
def update_form_send_log_note(con, tenant_id, log_id, note):
    cur = con.execute("UPDATE form_send_log SET note=? WHERE id=? AND tenant_id=?",
                       (note, log_id, tenant_id))
    con.commit()
    return cur.rowcount > 0


def set_form_send_log_manual_sent(con, tenant_id, log_id, manual_sent):
    """自動入力アシスト後、人が実際にフォームを送信し終えたことを記録する
    (MIKOMERUの「手動送信済み」チェック相当)。取り消し(チェックを外す)もできるよう
    manual_sent=Falseならmanual_sent_atをNULLへ戻す。"""
    value = datetime.now().isoformat(timespec="seconds") if manual_sent else None
    cur = con.execute("UPDATE form_send_log SET manual_sent_at=? WHERE id=? AND tenant_id=?",
                       (value, log_id, tenant_id))
    con.commit()
    return cur.rowcount > 0


# ── 予約送信(MIKOMERU同等の「送信開始日時を指定する」機能) ──
def create_scheduled_send(con, tenant_id, list_id, subject, body, dry_run, scheduled_at,
                           track_clicks=False):
    cur = con.execute("""INSERT INTO scheduled_sends
        (tenant_id, list_id, subject, body, dry_run, scheduled_at, track_clicks, status, created_at)
        VALUES (?,?,?,?,?,?,?,'PENDING',?)""",
        (tenant_id, list_id, subject, body, 1 if dry_run else 0, scheduled_at,
         1 if track_clicks else 0, datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return cur.lastrowid


# ── URLクリック計測(MIKOMERUの「URLアクセスの記録」相当) ──
def create_click_token(con, touch_id, target_url):
    """送信文章中の1つのURLにつき1個のトークンを発行する。token単体からは
    tenant/campaign/company/recipientを推測できない(推測困難なランダム値)。"""
    token = secrets.token_urlsafe(16)
    con.execute("""INSERT INTO email_tracking_tokens (token, touch_id, kind, target_url, created_at)
        VALUES (?,?,'click',?,?)""",
        (token, touch_id, target_url, datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return token


def resolve_click_token(con, token):
    """クリック計測トークンを解決し、初回クリック日時(email_clicked_at)と
    クリック回数(email_click_count)をtouchesへ記録する。存在しないトークン
    (typo・改ざん・期限切れ等)ならNoneを返す(呼び出し側は404にする)。"""
    row = con.execute("""SELECT touch_id, target_url FROM email_tracking_tokens
        WHERE token=? AND kind='click'""", (token,)).fetchone()
    if not row:
        return None
    con.execute("""UPDATE touches SET
        email_clicked_at = COALESCE(email_clicked_at, ?),
        email_click_count = email_click_count + 1
        WHERE id=?""", (datetime.now().isoformat(timespec="seconds"), row["touch_id"]))
    con.commit()
    return row["target_url"]


def list_scheduled_sends(con, tenant_id, list_id=None):
    q = """SELECT s.id, s.list_id, tl.name list_name, s.subject, s.dry_run, s.scheduled_at,
            s.track_clicks, s.status, s.created_at, s.executed_at
        FROM scheduled_sends s LEFT JOIN target_lists tl ON tl.id = s.list_id
        WHERE s.tenant_id=?"""
    params = [tenant_id]
    if list_id is not None:
        q += " AND s.list_id=?"
        params.append(list_id)
    q += " ORDER BY s.scheduled_at"
    return [dict(r) for r in con.execute(q, params).fetchall()]


def cancel_scheduled_send(con, tenant_id, scheduled_id):
    """PENDINGのものだけキャンセルできる(既に実行済み・失敗済みは今さら止められない)。"""
    cur = con.execute("""UPDATE scheduled_sends SET status='CANCELLED'
        WHERE id=? AND tenant_id=? AND status='PENDING'""", (scheduled_id, tenant_id))
    con.commit()
    return cur.rowcount > 0


def due_scheduled_sends(con, now_iso):
    """期限が来たPENDINGを取得する。scheduled_send_cli.pyがcronから呼ぶ。"""
    rows = con.execute("""SELECT id, tenant_id, list_id, subject, body, dry_run, track_clicks
        FROM scheduled_sends WHERE status='PENDING' AND scheduled_at<=?
        ORDER BY scheduled_at""", (now_iso,)).fetchall()
    return [dict(r) for r in rows]


def finish_scheduled_send(con, scheduled_id, status, result=None):
    con.execute("""UPDATE scheduled_sends SET status=?, result_json=?, executed_at=?
        WHERE id=?""",
        (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
         datetime.now().isoformat(timespec="seconds"), scheduled_id))
    con.commit()


# ── お知らせ(全テナント共通。投稿はannouncements_cli.py) ──
def add_announcement(con, title, body, published=True):
    cur = con.execute("""INSERT INTO announcements (title, body, published, created_at)
        VALUES (?,?,?,?)""",
        (title, body, 1 if published else 0, datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return cur.lastrowid


def list_announcements(con, published_only=True):
    q = "SELECT id, title, body, published, created_at FROM announcements"
    if published_only:
        q += " WHERE published=1"
    q += " ORDER BY created_at DESC"
    return [dict(r) for r in con.execute(q).fetchall()]


def set_announcement_published(con, announcement_id, published):
    cur = con.execute("UPDATE announcements SET published=? WHERE id=?",
                       (1 if published else 0, announcement_id))
    con.commit()
    return cur.rowcount > 0


# ── Kill Switch(異常時の即時送信停止) ──
def kill_switch_status(con, tenant_id=None):
    """(stopped: bool, reason: str|None) を返す。全体停止が最優先、
    次にテナント別停止を見る。senders.send_campaign()が実送信の直前(dry_run=False
    の行のみ)でこれを呼ぶことで、手動送信・cron・Stock Factory運用APIの
    すべての経路に同時に効く(=新しい送信経路を作らない限りバイパスできない)。"""
    g = con.execute("SELECT stopped, reason FROM kill_switch WHERE id=1").fetchone()
    if g and g["stopped"]:
        return True, g["reason"] or "全体停止中"
    if tenant_id is not None:
        t = con.execute("SELECT reason FROM tenant_kill_switch WHERE tenant_id=?",
                         (tenant_id,)).fetchone()
        if t:
            return True, t["reason"] or "このテナントは停止中"
    return False, None


def set_global_kill_switch(con, stopped, reason=None, updated_by=None):
    now = datetime.now().isoformat(timespec="seconds")
    con.execute("""INSERT INTO kill_switch (id, stopped, reason, updated_at, updated_by)
        VALUES (1,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET stopped=excluded.stopped, reason=excluded.reason,
            updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
        (1 if stopped else 0, reason, now, updated_by))
    con.commit()


def set_tenant_kill_switch(con, tenant_id, stopped, reason=None, updated_by=None):
    now = datetime.now().isoformat(timespec="seconds")
    if stopped:
        con.execute("""INSERT INTO tenant_kill_switch (tenant_id, reason, updated_at, updated_by)
            VALUES (?,?,?,?)
            ON CONFLICT(tenant_id) DO UPDATE SET reason=excluded.reason,
                updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
            (tenant_id, reason, now, updated_by))
    else:
        con.execute("DELETE FROM tenant_kill_switch WHERE tenant_id=?", (tenant_id,))
    con.commit()


def list_tenant_kill_switches(con):
    rows = con.execute("""SELECT k.tenant_id, t.name tenant_name, k.reason, k.updated_at
        FROM tenant_kill_switch k LEFT JOIN tenants t ON t.id = k.tenant_id
        ORDER BY k.updated_at DESC""").fetchall()
    return [dict(r) for r in rows]


# ── 企業1社×1リストの送信状態(target_list_members) ──
# 送信の実行そのものはsenders.send_campaign()に完全に委譲する(新しい送信経路は
# 作らない)。ここはその結果を「1社ごとの現在状態」としてtarget_list_membersへ
# 反映するだけの後処理。履歴(何度目のどの結果か)はform_send_logが持つので、
# ここは上書きしてよい最新状態のスナップショットに徹する。
def sync_target_list_member_status(con, list_id, campaign_id, step=1):
    """send_campaign()の実行直後に呼ぶ。呼び出し側がdry_run=Falseのときだけ呼ぶこと
    (呼ぶこと自体は安全だが、意味のある更新にはならない。理由は下記)。

    重要な注意: touches.sent_atはdry_run/実送信を問わず成功時に同じ形で立つため
    (SendResult.provider_idが"mock_"接頭辞かどうかでしか判別できない)、
    「sent_atがある=実送信成功」と単純に判定してはいけない。過去にdry_runで
    「送信」した企業を、後から本番送信した際にまとめて同期すると、dry_run分の
    行まで誤ってSUCCESS扱いになってしまう。noteに"provider_id=mock_"が
    含まれるかどうかで、実際に外部へ届いたかを判別する。"""
    rows = con.execute("SELECT company_id, sent_at, note FROM touches WHERE campaign_id=? AND step=?",
                        (campaign_id, step)).fetchall()
    now = datetime.now().isoformat(timespec="seconds")
    for r in rows:
        company_id, note = r["company_id"], r["note"] or ""
        log = con.execute("""SELECT status, reason_code, error_message, retry_count
            FROM form_send_log WHERE company_id=? AND list_id=?
            ORDER BY id DESC LIMIT 1""", (company_id, list_id)).fetchone()

        if r["sent_at"] and "provider_id=mock_" not in note:
            status = "SUCCESS"
        elif "Kill Switch" in note:
            status = "STOPPED"
        elif "送信中止" in note:
            status = "SKIP"  # can_contact()のガード(除外設定・配信停止・反応済み等)
        elif log and log["status"] and log["status"].startswith("SKIP"):
            status = "SKIP"
        elif log and log["status"] == "FAILED_UNSUPPORTED":
            status = "FAILED_UNSUPPORTED"
        elif note and "provider_id=mock_" not in note:
            status = "FAILED_RETRYABLE"
        else:
            continue  # 未処理、またはdry_run分のみでまだ実送信されていない行は触らない

        reason_code = log["status"] if log else None
        last_error = (log["error_message"] if log else None) or (note or None)
        retry_count = log["retry_count"] if log else 0
        latest_result = f"{status}" + (f"({reason_code})" if reason_code else "")
        con.execute("""UPDATE target_list_members SET send_status=?, reason_code=?,
            last_error=?, retry_count=?, latest_result=?, completed_at=?,
            contacted_at=CASE WHEN ?='SUCCESS' THEN ? ELSE contacted_at END, updated_at=?
            WHERE list_id=? AND company_id=?""",
            (status, reason_code, last_error, retry_count, latest_result, now,
             status, now, now, list_id, company_id))
    con.commit()


def set_target_list_member_outcome(con, tenant_id, list_id, company_id, field, value, memo=None):
    """返信・商談化・受注の手動記録(β版はメール自動取得等をしないため担当者が記録する)。
    fieldは'replied'|'deal'|'won'のいずれか。テナント境界はlist_id経由で確認する
    (他テナントのリストのmember_idを直接指定しても更新できないようにする)。"""
    if field not in ("replied", "deal", "won"):
        return False
    owns = con.execute("SELECT 1 FROM target_lists WHERE id=? AND tenant_id=?",
                        (list_id, tenant_id)).fetchone()
    if not owns:
        return False
    now = datetime.now().isoformat(timespec="seconds")
    q = f"""UPDATE target_list_members SET {field}=?, {field}_at=?, updated_at=?
        {", memo=?" if memo is not None else ""}
        WHERE list_id=? AND company_id=?"""
    params = [1 if value else 0, now if value else None, now]
    if memo is not None:
        params.append(memo)
    params += [list_id, company_id]
    cur = con.execute(q, params)
    con.commit()
    return cur.rowcount > 0


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
