"""
api.py — 外部からの反応を受け取るHTTPサーバ
ここが繋がって初めてファネルが閉じる。これがないと「送った」記録しか残らず、
CACもチャネル別成績も出せない = 売り物にならない。

エンドポイント:
  POST /api/signup    LPのフォーム送信      → responded=1, signed_up=1
  POST /api/activate  積算を1回実行した     → activated=1
  POST /api/paid      課金webhook          → paid=1, mrr_yen
  POST /api/optout    配信停止             → suppression に登録 + 未送信分を取消
  GET  /api/optout    配信停止（ワンクリック）→ メール footer のリンクから
  GET  /t/<touch_id>  開封・クリック計測    → responded=1 してLPへリダイレクト
  GET  /health        死活監視

  ── Stock Factory連携(社長のRuntimeから叩く運用API。Authorization: Bearer必須) ──
  GET  /api/ops/status     run.py statusと同等のJSON
  GET  /api/ops/metrics    metrics.pyの集計結果のJSON
  POST /api/ops/run-step   {"step","campaignId","dryRun"} → run.run_op()に委譲
                           step=send/followupは必ずsenders.send_campaign()経由になり、
                           db.can_contact()をバイパスしない(HANDOFF.mdの原則を厳守)
  GET  /api/ops/kill-switch    全体・テナント別のKill Switch状態一覧
  POST /api/ops/kill-switch    {"scope":"global"|"tenant","stopped":bool,
                           "tenant_id"(scope=tenantのみ),"reason"} → 停止/解除。
                           全送信経路の合流点であるsenders.send_campaign()側で
                           必ずチェックされる(kill_switch_cli.pyでも操作可能)

  ── 送信先リスト(SaaSとして他社に販売する側。テナントごとのapi_keyで認証) ──
  POST /api/tenant/lists/preview  {"filters"} → 該当件数のプレビュー(保存しない)
  POST /api/tenant/lists          {"name","filters"} → フィルタ型リストを保存
  POST /api/tenant/lists/csv      {"name","csv","discover_urls"} → 顧客持込CSVを
                           取り込む。discover_urls:trueで、CSVのURL列から実際に
                           問い合わせページを探す(MIKOMERUの「CSV検索(URLで検索)」
                           相当。1件ずつ実ブラウザで開くため上限あり)
  GET  /api/tenant/lists          自テナントのリスト一覧
  GET  /api/tenant/lists/<id>     リスト詳細(自テナントのものだけ。他社分は404)。
                           ?status=success|failed|skip|pending|replied|deal|wonで
                           企業ごとの送信状態で絞り込める(1社ごとのsend_status等を
                           含む。実送信結果はdry_run=falseの送信後に反映される)
  POST /api/tenant/lists/<id>/outcome  {"company_id","field":"replied"|"deal"|"won",
                           "value","memo"} → 返信・商談化・受注を手動記録(β版。
                           メール自動取得等はしない)
  POST /api/tenant/lists/<id>/send  {"subject","body","dry_run","scheduled_at"} →
                           リストへフォーム自動送信。dry_run既定true(=実サイトへは
                           送らない)。can_contact()・冪等性・ペーシング上限は
                           send_campaign()経由でそのまま適用される(HANDOFF.mdの
                           原則を厳守)。scheduled_at(未来のISO日時)を指定すると
                           即時実行せずscheduled_sendsへ予約登録するだけになる
  GET  /api/tenant/scheduled-sends?list_id=  予約送信の一覧(自テナント分のみ)
  POST /api/tenant/scheduled-sends/cancel  {"scheduled_id"} → PENDINGの予約を
                           キャンセル(実行済み・キャンセル済みは404)
  GET  /api/tenant/send-log       自テナントのフォーム自動送信履歴(form_send_log)。
                           ?company_id=で1社分の履歴だけに絞り込める
  GET  /api/tenant/send-log/{id}/screenshot?kind=before|after
                           送信前後のスクリーンショット画像(PNG)。自テナントの
                           記録のみ取得可(テナント分離)。無ければ404
  GET  /api/tenant/companies/search?q=  除外設定対象を探す簡易企業検索(2文字以上)
  GET  /api/tenant/exclusions     自テナントの送信除外設定一覧
  POST /api/tenant/exclusions          {"company_id","reason"} → 除外に追加
  POST /api/tenant/exclusions/remove   {"company_id"} → 除外を解除
                           除外はテナント別(tenant_exclusions)。全テナント共通の
                           法令対応suppressionとは別物で、can_contact()が両方を見る
  GET  /api/tenant/templates      自テナントの送信文章テンプレート一覧
  POST /api/tenant/templates           {"name","subject","body"} → 保存
  POST /api/tenant/templates/delete    {"template_id"} → 削除
  GET  /api/tenant/sender-templates    自テナントの送信元テンプレート一覧
  POST /api/tenant/sender-templates    {"name","sender_name","sender_email",
                           "sender_address","optout_url"} → 保存
  POST /api/tenant/sender-templates/delete    {"template_id"} → 削除
  POST /api/tenant/sender-templates/activate  {"template_id"} → このテナントの
                           送信者情報(tenants.sender_*)へ反映。反映先は
                           senders.send_campaign()が読む列そのものなので、
                           送信ロジック側は変更不要
  GET  /api/tenant/staff          自テナントの担当者一覧
  POST /api/tenant/staff               {"name","email"} → 担当者追加。
                           発行したapi_keyはこの応答でしか返さない
  POST /api/tenant/staff/revoke        {"staff_id"} → 担当者のapi_keyを失効
  GET  /api/tenant/announcements  公開中のお知らせ一覧(全テナント共通。
                           投稿はannouncements_cli.py、Web管理画面は作らない)
  GET  /api/tenant/activity-log   自テナントの操作履歴(リスト作成・送信開始を
                           時系列でまとめたもの。新規テーブルは持たず、
                           target_lists/campaignsを突き合わせて作る)
  GET  /api/tenant/kill-switch    自テナントの送信が現在止められているかだけを
                           確認する読み取り専用エンドポイント(他テナントの
                           状態や制御権限は渡さない。操作は/api/ops/*のみ)
  GET  /api/tenant/dashboard      β版ダッシュボード。今月の対象企業数・
                           試行数・成功/SKIP/FAILED数、累計成功数、
                           返信/商談化/受注の件数。form_send_log/
                           target_list_membersからの集計のみで、
                           新規の集計テーブルは持たない
  ※ Authorization: Bearer <tenant.api_key または staff.api_key>。テナントIDは
    このキーからサーバ側で解決し、リクエストボディのtenant_idは一切信用しない
    (offers.resolve_tenant_by_key。担当者ごとのキーでもテナント全体のキーでも
    解決されるtenant_idは同じで、見えるデータに差は無い)
  GET  / , /list_builder.html  操作画面(list_builder.html)をこのサーバ自身から配信。
                           別ドメインから配信すると、このAPIがまだ平文HTTPのため
                           ブラウザの混在コンテンツ制限でfetch()がブロックされるための対応

設計の要点:
  - touch_id で「どの接触が効いたか」を紐付ける。これが取れないと学習データにならない
  - touch_id が無い流入も受ける（自然流入・紹介）。company_id で照合し、
    直近の接触があればそれに帰属させる（アトリビューション）
  - webhookは署名検証を必須にする（誰でも paid=1 を送れてしまうため）
  - 冪等: 同じイベントが2回来ても二重計上しない

標準ライブラリのみで動く（本番はFastAPI/Flask + gunicornに載せ替えてよいが、
ロジックは同じなので移植は機械的）。

  python3 api.py serve --port 8787
  python3 api.py test              # 自己テスト（サーバを立てて叩く）
"""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import db
import metrics
import offers
import run as R
import target_lists as TL

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dev-secret-change-me")
LP_URL = os.environ.get("LP_URL", "https://ashibase.jp/sekisan")
# touch_idが無い流入を、直近何日以内の接触に帰属させるか
ATTRIBUTION_WINDOW_DAYS = 45

# Stock Factory等の運用API(/api/ops/*)専用のキー。WEBHOOK_SECRETと違い、
# 実送信(send/followup)まで叩ける強い権限のため既定値を持たせない
# (未設定なら誰にも一致しない=常に401、というフェイルセーフにする)。
SALES_ENGINE_API_KEY = os.environ.get("SALES_ENGINE_API_KEY")

_SEND_PATH_RE = re.compile(r"^/api/tenant/lists/(\d+)/send$")
_OUTCOME_PATH_RE = re.compile(r"^/api/tenant/lists/(\d+)/outcome$")
_SCREENSHOT_PATH_RE = re.compile(r"^/api/tenant/send-log/(\d+)/screenshot$")
_AUTOFILL_QUEUE_PATH_RE = re.compile(r"^/api/tenant/send-log/(\d+)/autofill-queue$")

# list_builder.htmlを同一オリジン(このAPIサーバ自身)から配信する。
# 別ドメイン(例: Vercel/HTTPS)からの配信だと、このAPIが未だ平文HTTPのため
# ブラウザの混在コンテンツ制限でfetch()がブロックされてしまうための対応。
_STATIC_PAGES = {"/list_builder.html": "list_builder.html", "/": "list_builder.html"}
_BASE_DIR = Path(__file__).parent


# ── 冪等性 ──────────────────────────────────
def _once(con, key):
    """同じイベントの二重計上を防ぐ。True=初回"""
    try:
        con.execute("INSERT INTO idempotency (key, created_at) VALUES (?,?)",
                    (key, datetime.now().isoformat(timespec="seconds")))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def cancel_pending_followups(con, company_id, keep_campaign=None):
    """反応があった会社への未送信フォローを取り消す。
    追いかけを止めるのは礼儀であると同時に、無駄な送信コストを削る。
    （テストが「反応済みの会社にStep2が残る」状態を検出したため追加）"""
    q = "DELETE FROM touches WHERE company_id=? AND sent_at IS NULL"
    p = [company_id]
    if keep_campaign:
        q += " AND campaign_id<>?"
        p.append(keep_campaign)
    n = con.execute(q, p).rowcount
    con.commit()
    return n


# ── アトリビューション ─────────────────────
def resolve_touch(con, touch_id=None, company_id=None, email=None):
    """イベントをどの接触に紐付けるか決める。
    1. touch_id があればそれ（最も正確）
    2. なければ company_id から直近の接触を探す（自然流入の帰属）
    3. それも無ければ email のドメインから会社を推定
    どれも当たらなければ None（＝オーガニック流入として別枠で記録）"""
    if touch_id:
        r = con.execute("SELECT id, company_id FROM touches WHERE id=?", (touch_id,)).fetchone()
        if r:
            return r["id"], r["company_id"], "direct"

    if not company_id and email and "@" in email:
        domain = email.split("@")[1].lower()
        r = con.execute("""SELECT id FROM companies
                           WHERE website_url LIKE ? AND dedup_of IS NULL LIMIT 1""",
                        (f"%{domain}%",)).fetchone()
        if r:
            company_id = r["id"]

    if company_id:
        since = (datetime.now() - timedelta(days=ATTRIBUTION_WINDOW_DAYS)).isoformat(timespec="seconds")
        r = con.execute("""SELECT id, company_id FROM touches
                           WHERE company_id=? AND sent_at IS NOT NULL AND sent_at >= ?
                           ORDER BY sent_at DESC LIMIT 1""", (company_id, since)).fetchone()
        if r:
            return r["id"], r["company_id"], "last_touch"
        return None, company_id, "organic"

    return None, None, "unknown"


# ── ハンドラ本体 ────────────────────────────
def h_signup(con, data):
    """LPのフォーム送信。反応と無料登録を同時に立てる。"""
    email = (data.get("email") or "").strip()
    if "@" not in email:
        return 400, {"error": "メールアドレスが不正です"}

    tid, cid, how = resolve_touch(con, data.get("touch_id"), data.get("company_id"), email)
    key = f"signup:{email}"
    if not _once(con, key):
        return 200, {"ok": True, "duplicate": True, "message": "登録済みです"}

    now = datetime.now().isoformat(timespec="seconds")
    if tid:
        con.execute("""UPDATE touches SET responded=1, signed_up=1,
                       responded_at=COALESCE(responded_at,?) WHERE id=?""", (now, tid))
    if cid and email:
        con.execute("UPDATE companies SET email=COALESCE(email,?) WHERE id=?", (email, cid))
    con.commit()
    cancelled = cancel_pending_followups(con, cid) if cid else 0
    return 200, {"ok": True, "touch_id": tid, "company_id": cid, "attribution": how,
                 "cancelled_followups": cancelled}


def h_activate(con, data):
    """積算を1回実行した = 価値に到達した。ここが有料転換の最良の先行指標。"""
    tid, cid, how = resolve_touch(con, data.get("touch_id"), data.get("company_id"),
                                  data.get("email"))
    if not tid:
        return 200, {"ok": True, "attribution": how, "note": "紐付く接触なし"}
    if not _once(con, f"activate:{tid}"):
        return 200, {"ok": True, "duplicate": True}
    con.execute("UPDATE touches SET activated=1 WHERE id=? AND signed_up=1", (tid,))
    con.commit()
    return 200, {"ok": True, "touch_id": tid, "attribution": how}


def h_paid(con, data, signature, raw):
    """課金webhook。署名検証は必須。無いと誰でもpaid=1を送れてしまう。"""
    if not verify_signature(raw, signature):
        return 401, {"error": "署名が不正です"}

    tid, cid, how = resolve_touch(con, data.get("touch_id"), data.get("company_id"),
                                  data.get("email"))
    mrr = int(data.get("mrr_yen") or 0)
    event_id = data.get("event_id") or f"{cid}:{mrr}"
    if not _once(con, f"paid:{event_id}"):
        return 200, {"ok": True, "duplicate": True}

    now = datetime.now().isoformat(timespec="seconds")
    if tid:
        con.execute("""UPDATE touches SET paid=1, mrr_yen=?, paid_at=?,
                       responded=1, signed_up=1, activated=1 WHERE id=?""", (mrr, now, tid))
        con.commit()
        return 200, {"ok": True, "touch_id": tid, "mrr_yen": mrr, "attribution": how}
    return 200, {"ok": True, "attribution": how, "note": "オーガニック課金として記録"}


def h_optout(con, data):
    """配信停止。即座にsuppressionへ入れ、未送信の予定も取り消す。"""
    tid, cid, _ = resolve_touch(con, data.get("touch_id"), data.get("company_id"),
                                data.get("email"))
    if not cid:
        return 404, {"error": "該当する会社が特定できませんでした"}
    db.suppress(con, cid, "optout", source=data.get("source", "web"),
                note=data.get("note"))
    n = con.execute("DELETE FROM touches WHERE company_id=? AND sent_at IS NULL",
                    (cid,)).rowcount
    con.commit()
    return 200, {"ok": True, "company_id": cid, "cancelled": n,
                 "message": "配信を停止しました"}


def h_click(con, touch_id):
    """メール/SMSのリンククリック。反応として記録してLPへ送る。"""
    r = con.execute("SELECT id, company_id FROM touches WHERE id=?", (touch_id,)).fetchone()
    if r and _once(con, f"click:{touch_id}"):
        con.execute("""UPDATE touches SET responded=1, opened=1,
                       responded_at=COALESCE(responded_at,?) WHERE id=?""",
                    (datetime.now().isoformat(timespec="seconds"), touch_id))
        con.commit()
        cancel_pending_followups(con, r["company_id"])
    return f"{LP_URL}?t={touch_id}"


def verify_signature(raw: bytes, signature: str) -> bool:
    if not signature:
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def sign(raw: bytes) -> str:
    """送信側（課金システム）が使う署名生成。テストと本番実装の参照用。"""
    return hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def verify_tenant_bearer(con, auth_header: str):
    """/api/tenant/* 用。Authorization: Bearer <tenant.api_key> からテナント行を
    解決する。運用専用の SALES_ENGINE_API_KEY とは完全に別の認証で、
    このキーはテナント自身のリスト作成・閲覧しかできない(run-step等には使えない)。
    見つからなければNone(=呼び出し側で401にする)。"""
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):]
    return offers.resolve_tenant_by_key(con, token)


# ── 送信先リスト(SaaS販売用) ─────────────────
def h_tenant_lists_preview(con, tenant_id, data):
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        return 400, {"error": "filtersはオブジェクトで指定してください"}
    return 200, TL.preview_filter(con, tenant_id, filters)


def h_tenant_lists_create(con, tenant_id, data):
    name = (data.get("name") or "").strip()
    if not name:
        return 400, {"error": "nameは必須です"}
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        return 400, {"error": "filtersはオブジェクトで指定してください"}
    return 200, TL.create_from_filter(con, tenant_id, name, filters)


def h_tenant_lists_csv(con, tenant_id, data):
    name = (data.get("name") or "").strip()
    csv_text = data.get("csv")
    if not name or not csv_text:
        return 400, {"error": "nameとcsvは必須です"}
    if len(csv_text) > 10_000_000:  # 10MB上限(暴走・誤操作の被害抑制)
        return 400, {"error": "CSVが大きすぎます(上限10MB)"}
    discover_urls = bool(data.get("discover_urls"))
    res = TL.create_from_csv(con, tenant_id, name, csv_text, discover_urls=discover_urls)
    if "error" in res:
        return 400, res
    return 200, res


def h_tenant_lists_list(con, tenant_id):
    return 200, {"lists": TL.list_lists(con, tenant_id)}


def h_tenant_list_detail(con, tenant_id, list_id, qs):
    limit = min(int(qs.get("limit", ["200"])[0]), 1000)
    offset = int(qs.get("offset", ["0"])[0])
    status_filter = qs.get("status", [None])[0]
    res = TL.get_list(con, tenant_id, list_id, limit=limit, offset=offset, status_filter=status_filter)
    if not res:
        return 404, {"error": "リストが見つかりません"}
    return 200, res


def h_tenant_list_member_outcome(con, tenant_id, list_id, data):
    """送信済み企業への返信・商談化・受注を担当者が手動で記録する(β版)。
    メール自動取得等はしない。"""
    company_id = data.get("company_id")
    field = data.get("field")
    value = data.get("value")
    if not isinstance(company_id, int) or field not in ("replied", "deal", "won") \
            or not isinstance(value, bool):
        return 400, {"error": "company_id(整数)・field('replied'|'deal'|'won')・value(真偽値)は必須です"}
    memo = data.get("memo")
    if memo is not None:
        memo = str(memo).strip() or None
    ok = db.set_target_list_member_outcome(con, tenant_id, list_id, company_id, field, value, memo=memo)
    if not ok:
        return 404, {"error": "リストまたは企業が見つかりません"}
    return 200, {"ok": True}


def h_tenant_send_log(con, tenant_id, qs):
    """テナント自身のフォーム自動送信履歴(form_send_log)。他テナント分は
    tenant_id=?で絞り込んでいるため見えない。?company_id=で1社分の履歴
    (何度目のどの結果か、時系列)だけに絞り込める。
    ?q=で会社名の部分一致検索、?status=SUCCESS,FAILED_UNSUPPORTEDのようにカンマ区切り
    で複数の結果ステータスに絞り込める(MIKOMERU同等の検索・結果フィルタ)。
    countsは(company_id/qの絞り込みは反映しつつ)statusでは絞り込む前の内訳件数
    ——一覧上部の集計バッジ用(チェックを外した項目の件数も見えている必要があるため)。"""
    limit = min(int(qs.get("limit", ["100"])[0]), 500)
    offset = int(qs.get("offset", ["0"])[0])
    company_id = qs.get("company_id", [None])[0]
    name_q = (qs.get("q", [""])[0] or "").strip()
    statuses = [s for s in (qs.get("status", [""])[0] or "").split(",") if s]

    base_where = "l.tenant_id=?"
    base_params = [tenant_id]
    if company_id and company_id.isdigit():
        base_where += " AND l.company_id=?"
        base_params.append(int(company_id))
    if name_q:
        base_where += " AND c.name LIKE ?"
        base_params.append(f"%{name_q}%")

    q = f"""SELECT l.id, l.company_id, c.name company_name, l.status, l.reason_code,
            l.started_at, l.finished_at, l.retry_count, l.execution_seconds,
            (l.screenshot_before_path IS NOT NULL) has_screenshot_before,
            (l.screenshot_after_path IS NOT NULL) has_screenshot_after
        FROM form_send_log l LEFT JOIN companies c ON c.id = l.company_id
        WHERE {base_where}"""
    params = list(base_params)
    if statuses:
        q += f" AND l.status IN ({','.join('?' * len(statuses))})"
        params += statuses
    q += " ORDER BY l.id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = con.execute(q, params).fetchall()

    count_rows = con.execute(f"""SELECT l.status, COUNT(*) n FROM form_send_log l
        LEFT JOIN companies c ON c.id = l.company_id WHERE {base_where}
        GROUP BY l.status""", base_params).fetchall()
    counts = {r["status"]: r["n"] for r in count_rows}
    return 200, {"log": [dict(r) for r in rows], "counts": counts}


def h_tenant_send_log_screenshot_path(con, tenant_id, log_id, kind):
    """送信前後スクリーンショットのファイルパスを、テナント本人の記録であることを
    確認した上で返す(他テナントの送信ログは company_id/tenant_id が一致しないため
    見えない=テナント分離)。kindは'before'か'after'のみ受け付ける。"""
    if kind not in ("before", "after"):
        return None
    col = "screenshot_before_path" if kind == "before" else "screenshot_after_path"
    row = con.execute(f"SELECT {col} AS p FROM form_send_log WHERE id=? AND tenant_id=?",
                       (log_id, tenant_id)).fetchone()
    return row["p"] if row else None


_AUTOFILL_QUEUE_TTL_SECONDS = 600   # 古いタブへ誤って入力しないよう10分で失効させる


def h_tenant_send_log_autofill_queue(con, tenant_id, log_id):
    """MIKOMERUの「自動入力(手動送信サポート)」相当。自動送信に失敗した企業について、
    元の送信文章・送信者情報をautofill_queueへ1件だけ置く(テナントにつき常に最新の
    1件のみ。ブックマークレット側がGET /api/tenant/autofill/pendingで取りに来る)。
    件名・本文はform_send_logには残していない(個人情報配慮の方針)ため、
    list_id経由でtarget_lists.campaign_idからtouchesを逆引きして復元する。
    保存済みリスト経由でない送信(list_id無し)は元の文章を復元できないため、
    その旨のエラーだけ返す(手動入力を促す)。"""
    row = con.execute("""SELECT company_id, list_id, contact_url, target_url
        FROM form_send_log WHERE id=? AND tenant_id=?""", (log_id, tenant_id)).fetchone()
    if not row:
        return 404, {"error": "対象の送信ログが見つかりません"}
    url = row["contact_url"] or row["target_url"]
    if not url:
        return 400, {"error": "送信先URLが不明なため自動入力を準備できません"}

    subject, body = None, None
    if row["list_id"]:
        camp = con.execute("SELECT campaign_id FROM target_lists WHERE id=?",
                            (row["list_id"],)).fetchone()
        if camp and camp["campaign_id"]:
            t = con.execute("""SELECT subject, body FROM touches
                WHERE campaign_id=? AND company_id=? ORDER BY id DESC LIMIT 1""",
                (camp["campaign_id"], row["company_id"])).fetchone()
            if t:
                subject, body = t["subject"], t["body"]
    if body is None:
        return 400, {"error": "元の送信文章を復元できませんでした。件名・本文は手動で"
                               "入力してください", "url": url}

    tn = con.execute("""SELECT sender_name, sender_email, sender_address, optout_url
        FROM tenants WHERE id=?""", (tenant_id,)).fetchone()
    sender_name = (tn["sender_name"] if tn else None) or "AshiBase（足場ベース）"
    sender_email = (tn["sender_email"] if tn else None) or "info@ashibase.jp"
    sender_address = (tn["sender_address"] if tn else None) or ""
    optout_url = (tn["optout_url"] if tn else None) or "https://ashibase.jp/optout"
    # senders.FormSender.footer()と同じ形式(実際に送るときに付与される署名)
    full_message = f"{body}\n\n{sender_name} / {sender_email}\n今後のご連絡が不要な場合: {optout_url}"
    values = {"company": sender_name, "name": sender_name, "email": sender_email,
              "phone": "", "address": sender_address, "subject": subject or "",
              "message": full_message, "furigana": "アシベース"}

    con.execute("""INSERT INTO autofill_queue (tenant_id, url, values_json, created_at)
        VALUES (?,?,?,?)
        ON CONFLICT(tenant_id) DO UPDATE SET url=excluded.url,
            values_json=excluded.values_json, created_at=excluded.created_at""",
        (tenant_id, url, json.dumps(values, ensure_ascii=False),
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return 200, {"url": url}


def h_tenant_autofill_pending(con, tenant_id):
    """ブックマークレットが呼ぶ。直前に「自動入力」ボタンで置いた1件を返す。
    古すぎる(10分超)場合は誤爆防止のため404にする(押し忘れて別の日に別のタブで
    使う、といった事故を防ぐ)。"""
    row = con.execute("SELECT url, values_json, created_at FROM autofill_queue WHERE tenant_id=?",
                       (tenant_id,)).fetchone()
    if not row:
        return 404, {"error": "自動入力の準備がありません。自動送信ログ画面の「自動入力」"
                               "ボタンを先に押してください"}
    age = (datetime.now() - datetime.fromisoformat(row["created_at"])).total_seconds()
    if age > _AUTOFILL_QUEUE_TTL_SECONDS:
        return 404, {"error": "自動入力の準備が古すぎます(10分以内に使ってください)。"
                               "もう一度「自動入力」ボタンを押してください"}
    return 200, {"url": row["url"], "values": json.loads(row["values_json"])}


def h_tenant_companies_search(con, tenant_id, qs):
    """送信除外設定で対象企業を探すための簡易検索。共有マスタ+自テナントの
    非公開データのみ(他テナントの非公開企業は検索にも出さない)。"""
    q = (qs.get("q", [""])[0] or "").strip()
    if len(q) < 2:
        return 400, {"error": "2文字以上で検索してください"}
    rows = con.execute("""SELECT id, name, pref FROM companies
        WHERE dedup_of IS NULL AND (owner_tenant_id IS NULL OR owner_tenant_id=?)
          AND name LIKE ? LIMIT 20""", (tenant_id, f"%{q}%")).fetchall()
    return 200, {"companies": [dict(r) for r in rows]}


def h_tenant_exclusions_list(con, tenant_id):
    return 200, {"exclusions": db.list_tenant_exclusions(con, tenant_id)}


def h_tenant_exclusions_add(con, tenant_id, data):
    company_id = data.get("company_id")
    if not isinstance(company_id, int):
        return 400, {"error": "company_idは必須です"}
    if not con.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
        return 404, {"error": "企業が見つかりません"}
    db.exclude_for_tenant(con, tenant_id, company_id, reason=(data.get("reason") or "").strip() or None)
    return 200, {"ok": True}


def h_tenant_exclusions_remove(con, tenant_id, data):
    company_id = data.get("company_id")
    if not isinstance(company_id, int):
        return 400, {"error": "company_idは必須です"}
    db.unexclude_for_tenant(con, tenant_id, company_id)
    return 200, {"ok": True}


def h_tenant_templates_list(con, tenant_id):
    return 200, {"templates": db.list_message_templates(con, tenant_id)}


def h_tenant_templates_add(con, tenant_id, data):
    name = (data.get("name") or "").strip()
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    if not name or not subject or not body:
        return 400, {"error": "name・subject・bodyはすべて必須です"}
    tid = db.add_message_template(con, tenant_id, name, subject, body)
    return 200, {"ok": True, "template_id": tid}


def h_tenant_templates_delete(con, tenant_id, data):
    template_id = data.get("template_id")
    if not isinstance(template_id, int):
        return 400, {"error": "template_idは必須です"}
    if not db.delete_message_template(con, tenant_id, template_id):
        return 404, {"error": "テンプレートが見つかりません"}
    return 200, {"ok": True}


def h_tenant_sender_templates_list(con, tenant_id):
    return 200, {"templates": db.list_sender_templates(con, tenant_id)}


def h_tenant_sender_templates_add(con, tenant_id, data):
    name = (data.get("name") or "").strip()
    sender_name = (data.get("sender_name") or "").strip()
    sender_email = (data.get("sender_email") or "").strip()
    if not name or not sender_name or not sender_email:
        return 400, {"error": "name・sender_name・sender_emailは必須です"}
    # last_name〜postal_codeは任意(姓・名・フリガナ・郵便番号が別欄の問い合わせ
    # フォーム向け)。空文字はNoneに正規化し、未設定として扱う
    def opt(key):
        return (data.get(key) or "").strip() or None
    tid = db.add_sender_template(con, tenant_id, name, sender_name, sender_email,
                                  sender_address=(data.get("sender_address") or "").strip(),
                                  optout_url=opt("optout_url"),
                                  last_name=opt("last_name"), first_name=opt("first_name"),
                                  last_name_kana=opt("last_name_kana"),
                                  first_name_kana=opt("first_name_kana"),
                                  postal_code=opt("postal_code"))
    return 200, {"ok": True, "template_id": tid}


def h_tenant_sender_templates_delete(con, tenant_id, data):
    template_id = data.get("template_id")
    if not isinstance(template_id, int):
        return 400, {"error": "template_idは必須です"}
    if not db.delete_sender_template(con, tenant_id, template_id):
        return 404, {"error": "テンプレートが見つかりません"}
    return 200, {"ok": True}


def h_tenant_sender_templates_activate(con, tenant_id, data):
    template_id = data.get("template_id")
    if not isinstance(template_id, int):
        return 400, {"error": "template_idは必須です"}
    if not db.activate_sender_template(con, tenant_id, template_id):
        return 404, {"error": "テンプレートが見つかりません"}
    return 200, {"ok": True}


def h_tenant_staff_list(con, tenant_id):
    return 200, {"staff": offers.list_staff(con, tenant_id)}


def h_tenant_staff_add(con, tenant_id, data):
    name = (data.get("name") or "").strip()
    if not name:
        return 400, {"error": "nameは必須です"}
    email = (data.get("email") or "").strip() or None
    staff_id, api_key = offers.add_staff(con, tenant_id, name, email=email)
    return 200, {"ok": True, "staff_id": staff_id, "api_key": api_key}


def h_tenant_staff_revoke(con, tenant_id, data):
    staff_id = data.get("staff_id")
    if not isinstance(staff_id, int):
        return 400, {"error": "staff_idは必須です"}
    if not offers.revoke_staff(con, tenant_id, staff_id):
        return 404, {"error": "担当者が見つかりません"}
    return 200, {"ok": True}


def h_tenant_announcements_list(con):
    return 200, {"announcements": db.list_announcements(con, published_only=True)}


def h_tenant_activity_log(con, tenant_id, qs):
    limit = min(int(qs.get("limit", ["100"])[0]), 500)
    return 200, {"log": TL.activity_log(con, tenant_id, limit=limit)}


def h_tenant_dashboard(con, tenant_id):
    """「AI営業社員がどれだけ働いたか」を一目で見せるβ版ダッシュボード。
    既存のform_send_log/target_list_membersから集計するだけで、
    新しい巨大なデータ構造は作らない。"""
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0) \
        .isoformat(timespec="seconds")

    def _count(where, params):
        return con.execute(f"SELECT COUNT(*) FROM form_send_log WHERE tenant_id=? AND {where}",
                            [tenant_id] + params).fetchone()[0]

    targeted = con.execute("""SELECT COUNT(DISTINCT company_id) FROM form_send_log
        WHERE tenant_id=? AND started_at>=?""", (tenant_id, month_start)).fetchone()[0]
    this_month = {
        "targeted_companies": targeted,
        "attempts": _count("started_at>=?", [month_start]),
        "success": _count("started_at>=? AND status='SUCCESS'", [month_start]),
        "skip": _count("started_at>=? AND status LIKE 'SKIP%'", [month_start]),
        "failed": _count("started_at>=? AND status IN ('FAILED_RETRYABLE','FAILED_UNSUPPORTED')",
                          [month_start]),
    }
    all_time_success = _count("status='SUCCESS'", [])

    outcomes = con.execute("""SELECT
            SUM(CASE WHEN m.replied=1 THEN 1 ELSE 0 END) replied,
            SUM(CASE WHEN m.deal=1 THEN 1 ELSE 0 END) deal,
            SUM(CASE WHEN m.won=1 THEN 1 ELSE 0 END) won
        FROM target_list_members m JOIN target_lists tl ON tl.id=m.list_id
        WHERE tl.tenant_id=?""", (tenant_id,)).fetchone()

    return 200, {
        "this_month": this_month,
        "all_time": {"success": all_time_success},
        "outcomes": {"replied": outcomes["replied"] or 0, "deal": outcomes["deal"] or 0,
                     "won": outcomes["won"] or 0},
    }


def h_tenant_list_send(con, tenant_id, list_id, data):
    """保存済みリストから実際にフォーム自動送信キャンペーンを走らせる。
    dry_runは既定でTrue(=実サイトへは何も送らない)。実送信するには
    明示的に dry_run:false を指定する必要がある(取り消せない操作のため)。
    scheduled_at(ISO日時。未来のみ)を指定すると、即時実行せずscheduled_sendsへ
    予約として登録するだけになる(MIKOMERUの「送信開始日時を指定する」相当。
    実際の実行はscheduled_send_cli.pyがcronから拾ってTL.send_list()をそのまま
    呼ぶため、can_contact()・Kill Switch・冪等性等の既存ガードはそのまま効く)。"""
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    if not subject or not body:
        return 400, {"error": "subjectとbodyは必須です"}
    dry_run = data.get("dry_run", True)
    if not isinstance(dry_run, bool):
        dry_run = True

    scheduled_at = (data.get("scheduled_at") or "").strip()
    if scheduled_at:
        try:
            when = datetime.fromisoformat(scheduled_at)
        except ValueError:
            return 400, {"error": "scheduled_atはISO日時形式で指定してください"
                                   "(例: 2026-08-23T09:00:00)"}
        if when <= datetime.now():
            return 400, {"error": "scheduled_atは未来の日時を指定してください"}
        lst = con.execute("SELECT id FROM target_lists WHERE id=? AND tenant_id=?",
                           (list_id, tenant_id)).fetchone()
        if not lst:
            return 404, {"error": "リストが見つかりません"}
        sid = db.create_scheduled_send(con, tenant_id, list_id, subject, body, dry_run,
                                        when.isoformat(timespec="seconds"))
        return 200, {"scheduled": True, "scheduled_id": sid,
                     "scheduled_at": when.isoformat(timespec="seconds")}

    res = TL.send_list(con, tenant_id, list_id, subject, body, dry_run=dry_run)
    if res is None:
        return 404, {"error": "リストが見つかりません"}
    if "error" in res:
        return 400, res
    return 200, res


def h_tenant_scheduled_sends_list(con, tenant_id, qs):
    list_id = qs.get("list_id", [None])[0]
    return 200, {"scheduled": db.list_scheduled_sends(
        con, tenant_id, list_id=int(list_id) if list_id and list_id.isdigit() else None)}


def h_tenant_scheduled_send_cancel(con, tenant_id, data):
    sid = data.get("scheduled_id")
    if not isinstance(sid, int):
        return 400, {"error": "scheduled_idは必須です"}
    if not db.cancel_scheduled_send(con, tenant_id, sid):
        return 404, {"error": "予約が見つからないか、既に実行済み/キャンセル済みです"}
    return 200, {"ok": True}


def verify_ops_bearer(auth_header: str) -> bool:
    """/api/ops/* 用。Authorization: Bearer <SALES_ENGINE_API_KEY> を検証する。
    SALES_ENGINE_API_KEYが未設定なら常にFalse(=常に401)にして、
    デフォルト値での事故(誰でも実送信APIを叩けてしまう)を防ぐ。"""
    if not SALES_ENGINE_API_KEY:
        return False
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    token = auth_header[len("Bearer "):]
    return hmac.compare_digest(token, SALES_ENGINE_API_KEY)


# ── Kill Switch(異常時の即時送信停止。運用専用API) ──
def h_ops_kill_switch_get(con):
    g = con.execute("SELECT stopped, reason, updated_at, updated_by FROM kill_switch WHERE id=1").fetchone()
    return 200, {"global": dict(g) if g else {"stopped": False},
                 "tenants": db.list_tenant_kill_switches(con)}


def h_ops_kill_switch_set(con, data):
    scope = data.get("scope")
    stopped = data.get("stopped")
    if scope not in ("global", "tenant") or not isinstance(stopped, bool):
        return 400, {"error": "scope('global'|'tenant')とstopped(真偽値)は必須です"}
    reason = (data.get("reason") or "").strip() or None
    updated_by = (data.get("updated_by") or "").strip() or None
    if scope == "global":
        db.set_global_kill_switch(con, stopped, reason=reason, updated_by=updated_by)
    else:
        tenant_id = data.get("tenant_id")
        if not isinstance(tenant_id, int):
            return 400, {"error": "scope='tenant'の場合tenant_id(整数)が必須です"}
        db.set_tenant_kill_switch(con, tenant_id, stopped, reason=reason, updated_by=updated_by)
    return 200, {"ok": True}


def h_tenant_kill_switch_status(con, tenant_id):
    """テナント自身が「今、自分の送信が止められているか」だけを確認できる、
    読み取り専用のエンドポイント。他テナントの状態や制御権限は一切渡さない。"""
    stopped, reason = db.kill_switch_status(con, tenant_id=tenant_id)
    return 200, {"stopped": stopped, "reason": reason}


# ── HTTPサーバ ──────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _con(self):
        return db.connect()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        # Authorizationは、自動入力ブックマークレット(送信先企業のドメイン=別オリジン
        # から本APIを叩く)のプリフライトに必要。Content-Type/X-Signatureは既存経路用
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Signature, Authorization")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "JSONが不正です"})

        path = urllib.parse.urlparse(self.path).path

        if path == "/api/ops/run-step":
            if not verify_ops_bearer(self.headers.get("Authorization")):
                return self._json(401, {"error": "unauthorized"})
            con = self._con()
            try:
                step = data.get("step")
                if not step:
                    return self._json(400, {"error": "stepは必須です"})
                res = R.run_op(con, step, campaign_id=data.get("campaignId"),
                               dry_run=bool(data.get("dryRun")))
                return self._json(200 if res.get("ok") else 500, res)
            finally:
                con.close()

        if path == "/api/ops/kill-switch":
            if not verify_ops_bearer(self.headers.get("Authorization")):
                return self._json(401, {"error": "unauthorized"})
            con = self._con()
            try:
                st, res = h_ops_kill_switch_set(con, data)
                return self._json(st, res)
            finally:
                con.close()

        send_match = _SEND_PATH_RE.match(path)
        outcome_match = _OUTCOME_PATH_RE.match(path)
        autofill_match = _AUTOFILL_QUEUE_PATH_RE.match(path)
        if path in ("/api/tenant/lists/preview", "/api/tenant/lists", "/api/tenant/lists/csv") \
                or send_match or outcome_match or autofill_match:
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if path == "/api/tenant/lists/preview":
                    st, res = h_tenant_lists_preview(con, tenant["id"], data)
                elif path == "/api/tenant/lists":
                    st, res = h_tenant_lists_create(con, tenant["id"], data)
                elif path == "/api/tenant/lists/csv":
                    st, res = h_tenant_lists_csv(con, tenant["id"], data)
                elif send_match:
                    st, res = h_tenant_list_send(con, tenant["id"], int(send_match.group(1)), data)
                elif outcome_match:
                    st, res = h_tenant_list_member_outcome(con, tenant["id"],
                                                            int(outcome_match.group(1)), data)
                else:
                    st, res = h_tenant_send_log_autofill_queue(con, tenant["id"],
                                                                int(autofill_match.group(1)))
                return self._json(st, res)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:200]})
            finally:
                con.close()

        if path in ("/api/tenant/exclusions", "/api/tenant/exclusions/remove"):
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if path == "/api/tenant/exclusions":
                    st, res = h_tenant_exclusions_add(con, tenant["id"], data)
                else:
                    st, res = h_tenant_exclusions_remove(con, tenant["id"], data)
                return self._json(st, res)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:200]})
            finally:
                con.close()

        if path == "/api/tenant/scheduled-sends/cancel":
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                st, res = h_tenant_scheduled_send_cancel(con, tenant["id"], data)
                return self._json(st, res)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:200]})
            finally:
                con.close()

        if path in ("/api/tenant/templates", "/api/tenant/templates/delete"):
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if path == "/api/tenant/templates":
                    st, res = h_tenant_templates_add(con, tenant["id"], data)
                else:
                    st, res = h_tenant_templates_delete(con, tenant["id"], data)
                return self._json(st, res)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:200]})
            finally:
                con.close()

        if path in ("/api/tenant/sender-templates", "/api/tenant/sender-templates/delete",
                    "/api/tenant/sender-templates/activate"):
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if path == "/api/tenant/sender-templates":
                    st, res = h_tenant_sender_templates_add(con, tenant["id"], data)
                elif path == "/api/tenant/sender-templates/delete":
                    st, res = h_tenant_sender_templates_delete(con, tenant["id"], data)
                else:
                    st, res = h_tenant_sender_templates_activate(con, tenant["id"], data)
                return self._json(st, res)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:200]})
            finally:
                con.close()

        if path in ("/api/tenant/staff", "/api/tenant/staff/revoke"):
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if path == "/api/tenant/staff":
                    st, res = h_tenant_staff_add(con, tenant["id"], data)
                else:
                    st, res = h_tenant_staff_revoke(con, tenant["id"], data)
                return self._json(st, res)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:200]})
            finally:
                con.close()

        con = self._con()
        try:
            if path == "/api/signup":
                st, res = h_signup(con, data)
            elif path == "/api/activate":
                st, res = h_activate(con, data)
            elif path == "/api/paid":
                st, res = h_paid(con, data, self.headers.get("X-Signature"), raw)
            elif path == "/api/optout":
                st, res = h_optout(con, data)
            else:
                st, res = 404, {"error": "not found"}
        except Exception as e:  # noqa: BLE001
            st, res = 500, {"error": str(e)[:200]}
        finally:
            con.close()
        self._json(st, res)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)

        if u.path in _STATIC_PAGES:
            f = _BASE_DIR / _STATIC_PAGES[u.path]
            body = f.read_bytes() if f.exists() else b"not found"
            self.send_response(200 if f.exists() else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)

        shot_match = _SCREENSHOT_PATH_RE.match(u.path)
        if shot_match:
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                kind = qs.get("kind", [None])[0]
                path_str = h_tenant_send_log_screenshot_path(con, tenant["id"], int(shot_match.group(1)), kind)
                if not path_str:
                    return self._json(404, {"error": "not found"})
                p = Path(path_str)
                if not p.is_file():
                    return self._json(404, {"error": "not found"})
                body = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            finally:
                con.close()

        if u.path in ("/api/ops/status", "/api/ops/metrics", "/api/ops/kill-switch"):
            if not verify_ops_bearer(self.headers.get("Authorization")):
                return self._json(401, {"error": "unauthorized"})
            con = self._con()
            try:
                if u.path == "/api/ops/status":
                    return self._json(200, R.status_dict(con))
                if u.path == "/api/ops/kill-switch":
                    st, res = h_ops_kill_switch_get(con)
                    return self._json(st, res)
                campaign = qs.get("campaignId", [None])[0]
                return self._json(200, metrics.compute(con, int(campaign) if campaign else None))
            finally:
                con.close()

        if (u.path == "/api/tenant/lists" or u.path.startswith("/api/tenant/lists/")
                or u.path == "/api/tenant/send-log"
                or u.path == "/api/tenant/autofill/pending"
                or u.path == "/api/tenant/scheduled-sends"
                or u.path == "/api/tenant/exclusions"
                or u.path == "/api/tenant/companies/search"
                or u.path == "/api/tenant/templates"
                or u.path == "/api/tenant/sender-templates"
                or u.path == "/api/tenant/staff"
                or u.path == "/api/tenant/announcements"
                or u.path == "/api/tenant/activity-log"
                or u.path == "/api/tenant/kill-switch"
                or u.path == "/api/tenant/dashboard"):
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if u.path == "/api/tenant/send-log":
                    st, res = h_tenant_send_log(con, tenant["id"], qs)
                elif u.path == "/api/tenant/autofill/pending":
                    st, res = h_tenant_autofill_pending(con, tenant["id"])
                elif u.path == "/api/tenant/scheduled-sends":
                    st, res = h_tenant_scheduled_sends_list(con, tenant["id"], qs)
                elif u.path == "/api/tenant/exclusions":
                    st, res = h_tenant_exclusions_list(con, tenant["id"])
                elif u.path == "/api/tenant/companies/search":
                    st, res = h_tenant_companies_search(con, tenant["id"], qs)
                elif u.path == "/api/tenant/templates":
                    st, res = h_tenant_templates_list(con, tenant["id"])
                elif u.path == "/api/tenant/sender-templates":
                    st, res = h_tenant_sender_templates_list(con, tenant["id"])
                elif u.path == "/api/tenant/staff":
                    st, res = h_tenant_staff_list(con, tenant["id"])
                elif u.path == "/api/tenant/announcements":
                    st, res = h_tenant_announcements_list(con)
                elif u.path == "/api/tenant/activity-log":
                    st, res = h_tenant_activity_log(con, tenant["id"], qs)
                elif u.path == "/api/tenant/kill-switch":
                    st, res = h_tenant_kill_switch_status(con, tenant["id"])
                elif u.path == "/api/tenant/dashboard":
                    st, res = h_tenant_dashboard(con, tenant["id"])
                elif u.path == "/api/tenant/lists":
                    st, res = h_tenant_lists_list(con, tenant["id"])
                else:
                    list_id_str = u.path[len("/api/tenant/lists/"):]
                    if not list_id_str.isdigit():
                        st, res = 404, {"error": "not found"}
                    else:
                        st, res = h_tenant_list_detail(con, tenant["id"], int(list_id_str), qs)
                return self._json(st, res)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:200]})
            finally:
                con.close()

        con = self._con()
        try:
            if u.path == "/health":
                n = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
                return self._json(200, {"ok": True, "companies": n,
                                        "at": datetime.now().isoformat(timespec="seconds")})
            if u.path.startswith("/t/"):
                tid = u.path.split("/t/")[1]
                url = h_click(con, tid)
                self.send_response(302)
                self.send_header("Location", url)
                self.end_headers()
                return
            if u.path == "/api/optout":
                st, res = h_optout(con, {k: v[0] for k, v in qs.items()})
                return self._json(st, res)
            self._json(404, {"error": "not found"})
        finally:
            con.close()

    def log_message(self, *args):
        pass   # アクセスログは抑制（本番はリバースプロキシ側で取る）


def serve(port=8787):
    # 起動時に1回だけスキーマを作成/更新する（migrate()は冪等）。これが無いと、真新しい
    # DBファイルで起動した直後は「no such table: companies」等で全リクエストが失敗する
    # （Stock Factory連携の疎通確認で発見）。
    con = db.connect(); db.migrate(con); con.commit()

    # 0.0.0.0 でListenする（従来は127.0.0.1限定）。外部公開の制御は
    # docker-compose.yml の `ports: ["127.0.0.1:8787:8787"]`（ホストの127.0.0.1のみに
    # 公開）が担っているため、コンテナ内部まで127.0.0.1限定にする必要はない——限定した
    # ままだと、Docker経由の外部（ホスト上の他プロセス・同一ホスト上の他コンテナ）からの
    # 接続がコンテナのloopbackに届かず、reset扱いになる（Stock Factory連携の疎通確認で
    # 発見）。self_test()（ローカル専用の自己テスト）は127.0.0.1のままでよい。
    srv = HTTPServer(("0.0.0.0", port), Handler)
    print(f"APIサーバ起動: http://0.0.0.0:{port}")
    print("  POST /api/signup /api/activate /api/paid /api/optout")
    print("  GET  /t/<touch_id>  /health")
    print("  Stock Factory連携: GET /api/ops/status /api/ops/metrics  "
          "POST /api/ops/run-step  (要 SALES_ENGINE_API_KEY)")
    print("  送信先リスト(SaaS): /api/tenant/lists*  (要 テナントごとのapi_key)")
    if not SALES_ENGINE_API_KEY:
        print("  ⚠ SALES_ENGINE_API_KEY 未設定のため /api/ops/* は常に401を返します")
    srv.serve_forever()


# ── 自己テスト ──────────────────────────────
def self_test(port=8899):
    import urllib.request

    con = db.connect(); db.migrate(con)
    # テスト用の接触を1件用意
    con.execute("DELETE FROM idempotency WHERE key LIKE '%test-api%'")
    row = con.execute("""SELECT t.id, t.company_id FROM touches t
                         WHERE t.sent_at IS NOT NULL AND t.paid=0 LIMIT 1""").fetchone()
    if not row:
        print("テスト対象の接触がありません。run.py all --demo を先に実行してください")
        return
    tid, cid = row["id"], row["company_id"]
    con.execute("""UPDATE touches SET responded=0, signed_up=0, activated=0, paid=0,
                   mrr_yen=0 WHERE id=?""", (tid,))
    con.execute("DELETE FROM suppression WHERE company_id=?", (cid,))
    con.commit()

    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def post(path, obj, sig=None):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        req = urllib.request.Request(base + path, data=raw,
                                     headers={"Content-Type": "application/json",
                                              **({"X-Signature": sig} if sig else {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None   # リダイレクトを追わない（外部URLへ出てしまうため）

    def get(path, follow=True):
        opener = urllib.request.build_opener() if follow else \
                 urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(base + path) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, (e.headers.get("Location") or "").encode()

    ok = []
    def t(name, cond, extra=""):
        ok.append(cond)
        print(f"  {'✓' if cond else '✗'} {name}" + (f"  {extra}" if extra and not cond else ""))

    print("── 死活監視 ──")
    st, _ = get("/health"); t("GET /health", st == 200)

    print("\n── 無料登録（LPフォーム） ──")
    st, r = post("/api/signup", {"email": "test-api@example.co.jp", "touch_id": tid})
    t("POST /api/signup", st == 200 and r.get("ok"))
    t("接触に反応と登録が記録される",
      con.execute("SELECT responded+signed_up FROM touches WHERE id=?", (tid,)).fetchone()[0] == 2)
    t("アトリビューションがdirect", r.get("attribution") == "direct")
    t("反応した会社への未送信フォローが取り消される",
      con.execute("""SELECT COUNT(*) FROM touches WHERE company_id=? AND sent_at IS NULL""",
                  (cid,)).fetchone()[0] == 0)
    st, r2 = post("/api/signup", {"email": "test-api@example.co.jp", "touch_id": tid})
    t("同じ登録は二重計上しない", r2.get("duplicate") is True)
    st, r3 = post("/api/signup", {"email": "こわれた"})
    t("不正なメールアドレスを弾く", st == 400)

    print("\n── 積算実行（価値到達） ──")
    st, r = post("/api/activate", {"touch_id": tid})
    t("POST /api/activate", st == 200)
    t("activatedが立つ",
      con.execute("SELECT activated FROM touches WHERE id=?", (tid,)).fetchone()[0] == 1)

    print("\n── 課金webhook ──")
    payload = {"touch_id": tid, "mrr_yen": 14800, "event_id": "test-api-evt-1"}
    raw = json.dumps(payload, ensure_ascii=False).encode()
    st, r = post("/api/paid", payload, sig="wrong-signature")
    t("署名が無いと401で拒否する", st == 401)
    st, r = post("/api/paid", payload, sig=sign(raw))
    t("正しい署名なら受け付ける", st == 200 and r.get("ok"))
    t("paidとMRRが記録される",
      con.execute("SELECT paid, mrr_yen FROM touches WHERE id=?", (tid,)).fetchone()[1] == 14800)
    st, r = post("/api/paid", payload, sig=sign(raw))
    t("同じ課金イベントを二重計上しない", r.get("duplicate") is True)

    print("\n── クリック計測 ──")
    st, loc = get(f"/t/{tid}", follow=False)
    t("GET /t/<id> がLPへリダイレクト", st == 302 and f"t={tid}".encode() in loc,
      f"status={st} location={loc[:60]}")
    t("クリックが反応として記録される",
      con.execute("SELECT opened FROM touches WHERE id=?", (tid,)).fetchone()[0] == 1)

    print("\n── 配信停止 ──")
    before = con.execute("SELECT COUNT(*) FROM touches WHERE company_id=? AND sent_at IS NULL",
                         (cid,)).fetchone()[0]
    st, r = post("/api/optout", {"company_id": cid})
    t("POST /api/optout", st == 200 and r.get("ok"))
    t("suppressionに登録される",
      con.execute("SELECT COUNT(*) FROM suppression WHERE company_id=?", (cid,)).fetchone()[0] == 1)
    t("未送信の予定が取り消される",
      con.execute("SELECT COUNT(*) FROM touches WHERE company_id=? AND sent_at IS NULL",
                  (cid,)).fetchone()[0] == 0, f"取消前{before}件")
    allowed, why = db.can_contact(con, cid)
    t("以後 can_contact が拒否する", (not allowed) and "配信停止" in why)

    print("\n── Stock Factory連携（運用API） ──")
    global SALES_ENGINE_API_KEY
    import uuid as _uuid
    SALES_ENGINE_API_KEY = "test-ops-" + _uuid.uuid4().hex
    ops_key = SALES_ENGINE_API_KEY

    def get_auth(path, token=None):
        req = urllib.request.Request(
            base + path, headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def post_auth(path, obj, token=None):
        praw = json.dumps(obj, ensure_ascii=False).encode()
        req = urllib.request.Request(
            base + path, data=praw,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    st, r = get_auth("/api/ops/status")
    t("認証ヘッダなしのGET /api/ops/statusは401", st == 401)
    st, r = get_auth("/api/ops/status", token="wrong-key")
    t("誤ったキーは401", st == 401)
    st, r = get_auth("/api/ops/status", token=ops_key)
    t("正しいキーでGET /api/ops/status", st == 200 and "companies_total" in r)
    st, r = get_auth("/api/ops/metrics", token=ops_key)
    t("GET /api/ops/metrics", st == 200 and "overall" in r)
    st, r = post_auth("/api/ops/run-step", {"step": "dedup"})
    t("POST /api/ops/run-stepも認証必須(401)", st == 401)
    st, r = post_auth("/api/ops/run-step", {"step": "dedup"}, token=ops_key)
    t("POST /api/ops/run-step: dedup", st == 200 and r.get("ok"))

    print("\n── 送信先リスト(SaaS販売用テナントAPI) ──")
    import offers as OF
    con.execute("DELETE FROM tenants WHERE name LIKE 'test-tenant-%'")
    con.commit()
    tid_a, key_a = OF.add_tenant(con, "test-tenant-A", "a@example.co.jp")
    tid_b, key_b = OF.add_tenant(con, "test-tenant-B", "b@example.co.jp")

    st, r = post_auth("/api/tenant/lists/preview", {"filters": {"prefs": ["東京都"]}})
    t("認証ヘッダなしのPOST /api/tenant/lists/previewは401", st == 401)
    st, r = post_auth("/api/tenant/lists/preview", {"filters": {"prefs": ["東京都"]}}, token=key_a)
    t("正しいキーでプレビュー取得", st == 200 and "count" in r)

    st, r = post_auth("/api/tenant/lists", {"name": "テストA_東京都", "filters": {"prefs": ["東京都"]}},
                      token=key_a)
    t("POST /api/tenant/lists: フィルタ型リスト作成", st == 200 and bool(r.get("list_id")))
    list_a_id = r.get("list_id")

    csv_text = "会社名,都道府県\nテナントB専用企業,福岡県\n"
    st, r = post_auth("/api/tenant/lists/csv", {"name": "B持込リスト", "csv": csv_text}, token=key_b)
    t("POST /api/tenant/lists/csv: CSV取込", st == 200 and r.get("new_companies", 0) >= 1)
    list_b_id = r.get("list_id")

    print("\n── CSV検索(URLで検索。MIKOMERU同等) ──")
    import form_navigator as _fn_mod
    orig_discover = _fn_mod.discover_contact_url
    _fn_mod.discover_contact_url = lambda url, **kw: (
        {"status": "FOUND", "contact_url": url + "/contact", "error": None} if "good" in url
        else {"status": "NO_FORM", "contact_url": None, "error": "form_not_found"})
    try:
        url_csv = ("会社名,都道府県,url\n"
                   "URL検索良い会社,東京都,https://good-url-test.example.co.jp\n"
                   "URL検索悪い会社,東京都,https://bad-url-test.example.co.jp\n")
        st, r = post_auth("/api/tenant/lists/csv",
                          {"name": "URL検索テスト", "csv": url_csv, "discover_urls": True}, token=key_a)
        t("discover_urls:trueでurl_discoveryの内訳が返る",
          st == 200 and r.get("url_discovery", {}).get("found") == 1
          and r["url_discovery"]["no_form"] == 1)
        url_list_id = r.get("list_id")
        good_contact = con.execute("""SELECT contact_url FROM companies c
            JOIN target_list_members m ON m.company_id=c.id
            WHERE m.list_id=? AND c.name='URL検索良い会社'""", (url_list_id,)).fetchone()["contact_url"]
        t("見つかった問い合わせページがcontact_urlに反映される",
          good_contact == "https://good-url-test.example.co.jp/contact")

        st, r = post_auth("/api/tenant/lists/csv", {"name": "URL検索未指定", "csv": url_csv}, token=key_a)
        t("discover_urls未指定(既定false)ならurl_discoveryは返らない",
          st == 200 and "url_discovery" not in r)

        con.execute("DELETE FROM target_list_members WHERE list_id IN (?,?)",
                    (url_list_id, r.get("list_id")))
        con.execute("DELETE FROM target_lists WHERE id IN (?,?)", (url_list_id, r.get("list_id")))
        con.execute("""DELETE FROM companies WHERE owner_tenant_id=? AND
            name IN ('URL検索良い会社','URL検索悪い会社')""", (tid_a,))
        con.commit()
    finally:
        _fn_mod.discover_contact_url = orig_discover

    st, r = get_auth("/api/tenant/lists", token=key_a)
    t("GET /api/tenant/lists は自テナント分のみ返す",
      st == 200 and all(l["id"] != list_b_id for l in r.get("lists", [])))

    st, r = get_auth(f"/api/tenant/lists/{list_a_id}", token=key_a)
    t("GET /api/tenant/lists/<id> で自分のリストは見える", st == 200 and "members" in r)
    t("企業ごとのsend_status(初期値PENDING)が含まれる",
      len(r.get("members", [])) > 0 and all(m["send_status"] == "PENDING" for m in r["members"]))

    st, r = get_auth(f"/api/tenant/lists/{list_a_id}?status=pending", token=key_a)
    t("?status=pendingで絞り込める(未送信のみ)",
      st == 200 and len(r.get("members", [])) > 0)
    st, r = get_auth(f"/api/tenant/lists/{list_a_id}?status=success", token=key_a)
    t("?status=successで絞り込める(まだ0件のはず)",
      st == 200 and len(r.get("members", [])) == 0)

    outcome_company_id = con.execute("""SELECT c.id FROM target_list_members m
        JOIN companies c ON c.id=m.company_id WHERE m.list_id=? LIMIT 1""", (list_a_id,)).fetchone()[0]
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/outcome",
                      {"company_id": outcome_company_id, "field": "replied", "value": True,
                       "memo": "電話で反応あり"}, token=key_a)
    t("POST /api/tenant/lists/<id>/outcomeで返信を記録できる", st == 200 and r.get("ok"))
    st, r = get_auth(f"/api/tenant/lists/{list_a_id}?status=replied", token=key_a)
    t("記録した返信が?status=repliedで拾える",
      st == 200 and any(m["id"] == outcome_company_id for m in r.get("members", [])))
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/outcome",
                      {"company_id": outcome_company_id, "field": "invalid", "value": True}, token=key_a)
    t("不正なfieldは400", st == 400)
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/outcome",
                      {"company_id": outcome_company_id, "field": "deal", "value": True}, token=key_b)
    t("他テナントのリストへの記録は404(横断更新できない)", st == 404)

    st, r = get_auth(f"/api/tenant/lists/{list_b_id}", token=key_a)
    t("他テナントのリストIDを指定しても404(横断閲覧できない)", st == 404)

    st, r = get_auth(f"/api/tenant/lists/{list_a_id}", token=key_b)
    t("逆方向も同様に404", st == 404)

    st, r = post_auth("/api/tenant/lists/preview", {"filters": {"prefs": ["福岡県"]}}, token=key_a)
    t("他テナントがCSVで持ち込んだ非公開企業はフィルタにも出てこない",
      st == 200 and not any(s["name"] == "テナントB専用企業" for s in r.get("sample", [])))

    print("\n── 送信先リストからの送信(dry_run) ──")
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/send", {"body": "本文のみ"}, token=key_a)
    t("subjectが無いと400", st == 400)
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/send",
                      {"subject": "テスト件名", "body": "テスト本文"}, token=key_a)
    t("POST /api/tenant/lists/<id>/send はdry_run既定でtrue",
      st == 200 and r.get("dry_run") is True and "stats" in r)
    send_campaign_id = r.get("campaign_id")
    t("キャンペーンが実際に作られている",
      send_campaign_id and con.execute(
          "SELECT COUNT(*) FROM campaigns WHERE id=?", (send_campaign_id,)).fetchone()[0] == 1)
    t("dry_run送信ではtarget_list_membersのsend_statusはPENDINGのまま(反映しない)",
      all(m["send_status"] == "PENDING" for m in con.execute(
          "SELECT send_status FROM target_list_members WHERE list_id=?", (list_a_id,)).fetchall()))

    # Kill Switchは既定で全体停止中(db.migrate()の安全側デフォルト。ここまでの
    # テストで誰も解除していない)。dry_run=falseで送っても実チャネルには一切
    # 触れずKill Switchで中止されることと、その結果がtarget_list_membersへ
    # 正しく同期される(STOPPED)ことを確認する
    t("dry_runで「送信」扱いだった分は実送信扱い(SUCCESS)に誤変換されない"
      "(sent_atはdry_run/実送信を問わず同じ形で立つため、provider_id=mock_で判別している)",
      all(row["send_status"] != "SUCCESS" for row in con.execute(
          "SELECT send_status FROM target_list_members WHERE list_id=?", (list_a_id,)).fetchall()))

    # 単独の小さなリストで、Kill Switch停止時の同期(STOPPED)をクリーンな状態で検証する
    # (list_aは既にdry_run分の送信履歴で埋まっており、can_contact()の判定が絡んで
    # Kill Switchまで到達しない行が混ざるため、別途まっさらな企業で確認する)
    clean_company = None
    for row in con.execute("SELECT id FROM companies WHERE contact_url IS NOT NULL AND dedup_of IS NULL"):
        if db.can_contact(con, row["id"])[0]:
            clean_company = row["id"]
            break
    if clean_company:
        now_ks = datetime.now().isoformat(timespec="seconds")
        cur = con.execute("""INSERT INTO target_lists (tenant_id,name,source,company_count,created_at)
            VALUES (?,?,?,?,?)""", (tid_a, "テストA_KS単体", "filter", 1, now_ks))
        ks_list_id = cur.lastrowid
        con.execute("""INSERT INTO target_list_members (list_id, company_id, send_status,
            created_at, updated_at) VALUES (?,?,'PENDING',?,?)""",
            (ks_list_id, clean_company, now_ks, now_ks))
        con.commit()
        res = TL.send_list(con, tid_a, ks_list_id, "件名", "本文", dry_run=False)
        t("単体テストでもKill Switch停止中は実送信されない",
          res is not None and "error" not in res and res["stats"]["sent"] == 0)
        ks_status = con.execute("SELECT send_status FROM target_list_members WHERE list_id=? AND company_id=?",
                                 (ks_list_id, clean_company)).fetchone()["send_status"]
        t("Kill Switchで止まった結果がtarget_list_membersにSTOPPEDとして同期される",
          ks_status == "STOPPED", f"status={ks_status}")
        con.execute("DELETE FROM touches WHERE campaign_id=(SELECT campaign_id FROM target_lists WHERE id=?)",
                     (ks_list_id,))
        con.execute("DELETE FROM campaigns WHERE id=(SELECT campaign_id FROM target_lists WHERE id=?)",
                     (ks_list_id,))
        con.execute("DELETE FROM target_list_members WHERE list_id=?", (ks_list_id,))
        con.execute("DELETE FROM target_lists WHERE id=?", (ks_list_id,))
        con.commit()
    else:
        t("Kill Switch単体テスト", False, "適切な企業が見つからずスキップ")

    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/send",
                      {"subject": "2回目", "body": "2回目"}, token=key_a)
    t("同じリストへの再送信は同じcampaign_idを使い回す(二重送信防止)",
      r.get("campaign_id") == send_campaign_id)

    st, r = post_auth(f"/api/tenant/lists/{list_b_id}/send",
                      {"subject": "x", "body": "y"}, token=key_a)
    t("他テナントのリストへは送信できない(404)", st == 404)

    print("\n── 予約送信(MIKOMERUの「送信開始日時を指定する」相当) ──")
    past_at = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/send",
                      {"subject": "予約テスト", "body": "本文", "scheduled_at": past_at}, token=key_a)
    t("過去の日時は400", st == 400)
    r_bad_fmt = post_auth(f"/api/tenant/lists/{list_a_id}/send",
                          {"subject": "x", "body": "y", "scheduled_at": "not-a-date"}, token=key_a)
    t("不正な日時形式は400", r_bad_fmt[0] == 400)

    future_at = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/send",
                      {"subject": "予約テスト", "body": "本文", "scheduled_at": future_at}, token=key_a)
    t("未来の日時なら予約登録され、即時送信はされない",
      st == 200 and r.get("scheduled") is True and isinstance(r.get("scheduled_id"), int))
    scheduled_id = r.get("scheduled_id")

    st, r = post_auth(f"/api/tenant/lists/{list_b_id}/send",
                      {"subject": "x", "body": "y", "scheduled_at": future_at}, token=key_a)
    t("他テナントのリストへは予約もできない(404)", st == 404)

    st, r = get_auth("/api/tenant/scheduled-sends")
    t("認証ヘッダなしのGET /api/tenant/scheduled-sendsは401", st == 401)
    st, r = get_auth("/api/tenant/scheduled-sends", token=key_a)
    t("予約一覧に登録した予約が出る",
      st == 200 and any(s["id"] == scheduled_id and s["status"] == "PENDING"
                         for s in r.get("scheduled", [])))
    st, r = get_auth("/api/tenant/scheduled-sends", token=key_b)
    t("他テナントの予約は見えない(テナント分離)",
      st == 200 and all(s["id"] != scheduled_id for s in r.get("scheduled", [])))

    st, r = post_auth("/api/tenant/scheduled-sends/cancel", {"scheduled_id": scheduled_id}, token=key_b)
    t("他テナントは自分の予約としてキャンセルできない(404)", st == 404)
    st, r = post_auth("/api/tenant/scheduled-sends/cancel", {"scheduled_id": scheduled_id}, token=key_a)
    t("キャンセルできる", st == 200 and r.get("ok"))
    st, r = post_auth("/api/tenant/scheduled-sends/cancel", {"scheduled_id": scheduled_id}, token=key_a)
    t("既にキャンセル済みの予約を再キャンセルしようとすると404", st == 404)

    # due_scheduled_sends()の期限判定を直接確認(cron側の抽出ロジック)
    due_at = (datetime.now() + timedelta(seconds=1)).isoformat(timespec="seconds")
    sid2 = db.create_scheduled_send(con, tid_a, list_a_id, "件名", "本文", True, due_at)
    t("scheduled_atがまだ先ならdue_scheduled_sends()には出てこない",
      all(s["id"] != sid2 for s in db.due_scheduled_sends(con, datetime.now().isoformat(timespec="seconds"))))
    t("scheduled_atを過ぎるとdue_scheduled_sends()に出てくる",
      any(s["id"] == sid2 for s in db.due_scheduled_sends(
          con, (datetime.now() + timedelta(seconds=2)).isoformat(timespec="seconds"))))
    db.finish_scheduled_send(con, sid2, "DONE", {"sent": 1})
    t("finish_scheduled_send()後はdue_scheduled_sends()に出てこない(PENDINGでなくなるため)",
      all(s["id"] != sid2 for s in db.due_scheduled_sends(
          con, (datetime.now() + timedelta(seconds=2)).isoformat(timespec="seconds"))))
    con.execute("DELETE FROM scheduled_sends WHERE tenant_id IN (?,?)", (tid_a, tid_b))
    con.commit()

    print("\n── リスト送信の同時リクエストでの競合(campaign_idの二重生成防止) ──")
    # 同じリストへ2つの同時送信リクエスト(ボタン連打・2人の担当者)が来ても、
    # 別々のcampaignが二重に作られて二重送信にならないことを確認する。
    st, r = post_auth("/api/tenant/lists", {"name": "テストA_競合検証", "filters": {"prefs": ["東京都"]}},
                      token=key_a)
    race_list_id = r.get("list_id")
    race_results = []

    def _send_race_worker():
        con_t = db.connect()  # 別スレッド=別HTTPリクエストを模す
        race_results.append(TL.send_list(con_t, tid_a, race_list_id, "件名", "本文", dry_run=True))
        con_t.close()

    threads = [threading.Thread(target=_send_race_worker) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    campaign_ids_used = {r["campaign_id"] for r in race_results if r}
    t("3スレッド同時送信でも採用されるcampaign_idは1つだけ", len(campaign_ids_used) == 1,
      f"campaign_ids={campaign_ids_used}")
    if campaign_ids_used:
        won_cid = next(iter(campaign_ids_used))
        touch_count = con.execute("SELECT COUNT(*) FROM touches WHERE campaign_id=?",
                                   (won_cid,)).fetchone()[0]
        t("採用されたcampaignのtouchesは重複していない(リスト企業数と一致)",
          touch_count == race_results[0]["target_count"], f"touches={touch_count}")
    # このテストで作られたcampaign(勝者・敗者とも)を名前で特定して片付ける。
    # campaigns.idが正しい列名(campaign_idという列は存在しない。以前ここで
    # 誤って"campaign_id"を指定し、SQLiteが外側touches.campaign_idへの相関参照と
    # 解釈してtouches全件を消してしまった事故があったため、必ずcampaigns.idを使うこと)
    race_cids = [row["id"] for row in con.execute(
        "SELECT id FROM campaigns WHERE name=?", ("[リスト送信] テストA_競合検証",)).fetchall()]
    for cid_ in race_cids:
        con.execute("DELETE FROM touches WHERE campaign_id=?", (cid_,))
        con.execute("DELETE FROM campaigns WHERE id=?", (cid_,))
    con.execute("DELETE FROM target_list_members WHERE list_id=?", (race_list_id,))
    con.execute("DELETE FROM target_lists WHERE id=?", (race_list_id,))
    con.commit()

    print("\n── その他ログ(活動履歴) ──")
    st, r = get_auth("/api/tenant/activity-log")
    t("認証ヘッダなしのGET /api/tenant/activity-logは401", st == 401)
    st, r = get_auth("/api/tenant/activity-log", token=key_a)
    t("GET /api/tenant/activity-logにリスト作成イベントが出る",
      st == 200 and any(e["type"] == "list_created" and "テストA_東京都" in e["detail"]
                         for e in r.get("log", [])))
    t("送信イベントも出る(初回送信時刻)",
      any(e["type"] == "list_sent" and "テストA_東京都" in e["detail"] for e in r.get("log", [])))
    st, r = get_auth("/api/tenant/activity-log", token=key_b)
    t("他テナントのリスト作成イベントは見えない(テナント分離)",
      st == 200 and all("テストA_東京都" not in e["detail"] for e in r.get("log", [])))

    print("\n── 自テナント向けKill Switch状態確認 ──")
    st, r = get_auth("/api/tenant/kill-switch")
    t("認証ヘッダなしのGET /api/tenant/kill-switchは401", st == 401)
    st, r = get_auth("/api/tenant/kill-switch", token=key_a)
    t("GET /api/tenant/kill-switchで自テナントの状態が取れる", st == 200 and "stopped" in r)

    print("\n── 自動送信ログ ──")
    # dry_runはform_send_logへ書かない(Playwrightに触れないため)ので、
    # ログ表示自体の検証用に1件だけ手で入れる。送信前画像も併せて検証する
    # (MIKOMERU同等の目視確認機能。送信後画像は無い状態=NULLのケースとして残す)。
    import tempfile as _tempfile
    shot_dir = Path(_tempfile.mkdtemp(prefix="eigyouai-test-shots-"))
    shot_path = shot_dir / "test_before.png"
    shot_path.write_bytes(b"\x89PNG\r\n\x1a\ntest-image-bytes")
    con.execute("""INSERT INTO form_send_log (company_id, tenant_id, target_url, started_at,
        status, reason_code, screenshot_before_path)
        VALUES (1, ?, 'https://example.co.jp', ?, 'SUCCESS', 'success_text_matched', ?)""",
        (tid_a, datetime.now().isoformat(timespec="seconds"), str(shot_path)))
    con.commit()
    log_id = con.execute("SELECT id FROM form_send_log WHERE tenant_id=? ORDER BY id DESC LIMIT 1",
                          (tid_a,)).fetchone()[0]
    st, r = get_auth("/api/tenant/send-log")
    t("認証ヘッダなしのGET /api/tenant/send-logは401", st == 401)
    st, r = get_auth("/api/tenant/send-log", token=key_a)
    t("自テナントの送信ログが取れる",
      st == 200 and len(r.get("log", [])) == 1 and r["log"][0]["status"] == "SUCCESS")
    t("has_screenshot_before/afterが正しい", r["log"][0]["has_screenshot_before"] == 1
      and r["log"][0]["has_screenshot_after"] == 0)
    st, r = get_auth("/api/tenant/send-log?company_id=1", token=key_a)
    t("?company_id=で1社分に絞り込める", st == 200 and len(r.get("log", [])) == 1)
    st, r = get_auth("/api/tenant/send-log?company_id=999999999", token=key_a)
    t("該当しない企業IDでは0件になる", st == 200 and len(r.get("log", [])) == 0)
    st, r = get_auth("/api/tenant/send-log", token=key_b)
    t("他テナントのログは見えない", st == 200 and len(r.get("log", [])) == 0)

    print("\n── 自動送信ログの検索・結果フィルタ・集計 ──")
    other_cid = con.execute("SELECT id FROM companies WHERE id != 1 LIMIT 1").fetchone()[0]
    con.execute("""INSERT INTO form_send_log (company_id, tenant_id, target_url, started_at,
        status, reason_code) VALUES (?, ?, 'https://example.co.jp', ?, 'FAILED_UNSUPPORTED',
        'success_not_confirmed')""",
        (other_cid, tid_a, datetime.now().isoformat(timespec="seconds")))
    con.commit()
    st, r = get_auth("/api/tenant/send-log", token=key_a)
    t("countsに両方のステータスの内訳が出る",
      r["counts"].get("SUCCESS") == 1 and r["counts"].get("FAILED_UNSUPPORTED") == 1)
    st, r = get_auth("/api/tenant/send-log?status=SUCCESS", token=key_a)
    t("?status=で1種類だけに絞り込める",
      len(r["log"]) == 1 and r["log"][0]["status"] == "SUCCESS")
    t("絞り込んでもcountsは全体の内訳のまま(バッジ表示用)",
      r["counts"].get("FAILED_UNSUPPORTED") == 1)
    st, r = get_auth("/api/tenant/send-log?status=SUCCESS,FAILED_UNSUPPORTED", token=key_a)
    t("?status=をカンマ区切りで複数指定できる", len(r["log"]) == 2)
    company1_name = con.execute("SELECT name FROM companies WHERE id=1").fetchone()[0]
    st, r = get_auth(f"/api/tenant/send-log?q={urllib.parse.quote(company1_name)}", token=key_a)
    t("?q=で会社名の部分一致検索ができる",
      len(r["log"]) == 1 and r["log"][0]["company_id"] == 1)

    print("\n── 送信前後スクリーンショット ──")
    def get_raw(path, token=None):
        req = urllib.request.Request(
            base + path, headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), None

    st, body, ctype = get_raw(f"/api/tenant/send-log/{log_id}/screenshot?kind=before")
    t("認証ヘッダなしのGET .../screenshotは401", st == 401)
    st, body, ctype = get_raw(f"/api/tenant/send-log/{log_id}/screenshot?kind=before", token=key_a)
    t("自テナントの送信前画像が取得できる", st == 200 and ctype == "image/png"
      and body == shot_path.read_bytes())
    st, body, ctype = get_raw(f"/api/tenant/send-log/{log_id}/screenshot?kind=after", token=key_a)
    t("送信後画像が無い場合は404", st == 404)
    st, body, ctype = get_raw(f"/api/tenant/send-log/{log_id}/screenshot?kind=bogus", token=key_a)
    t("不正なkindは404", st == 404)
    st, body, ctype = get_raw(f"/api/tenant/send-log/{log_id}/screenshot?kind=before", token=key_b)
    t("他テナントは自分の送信前画像として取得できない(テナント分離)", st == 404)
    st, body, ctype = get_raw("/api/tenant/send-log/999999999/screenshot?kind=before", token=key_a)
    t("存在しないログIDは404", st == 404)

    print("\n── 自動入力(手動送信サポート機能) ──")
    # list_a_idは既にdry_run送信済み(subject=テスト件名/body=テスト本文がtouchesに
    # 入っている)。その中の1社について、自動送信が失敗した想定のログを1件作る
    af_company = con.execute("""SELECT c.id FROM target_list_members m
        JOIN companies c ON c.id=m.company_id WHERE m.list_id=? LIMIT 1""", (list_a_id,)).fetchone()
    con.execute("""INSERT INTO form_send_log (company_id, tenant_id, list_id, target_url,
        started_at, status, reason_code)
        VALUES (?, ?, ?, 'https://example.co.jp/autofill-test', ?, 'FAILED_UNSUPPORTED',
        'success_not_confirmed')""",
        (af_company["id"], tid_a, list_a_id, datetime.now().isoformat(timespec="seconds")))
    con.commit()
    af_log_id = con.execute("""SELECT id FROM form_send_log WHERE tenant_id=? AND company_id=?
        ORDER BY id DESC LIMIT 1""", (tid_a, af_company["id"])).fetchone()[0]

    st, r = post_auth(f"/api/tenant/send-log/{af_log_id}/autofill-queue", {})
    t("認証ヘッダなしのPOST .../autofill-queueは401", st == 401)
    st, r = post_auth(f"/api/tenant/send-log/{af_log_id}/autofill-queue", {}, token=key_a)
    t("自動入力の準備ができる(元の文章を復元できたリスト送信のため)",
      st == 200 and r.get("url") == "https://example.co.jp/autofill-test", f"r={r}")

    st, r = get_auth("/api/tenant/autofill/pending")
    t("認証ヘッダなしのGET /api/tenant/autofill/pendingは401", st == 401)
    expected_subject, expected_body = con.execute("""SELECT t.subject, t.body FROM touches t
        JOIN target_lists tl ON tl.campaign_id=t.campaign_id
        WHERE tl.id=? AND t.company_id=? ORDER BY t.id DESC LIMIT 1""",
        (list_a_id, af_company["id"])).fetchone()
    st, r = get_auth("/api/tenant/autofill/pending", token=key_a)
    t("準備した内容が件名・本文込みで取得できる(直近の送信文章が復元される)",
      st == 200 and r["values"]["subject"] == expected_subject
      and expected_body in r["values"]["message"], f"st={st} r={r}")
    st, r = get_auth("/api/tenant/autofill/pending", token=key_b)
    t("他テナントには自分宛の自動入力しか見えない(テナント分離)", st == 404)

    # list_id無し(元の文章を逆引きできない)ケース
    con.execute("""INSERT INTO form_send_log (company_id, tenant_id, target_url, started_at,
        status, reason_code) VALUES (999999998, ?, 'https://example.co.jp', ?,
        'FAILED_UNSUPPORTED', 'success_not_confirmed')""",
        (tid_a, datetime.now().isoformat(timespec="seconds")))
    con.commit()
    no_list_log_id = con.execute("""SELECT id FROM form_send_log WHERE tenant_id=? AND company_id=999999998
        ORDER BY id DESC LIMIT 1""", (tid_a,)).fetchone()[0]
    st, r = post_auth(f"/api/tenant/send-log/{no_list_log_id}/autofill-queue", {}, token=key_a)
    t("元の文章を復元できない場合は400でその旨を返す(手動入力を促す)",
      st == 400 and "url" in r)

    # TTL切れの検証(created_atを古い時刻に書き換える)
    post_auth(f"/api/tenant/send-log/{af_log_id}/autofill-queue", {}, token=key_a)
    old_at = (datetime.now() - timedelta(seconds=700)).isoformat(timespec="seconds")
    con.execute("UPDATE autofill_queue SET created_at=? WHERE tenant_id=?", (old_at, tid_a))
    con.commit()
    st, r = get_auth("/api/tenant/autofill/pending", token=key_a)
    t("10分以上前の準備は失効扱いになる", st == 404)

    print("\n── β版ダッシュボード ──")
    st, r = get_auth("/api/tenant/dashboard")
    t("認証ヘッダなしのGET /api/tenant/dashboardは401", st == 401)
    st, r = get_auth("/api/tenant/dashboard", token=key_a)
    t("今月の送信成功数に手動投入した1件が反映される",
      st == 200 and r["this_month"]["success"] >= 1)
    t("累計送信成功数にも反映される", r["all_time"]["success"] >= 1)
    t("返信の件数が反映される(先の出来事記録テストで1件記録済み)",
      r["outcomes"]["replied"] >= 1)
    st, r = get_auth("/api/tenant/dashboard", token=key_b)
    t("他テナントのダッシュボードには自テナントの数字が出ない(テナント分離)",
      st == 200 and r["this_month"]["success"] == 0)

    con.execute("DELETE FROM form_send_log WHERE tenant_id=?", (tid_a,))
    con.commit()

    print("\n── 送信除外設定 ──")
    # can_contact()が既にFalseな会社(他テストの副作用で反応済み等)だと
    # 除外設定による判定変化を検証できないので、まず素の状態でTrueな会社を選ぶ
    excl_company_id = None
    for row in con.execute("SELECT id, name FROM companies WHERE dedup_of IS NULL"):
        if db.can_contact(con, row["id"])[0]:
            excl_company_id, excl_name = row["id"], row["name"]
            break
    excl_name_part = urllib.parse.quote(excl_name[:2])
    st, r = get_auth(f"/api/tenant/companies/search?q={excl_name_part}")
    t("認証ヘッダなしのGET /api/tenant/companies/searchは401", st == 401)
    st, r = get_auth("/api/tenant/companies/search?q=a", token=key_a)
    t("検索語が2文字未満なら400", st == 400)
    st, r = get_auth(f"/api/tenant/companies/search?q={excl_name_part}", token=key_a)
    t("2文字以上の検索で企業が引ける",
      st == 200 and any(c["id"] == excl_company_id for c in r.get("companies", [])))
    st, r = get_auth("/api/tenant/companies/search?q=" + urllib.parse.quote("テナントB専用"), token=key_a)
    t("【テナント分離監査】他テナントの非公開企業は除外設定の検索にも出てこない",
      st == 200 and all(c["name"] != "テナントB専用企業" for c in r.get("companies", [])))

    st, r = post_auth("/api/tenant/exclusions", {"company_id": "not-an-int"}, token=key_a)
    t("company_idが整数でないと400", st == 400)
    st, r = post_auth("/api/tenant/exclusions", {"company_id": 999999999}, token=key_a)
    t("存在しない企業IDは404", st == 404)
    st, r = post_auth("/api/tenant/exclusions", {"company_id": excl_company_id, "reason": "競合他社"},
                      token=key_a)
    t("POST /api/tenant/exclusions で除外に追加", st == 200 and r.get("ok"))

    st, r = get_auth("/api/tenant/exclusions", token=key_a)
    t("GET /api/tenant/exclusions に追加した企業が出る",
      st == 200 and any(e["company_id"] == excl_company_id for e in r.get("exclusions", [])))
    st, r = get_auth("/api/tenant/exclusions", token=key_b)
    t("他テナントの除外設定は見えない(テナント分離)",
      st == 200 and all(e["company_id"] != excl_company_id for e in r.get("exclusions", [])))

    allowed, why = db.can_contact(con, excl_company_id, tenant_id=tid_a)
    t("除外されたテナントではcan_contact()がFalseを返す(バイパスなし)",
      allowed is False and why == "テナント除外設定")
    allowed_b, why_b = db.can_contact(con, excl_company_id, tenant_id=tid_b)
    t("他テナントの送信には影響しない", allowed_b is True)

    st, r = post_auth("/api/tenant/exclusions/remove", {"company_id": excl_company_id}, token=key_a)
    t("POST /api/tenant/exclusions/remove で除外解除", st == 200 and r.get("ok"))
    st, r = get_auth("/api/tenant/exclusions", token=key_a)
    t("解除後はGET /api/tenant/exclusionsに出てこない",
      st == 200 and all(e["company_id"] != excl_company_id for e in r.get("exclusions", [])))
    allowed_after, _ = db.can_contact(con, excl_company_id, tenant_id=tid_a)
    t("解除後はcan_contact()が再びTrueを返す", allowed_after is True)

    con.execute("DELETE FROM tenant_exclusions WHERE tenant_id IN (?,?)", (tid_a, tid_b))
    con.commit()

    print("\n── 送信文章テンプレート ──")
    st, r = post_auth("/api/tenant/templates", {"name": "初回案内", "subject": "件名"}, token=key_a)
    t("bodyが無いと400", st == 400)
    st, r = post_auth("/api/tenant/templates",
                      {"name": "初回案内", "subject": "積算のお手伝い", "body": "本文です"}, token=key_a)
    t("POST /api/tenant/templates でテンプレート保存", st == 200 and bool(r.get("template_id")))
    tmpl_id = r.get("template_id")

    st, r = get_auth("/api/tenant/templates", token=key_a)
    t("GET /api/tenant/templates に保存した内容が出る",
      st == 200 and any(x["id"] == tmpl_id and x["subject"] == "積算のお手伝い"
                         for x in r.get("templates", [])))
    st, r = get_auth("/api/tenant/templates", token=key_b)
    t("他テナントのテンプレートは見えない(テナント分離)",
      st == 200 and all(x["id"] != tmpl_id for x in r.get("templates", [])))

    st, r = post_auth("/api/tenant/templates/delete", {"template_id": tmpl_id}, token=key_b)
    t("他テナントのテンプレートは削除できない(404)", st == 404)
    st, r = post_auth("/api/tenant/templates/delete", {"template_id": tmpl_id}, token=key_a)
    t("POST /api/tenant/templates/delete で削除", st == 200 and r.get("ok"))
    st, r = get_auth("/api/tenant/templates", token=key_a)
    t("削除後はGET /api/tenant/templatesに出てこない",
      st == 200 and all(x["id"] != tmpl_id for x in r.get("templates", [])))

    con.execute("DELETE FROM message_templates WHERE tenant_id IN (?,?)", (tid_a, tid_b))
    con.commit()

    print("\n── 送信元テンプレート ──")
    st, r = post_auth("/api/tenant/sender-templates", {"name": "本社", "sender_name": "テスト株式会社"},
                      token=key_a)
    t("sender_emailが無いと400", st == 400)
    st, r = post_auth("/api/tenant/sender-templates",
                      {"name": "本社", "sender_name": "テスト株式会社 営業部",
                       "sender_email": "sales@test-a.example.co.jp",
                       "sender_address": "東京都千代田区1-1-1", "optout_url": "https://test-a.example.co.jp/optout"},
                      token=key_a)
    t("POST /api/tenant/sender-templates で保存", st == 200 and bool(r.get("template_id")))
    stmpl_id = r.get("template_id")

    st, r = get_auth("/api/tenant/sender-templates", token=key_a)
    t("GET /api/tenant/sender-templates に保存内容が出る",
      st == 200 and any(x["id"] == stmpl_id and x["sender_email"] == "sales@test-a.example.co.jp"
                         for x in r.get("templates", [])))
    st, r = get_auth("/api/tenant/sender-templates", token=key_b)
    t("他テナントの送信元テンプレートは見えない(テナント分離)",
      st == 200 and all(x["id"] != stmpl_id for x in r.get("templates", [])))

    st, r = post_auth("/api/tenant/sender-templates/activate", {"template_id": stmpl_id}, token=key_b)
    t("他テナントのテンプレートは有効化できない(404)", st == 404)
    st, r = post_auth("/api/tenant/sender-templates/activate", {"template_id": stmpl_id}, token=key_a)
    t("POST /api/tenant/sender-templates/activate で有効化", st == 200 and r.get("ok"))

    tenant_row = con.execute("SELECT sender_name, sender_email, sender_address, optout_url "
                              "FROM tenants WHERE id=?", (tid_a,)).fetchone()
    t("有効化するとtenants.sender_*へ反映される",
      tenant_row["sender_name"] == "テスト株式会社 営業部"
      and tenant_row["sender_email"] == "sales@test-a.example.co.jp"
      and tenant_row["sender_address"] == "東京都千代田区1-1-1")

    st, r = post_auth("/api/tenant/sender-templates/delete", {"template_id": stmpl_id}, token=key_b)
    t("他テナントのテンプレートは削除できない(404)", st == 404)
    st, r = post_auth("/api/tenant/sender-templates/delete", {"template_id": stmpl_id}, token=key_a)
    t("POST /api/tenant/sender-templates/delete で削除", st == 200 and r.get("ok"))
    st, r = get_auth("/api/tenant/sender-templates", token=key_a)
    t("削除後はGET /api/tenant/sender-templatesに出てこない",
      st == 200 and all(x["id"] != stmpl_id for x in r.get("templates", [])))

    con.execute("DELETE FROM sender_templates WHERE tenant_id IN (?,?)", (tid_a, tid_b))
    con.commit()

    print("\n── 担当者管理 ──")
    st, r = post_auth("/api/tenant/staff", {"email": "no-name@example.co.jp"}, token=key_a)
    t("nameが無いと400", st == 400)
    st, r = post_auth("/api/tenant/staff", {"name": "担当 太郎", "email": "taro@test-a.example.co.jp"},
                      token=key_a)
    t("POST /api/tenant/staff で担当者追加、api_keyが返る",
      st == 200 and bool(r.get("staff_id")) and r.get("api_key", "").startswith("tk_"))
    staff_id = r.get("staff_id")
    staff_key = r.get("api_key")

    st, r = get_auth("/api/tenant/staff", token=key_a)
    t("GET /api/tenant/staff に追加した担当者が出る(api_keyは含まれない)",
      st == 200 and any(s["id"] == staff_id and s["name"] == "担当 太郎" for s in r.get("staff", []))
      and all("api_key" not in s for s in r.get("staff", [])))
    st, r = get_auth("/api/tenant/staff", token=key_b)
    t("他テナントの担当者は見えない(テナント分離)",
      st == 200 and all(s["id"] != staff_id for s in r.get("staff", [])))

    st, r = get_auth("/api/tenant/lists", token=staff_key)
    t("担当者専用のapi_keyでも同じテナントのデータにアクセスできる", st == 200)

    print("\n── 送信完了通知(MIKOMERU同等。宛先解決のみ検証。実送信はT2実装後) ──")
    import senders as _senders_mod
    notify_calls = []

    def _fake_deliver(self, to, sender, subject, body):
        notify_calls.append(to.email)
        raise NotImplementedError("test-stub: メール送信基盤は未実装")

    orig_deliver = _senders_mod.MailSender._deliver
    _senders_mod.MailSender._deliver = _fake_deliver
    try:
        TL._notify_completion(con, tid_a, "テストリスト", 3, {"sent": 3, "failed": 0})
        t("担当者が登録されていれば、その全員のメールアドレス宛に通知を試みる",
          notify_calls == ["taro@test-a.example.co.jp"])
    finally:
        _senders_mod.MailSender._deliver = orig_deliver

    st, r = post_auth("/api/tenant/staff/revoke", {"staff_id": staff_id}, token=key_b)
    t("他テナントは担当者を失効できない(404)", st == 404)
    st, r = post_auth("/api/tenant/staff/revoke", {"staff_id": staff_id}, token=key_a)
    t("POST /api/tenant/staff/revoke で失効", st == 200 and r.get("ok"))

    st, r = get_auth("/api/tenant/lists", token=staff_key)
    t("失効後は担当者のapi_keyが使えなくなる(401)", st == 401)
    st, r = get_auth("/api/tenant/staff", token=key_a)
    t("失効後はGET /api/tenant/staffに出てこない",
      st == 200 and all(s["id"] != staff_id for s in r.get("staff", [])))

    con.execute("DELETE FROM staff WHERE tenant_id IN (?,?)", (tid_a, tid_b))
    con.commit()

    notify_calls.clear()
    _senders_mod.MailSender._deliver = _fake_deliver
    try:
        TL._notify_completion(con, tid_a, "テストリスト", 3, {"sent": 3, "failed": 0})
        tenant_a_sender_email = con.execute("SELECT sender_email FROM tenants WHERE id=?",
                                             (tid_a,)).fetchone()["sender_email"]
        t("担当者がいなければテナントの送信元メールアドレスへフォールバックする",
          notify_calls == [tenant_a_sender_email])
    finally:
        _senders_mod.MailSender._deliver = orig_deliver

    print("\n── お知らせ ──")
    ann_pub_id = db.add_announcement(con, "テスト告知(公開)", "本文", published=True)
    ann_draft_id = db.add_announcement(con, "テスト告知(非公開)", "本文", published=False)
    st, r = get_auth("/api/tenant/announcements")
    t("認証ヘッダなしのGET /api/tenant/announcementsは401", st == 401)
    st, r = get_auth("/api/tenant/announcements", token=key_a)
    ids = [a["id"] for a in r.get("announcements", [])]
    t("公開中のお知らせが取れる", st == 200 and ann_pub_id in ids)
    t("非公開のお知らせは出てこない", ann_draft_id not in ids)
    st, r = get_auth("/api/tenant/announcements", token=key_b)
    t("お知らせはテナントを問わず全員に見える(全テナント共通)",
      st == 200 and ann_pub_id in [a["id"] for a in r.get("announcements", [])])
    con.execute("DELETE FROM announcements WHERE id IN (?,?)", (ann_pub_id, ann_draft_id))
    con.commit()

    con.execute("DELETE FROM touches WHERE campaign_id=?", (send_campaign_id,))
    con.execute("DELETE FROM campaigns WHERE id=?", (send_campaign_id,))
    con.execute("DELETE FROM target_list_members WHERE list_id IN (?,?)", (list_a_id, list_b_id))
    con.execute("DELETE FROM target_lists WHERE id IN (?,?)", (list_a_id, list_b_id))
    con.execute("DELETE FROM companies WHERE owner_tenant_id IN (?,?)", (tid_a, tid_b))
    con.execute("DELETE FROM offers WHERE tenant_id IN (?,?)", (tid_a, tid_b))
    con.execute("DELETE FROM tenants WHERE id IN (?,?)", (tid_a, tid_b))
    con.commit()

    print("\n── can_contact()バイパス防止の検証（最重要） ──")
    # 配信停止されていない会社を1社選び、未送信の接触を1件作ってから配信停止する。
    # run_op("send")経由でも実際にブロックされることを直接確認する
    # (=このAPIがcan_contact()を素通りする新しい送信経路になっていないことの証拠)
    comp = con.execute("""SELECT id FROM companies
                          WHERE id NOT IN (SELECT company_id FROM suppression) LIMIT 1""").fetchone()
    if comp:
        test_company = comp["id"]
        cur = con.execute(
            "INSERT INTO campaigns (name, started_at, target_rule) VALUES (?,?,?)",
            ("test-ops-send", datetime.now().isoformat(timespec="seconds"), "ALL"))
        test_cid = cur.lastrowid
        con.execute("""INSERT INTO touches (campaign_id, company_id, channel, variant, step,
                       body, unit_cost_yen) VALUES (?,?,?,?,?,?,?)""",
                    (test_cid, test_company, "メール", "A", 1, "テスト本文", 1))
        db.suppress(con, test_company, "optout", source="test-ops")
        con.commit()

        st, r = post_auth("/api/ops/run-step",
                          {"step": "send", "campaignId": test_cid, "dryRun": True}, token=ops_key)
        t("配信停止済み会社へのsendはブロックされる(can_contact()バイパスなし)",
          st == 200 and r.get("ok") and "ガードで中止1" in (r.get("details") or ""),
          f"details={r.get('details')}")
        t("実際には送信されていない(sent_atが立っていない)",
          con.execute("SELECT sent_at FROM touches WHERE campaign_id=?",
                      (test_cid,)).fetchone()[0] is None)

        con.execute("DELETE FROM suppression WHERE company_id=?", (test_company,))
        con.execute("DELETE FROM touches WHERE campaign_id=?", (test_cid,))
        con.execute("DELETE FROM campaigns WHERE id=?", (test_cid,))
        con.commit()
    else:
        t("can_contact()バイパス防止の検証", False, "テスト用の会社が見つからずスキップ")

    print("\n── Kill Switch(異常時の即時送信停止) ──")
    g0 = con.execute("SELECT stopped, reason FROM kill_switch WHERE id=1").fetchone()
    orig_global_stopped = bool(g0["stopped"]) if g0 else True
    orig_global_reason = g0["reason"] if g0 else None
    t("初期状態(db.migrate()直後)ではKill Switchが全体停止になっている(安全側)",
      db.kill_switch_status(con)[0] is True)

    db.set_global_kill_switch(con, True, reason="test-kill-switch", updated_by="test")
    # メールアドレス未設定の会社を選ぶ: Kill Switch解除後の検証で、実チャネルの
    # validate()に弾かれる(=_deliver()に到達せず実送信は起きない)状態を作るため
    comp2 = con.execute("""SELECT id FROM companies
        WHERE id NOT IN (SELECT company_id FROM suppression) AND email IS NULL LIMIT 1""").fetchone()
    if comp2:
        ks_company = comp2["id"]
        cur = con.execute("INSERT INTO campaigns (name, started_at, target_rule) VALUES (?,?,?)",
                           ("test-kill-switch", datetime.now().isoformat(timespec="seconds"), "ALL"))
        ks_cid = cur.lastrowid
        con.execute("""INSERT INTO touches (campaign_id, company_id, channel, variant, step,
                       body, unit_cost_yen) VALUES (?,?,?,?,?,?,?)""",
                    (ks_cid, ks_company, "メール", "A", 1, "テスト本文", 1))
        con.commit()

        st, r = post_auth("/api/ops/run-step",
                          {"step": "send", "campaignId": ks_cid, "dryRun": False}, token=ops_key)
        row = con.execute("SELECT sent_at, note FROM touches WHERE campaign_id=?", (ks_cid,)).fetchone()
        t("Kill Switch有効時はdry_run=falseでも実送信されない",
          st == 200 and r.get("ok") and row["sent_at"] is None)
        t("送信していない理由がtouchesに残る(Kill Switchで中止)",
          "Kill Switch" in (row["note"] or ""))

        db.set_global_kill_switch(con, False, updated_by="test")
        t("解除後はkill_switch_status()がFalseを返す", db.kill_switch_status(con)[0] is False)
        st, r = post_auth("/api/ops/run-step",
                          {"step": "send", "campaignId": ks_cid, "dryRun": False}, token=ops_key)
        row2 = con.execute("SELECT sent_at, note FROM touches WHERE campaign_id=?", (ks_cid,)).fetchone()
        t("解除後はKill Switchでは止まらない(実チャネルの検証まで進む)",
          "Kill Switch" not in (row2["note"] or "") and row2["sent_at"] is None,
          f"note={row2['note']}")

        con.execute("DELETE FROM touches WHERE campaign_id=?", (ks_cid,))
        con.execute("DELETE FROM campaigns WHERE id=?", (ks_cid,))
        con.commit()
    else:
        t("Kill Switchの検証(全体)", False, "メール未設定の会社が見つからずスキップ")

    db.set_tenant_kill_switch(con, 999999, True, reason="test")
    t("テナント別停止: 指定テナントはstopped=Trueになる",
      db.kill_switch_status(con, tenant_id=999999)[0] is True)
    t("テナント別停止: 他テナントには影響しない",
      db.kill_switch_status(con, tenant_id=888888)[0] is False)
    db.set_tenant_kill_switch(con, 999999, False)
    t("テナント別停止を解除するとFalseに戻る",
      db.kill_switch_status(con, tenant_id=999999)[0] is False)

    st, r = get_auth("/api/ops/kill-switch")
    t("認証ヘッダなしのGET /api/ops/kill-switchは401", st == 401)
    st, r = get_auth("/api/ops/kill-switch", token=ops_key)
    t("GET /api/ops/kill-switchで状態が取れる", st == 200 and "global" in r and "tenants" in r)
    st, r = post_auth("/api/ops/kill-switch", {"scope": "global", "stopped": True})
    t("認証ヘッダなしのPOST /api/ops/kill-switchは401", st == 401)
    st, r = post_auth("/api/ops/kill-switch",
                      {"scope": "global", "stopped": True, "reason": "audit-test"}, token=ops_key)
    t("POST /api/ops/kill-switchで全体停止を操作できる",
      st == 200 and r.get("ok") and db.kill_switch_status(con)[0] is True)

    # 元の状態(基本は安全側の「停止中」)へ確実に戻す。テストが実際のDBの
    # 運用状態を変えっぱなしにしないため
    db.set_global_kill_switch(con, orig_global_stopped, reason=orig_global_reason,
                              updated_by="test-restore")

    srv.shutdown()
    # 後片付け
    con.execute("DELETE FROM suppression WHERE company_id=?", (cid,))
    con.execute("DELETE FROM idempotency WHERE key LIKE '%test-api%'")
    # activate:{tid}・click:{tid}は"test-api"を含まないため上のLIKEでは消えない。
    # 消し忘れると、次回このテストを再実行した時に同じtidを掴んだ場合
    # _once()が「既に実行済み」と判定してactivated等が立たなくなる
    con.execute("DELETE FROM idempotency WHERE key IN (?, ?)", (f"activate:{tid}", f"click:{tid}"))
    # このテストが冒頭で使った接触をpaid=0に戻す(課金webhookのテストでpaid=1に
    # してしまうため、戻さないと次回このテストを走らせた時に「テスト対象の接触が
    # ありません」で再度run.py all --demoが必要になってしまう)
    con.execute("""UPDATE touches SET responded=0, signed_up=0, activated=0, paid=0,
                   mrr_yen=0 WHERE id=?""", (tid,))
    con.commit()
    print(f"\n  成功 {sum(ok)} / {len(ok)}")
    return all(ok)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8787
    if cmd == "test":
        sys.exit(0 if self_test() else 1)
    serve(port)
