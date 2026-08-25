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
  GET  /track/click/<token>  MIKOMERUの「URLアクセスの記録」相当。テナントの送信文章
                           中のURLがクリックされたことを記録し、本来のURLへ302
                           リダイレクトする(senders.rewrite_tracked_links()参照)
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
  GET  /api/tenant/lists          自テナントのリスト一覧(?include_deleted=1で
                           削除済みも含める。MIKOMERUの「削除したものを含めて表示」相当)
  GET  /api/tenant/lists/<id>     リスト詳細(自テナントのものだけ。他社分は404)。
                           ?status=success|failed|skip|pending|replied|deal|wonで
                           企業ごとの送信状態で絞り込める(1社ごとのsend_status等を
                           含む。実送信結果はdry_run=falseの送信後に反映される)。
                           ?q=で会社名の部分一致検索ができる。各企業にeditable
                           (bool)を含む(自社がCSV等で追加した非公開データのみtrue。
                           全社共有マスタはfalseで、/members/<id>での編集不可)
  POST /api/tenant/lists/<id>/rename  {"name"} → リスト名変更(MIKOMERU「編集」相当)
  POST /api/tenant/lists/<id>/duplicate  {"name"} → リストの複製(MIKOMERU「複製」相当)
  POST /api/tenant/lists/<id>/remove-members  {"company_ids":[...]} →
                           リストから企業を個別に除外(MIKOMERU「個別削除」相当)
  POST /api/tenant/lists/<id>/members/<company_id>  {"name","contact_url","phone",
                           "email"} → リスト内の企業情報を編集(自社がCSV等で
                           追加した非公開データのみ。全社共有マスタは編集不可で400)
  POST /api/tenant/lists/delete   {"list_ids":[...]} → ソフト削除(一括)
  POST /api/tenant/lists/restore  {"list_ids":[...]} → 復元(一括)。
                           物理削除はしない(送信履歴の追跡を残すため)
  POST /api/tenant/lists・/api/tenant/lists/csv は既存の name/filters/csv に加え
                           existing_list_id(整数)を指定すると、新規リストではなく
                           そのリストへ追加する(MIKOMERUの「リスト保存」モーダルの
                           「既存のリストに追加する」相当)
  POST /api/tenant/search/filter  {"filters"} → MIKOMERUの「リスト取得」[検索]相当。
                           結果テーブル用にpreview_filter()より多い件数を返す
                           (MIKOMERUにもリスト取得側には検索ログが無いため、こちらは
                           search_logへは記録しない)
  POST /api/tenant/search/csv     {"csv","mode":"name"|"url","name_col","url_col",
                           "filename"} → MIKOMERUの「CSV検索」(会社名で検索/URLで検索)
                           [検索実行]相当。mode="url"はurl_col必須で問い合わせページを
                           必ず探索する。実行のたびsearch_logへ記録が残る
  GET  /api/tenant/search-log         自テナントのCSV検索ログ一覧(MIKOMERU同様、
                           CSV検索(会社名/URL)の実行だけを記録する)
  GET  /api/tenant/search-log/<id>    検索ログ詳細(該当企業一覧付き)
  POST /api/tenant/search-log/<id>/save-as-list  {"name" or "existing_list_id"} →
                           検索結果をリストとして保存(filter型は検索条件を今再実行する)
  GET  /api/tenant/search-log/<id>/csv  検索ログの内容をCSVでダウンロード
  POST /api/tenant/lists/<id>/outcome  {"company_id","field":"replied"|"deal"|"won",
                           "value","memo"} → 返信・商談化・受注を手動記録(β版。
                           メール自動取得等はしない)
  POST /api/tenant/lists/<id>/send  {"subject","body","dry_run","scheduled_at",
                           "sender_template_id","allow_no_solicit","cancel_recent_days",
                           "sender_override"} → リストへフォーム自動送信。
                           dry_run既定true(=実サイトへは送らない。list_builder.htmlの
                           UI自体はMIKOMERU同様トグルを持たず常にfalseを送るが、
                           API自体は引き続きtrueを既定に受け付ける)。can_contact()・
                           冪等性・ペーシング上限はsend_campaign()経由でそのまま適用
                           される(HANDOFF.mdの原則を厳守)。scheduled_at(未来のISO日時)
                           を指定すると即時実行せずscheduled_sendsへ予約登録するだけに
                           なる。sender_template_id(整数)を指定すると、テナントの
                           「有効化」済み送信元の代わりにその送信元テンプレートを
                           この送信だけに使う(MIKOMERUの自動送信画面「送信元テンプレート
                           から選択」相当)。allow_no_solicit(bool)はMIKOMERUの「営業拒否
                           サイトへの送信」相当(既定false)。cancel_recent_days(正の整数)は
                           「過去送信対象キャンセル」相当(指定日数以内に実送信済みの会社を
                           対象から除外)。sender_override(オブジェクト)は会社名/住所/
                           部署/役職/氏名/カナ/メール/電話のうち指定したものだけ、
                           この送信に限りその場で上書きする(保存はしない)
  GET  /api/tenant/scheduled-sends?list_id=  予約送信の一覧(自テナント分のみ)
  POST /api/tenant/scheduled-sends/cancel  {"scheduled_id"} → PENDINGの予約を
                           キャンセル(実行済み・キャンセル済みは404)
  GET  /api/tenant/send-log       自テナントのフォーム自動送信履歴(form_send_log)。
                           ?company_id=/?list_id=で絞り込める(会社別の明細=詳細画面用)
  GET  /api/tenant/send-log/executions  MIKOMERUの「自動送信ログ」一覧相当(T22)。
                           会社別の明細ではなく、「いつ・誰が・どのリストへ送ったか」を
                           1リスト=1実行として集計して返す(target_lists.pyの
                           list_send_executions()参照)。?list_id=/?date_from=/?date_to=
                           (YYYY-MM-DD)で絞り込める。?limit=で件数上限(ホームの
                           「最近の営業履歴」で使用)
  POST /api/tenant/send-log/executions/{list_id}/note  {"note"} → 実行(リスト)単位の
                           備考を更新(会社ごとのsend-log/{id}/noteとは別物)
  GET  /api/tenant/send-log/{id}/screenshot?kind=before|after
                           送信前後のスクリーンショット画像(PNG)。自テナントの
                           記録のみ取得可(テナント分離)。無ければ404
  GET  /api/tenant/companies/search?q=  除外設定対象を探す簡易企業検索(2文字以上)
  GET  /api/tenant/exclusions     自テナントの送信除外設定一覧
  POST /api/tenant/exclusions          {"company_id","reason"} → 除外に追加
  POST /api/tenant/exclusions/remove   {"company_id"} → 除外を解除
  POST /api/tenant/exclusions/csv      {"csv","reason"} → 会社名の列を含むCSVを
                           一括で除外登録する(MIKOMERUの送信除外設定「CSVで登録」
                           タブ相当)。商号一致で照合できた行だけ除外される
                           除外はテナント別(tenant_exclusions)。全テナント共通の
                           法令対応suppressionとは別物で、can_contact()が両方を見る
  GET  /api/tenant/templates      自テナントの送信文章テンプレート一覧
  POST /api/tenant/templates           {"name","subject","body"} → 保存
  POST /api/tenant/templates/delete    {"template_id"} → 削除
  POST /api/tenant/templates/update    {"template_id","name","subject","body"} →
                           編集(T39。従来は削除して作り直すしか無かった)
  GET  /api/tenant/sender-templates    自テナントの送信元テンプレート一覧
  POST /api/tenant/sender-templates    {"name","sender_name","sender_email",
                           "sender_address","optout_url","last_name","first_name",
                           "last_name_kana","first_name_kana","postal_code","prefecture",
                           "city","block","building","phone","department","position"} → 保存
  POST /api/tenant/sender-templates/delete    {"template_id"} → 削除
  POST /api/tenant/sender-templates/update    {"template_id", ...(addと同じ項目)} →
                           編集(T39)。既に「有効にする」済みのテンプレートを
                           編集しても、tenants.sender_*へは自動反映されない
                           (反映するには編集後に改めて「有効にする」を押す)
  POST /api/tenant/sender-templates/activate  {"template_id"} → このテナントの
                           送信者情報(tenants.sender_*)へ反映。反映先は
                           senders.send_campaign()が読む列そのものなので、
                           送信ロジック側は変更不要
  GET  /api/tenant/staff          自テナントの担当者一覧(承認待ちは含まない)
  POST /api/tenant/staff               {"name","email"} → 担当者追加(APIキーのみ、
                           認証不要で即使える従来方式)。発行したapi_keyは
                           この応答でしか返さない
  POST /api/tenant/staff/revoke        {"staff_id"} → 担当者のapi_keyを失効
  POST /api/tenant/staff/register  {"name","email","password","role"} →
                           MIKOMERUの「担当者登録」相当(メール+パスワード)。
                           メール認証(GET /verify/staff/<token>)が完了するまで
                           そのapi_keyは使えない。認証用URLは実際にSendGrid経由で
                           担当者へメール送信する(T33)。応答のemail_sentが送信
                           成否を示す。SENDGRID_API_KEY未設定・送信失敗時のみ、
                           運用者が手動で共有できるようverify_pathを応答に含める
                           フォールバックにする(黙って失敗させない。HANDOFF.md
                           T21/T33参照)
  GET  /api/tenant/staff/pending  承認待ち(メール未認証)の担当者一覧
  POST /api/tenant/staff/resend    {"staff_id"} → 認証用URLを再発行し、
                           register同様メールで再送する(応答の形もregisterと同じ)
  GET  /verify/staff/<token>      (認証不要・公開) メール認証リンク。
                           成功・失敗をHTMLページで表示する(MIKOMERUの
                           「認証完了」画面相当)
  POST /api/login    (認証不要・公開) {"email","password"} → ログイン。
                           成功時、そのままAPIキー方式の認証に使えるapi_keyを
                           返す(ブラウザにセッションを持たせる方式ではなく、
                           既存のBearer api_key認証とそのまま互換にするため)
  POST /api/password-reset/request  (認証不要・公開・T34) {"email"} →
                           該当アカウントがあればパスワード再設定メールを送る。
                           メールアドレス列挙攻撃を防ぐため、該当有無に関わらず
                           常に同じ成功応答を返す(リセットURLは応答に含めない。
                           T33のverify_pathフォールバックとは違い、ここは匿名の
                           誰でも呼べるエンドポイントのため)
  POST /api/password-reset/confirm  (認証不要・公開・T34) {"token","new_password"}
                           → 新しいパスワードを確定する
  GET  /reset-password/<token>  (認証不要・公開・T34) 新しいパスワードを入力する
                           フォームページ。list_builder.htmlを経由せず、
                           このページ単体でPOST /api/password-reset/confirmまで
                           完結する(GET /verify/staff/<token>と同じ設計)
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
import csv
import hashlib
import hmac
import io
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
import storage
import target_lists as TL

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dev-secret-change-me")
LP_URL = os.environ.get("LP_URL", "https://ashibase.jp/sekisan")
# 認証メール本文に埋め込む、このAPI自身の公開URL(GET /verify/staff/<token>を
# 実際に叩けるドメイン)。本番では実際の公開ドメインを環境変数で上書きする。
API_PUBLIC_URL = os.environ.get("API_PUBLIC_URL", "https://ashibase.jp")
# touch_idが無い流入を、直近何日以内の接触に帰属させるか
ATTRIBUTION_WINDOW_DAYS = 45

# Stock Factory等の運用API(/api/ops/*)専用のキー。WEBHOOK_SECRETと違い、
# 実送信(send/followup)まで叩ける強い権限のため既定値を持たせない
# (未設定なら誰にも一致しない=常に401、というフェイルセーフにする)。
SALES_ENGINE_API_KEY = os.environ.get("SALES_ENGINE_API_KEY")

_SEND_PATH_RE = re.compile(r"^/api/tenant/lists/(\d+)/send$")
_OUTCOME_PATH_RE = re.compile(r"^/api/tenant/lists/(\d+)/outcome$")
_PREVIEW_MSG_PATH_RE = re.compile(r"^/api/tenant/lists/(\d+)/preview-message$")
_RENAME_PATH_RE = re.compile(r"^/api/tenant/lists/(\d+)/rename$")
_DUPLICATE_PATH_RE = re.compile(r"^/api/tenant/lists/(\d+)/duplicate$")
_REMOVE_MEMBERS_PATH_RE = re.compile(r"^/api/tenant/lists/(\d+)/remove-members$")
_MEMBER_UPDATE_PATH_RE = re.compile(r"^/api/tenant/lists/(\d+)/members/(\d+)$")
_SEARCH_LOG_DETAIL_PATH_RE = re.compile(r"^/api/tenant/search-log/(\d+)$")
_SEARCH_LOG_SAVE_PATH_RE = re.compile(r"^/api/tenant/search-log/(\d+)/save-as-list$")
_SEARCH_LOG_CSV_PATH_RE = re.compile(r"^/api/tenant/search-log/(\d+)/csv$")
_SCREENSHOT_PATH_RE = re.compile(r"^/api/tenant/send-log/(\d+)/screenshot$")
_AUTOFILL_QUEUE_PATH_RE = re.compile(r"^/api/tenant/send-log/(\d+)/autofill-queue$")
_SEND_LOG_NOTE_PATH_RE = re.compile(r"^/api/tenant/send-log/(\d+)/note$")
_SEND_LOG_MANUAL_SENT_PATH_RE = re.compile(r"^/api/tenant/send-log/(\d+)/manual-sent$")
_SEND_LOG_EXEC_NOTE_PATH_RE = re.compile(r"^/api/tenant/send-log/executions/(\d+)/note$")
_VERIFY_STAFF_PATH_RE = re.compile(r"^/verify/staff/([A-Za-z0-9_-]+)$")
_RESET_PASSWORD_PATH_RE = re.compile(r"^/reset-password/([A-Za-z0-9_-]+)$")

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
    except storage.IntegrityError:
        # Postgresは失敗した文があるとロールバックするまで同じトランザクション上の
        # 以後の文をすべて拒否する(SQLiteには無い挙動)。ここで一意制約違反を
        # 「想定内の重複」として握り潰す以上、そのまま続けられるよう明示的に
        # 巻き戻す(sqlite3.Connection.rollback()は何も無くても安全に呼べる)。
        con.rollback()
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


def h_track_click(con, token):
    """MIKOMERUの「URLアクセスの記録」相当。senders.rewrite_tracked_links()が
    本文に埋め込んだトラッキングリンクのクリックを記録し、本来のURLを返す
    (呼び出し側で302リダイレクトする)。h_click()/LP_URLとは別物(こちらは
    AshiBase自身の成長エンジンではなく、各テナントが送る文章中の任意のURLを
    対象にする)。トークンが見つからなければNoneを返す。"""
    return db.resolve_click_token(con, token)


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
    見つからなければNone(=呼び出し側で401にする)。

    戻り値のdictには通常のtenants列に加えて_staff_id/_staff_name を含める
    (誰が実行したかを記録したい呼び出し側向け。テナント共用キーで認証した
    場合はどちらもNone=「担当者を特定できない」)。"""
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):]
    tenant = offers.resolve_tenant_by_key(con, token)
    if not tenant:
        return None
    result = dict(tenant)
    staff = offers.resolve_staff_by_key(con, token)
    result["_staff_id"] = staff["id"] if staff else None
    result["_staff_name"] = staff["name"] if staff else None
    return result


# ── 送信先リスト(SaaS販売用) ─────────────────
def h_tenant_lists_preview(con, tenant_id, data):
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        return 400, {"error": "filtersはオブジェクトで指定してください"}
    return 200, TL.preview_filter(con, tenant_id, filters)


def h_tenant_search_filter(con, tenant_id, data):
    """MIKOMERUの「リスト取得」画面の[検索]ボタン相当。プレビューと違い、
    実行するたびにsearch_logへ記録が残る(検索しただけではリストとして保存されない)。"""
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        return 400, {"error": "filtersはオブジェクトで指定してください"}
    return 200, TL.run_filter_search(con, tenant_id, filters)


def h_tenant_search_csv(con, tenant_id, data):
    """MIKOMERUの「CSV検索」画面の[検索実行]ボタン相当(会社名で検索/URLで検索)。"""
    csv_text = data.get("csv")
    if not csv_text:
        return 400, {"error": "csvは必須です"}
    if len(csv_text) > 10_000_000:  # 10MB上限(暴走・誤操作の被害抑制)
        return 400, {"error": "CSVが大きすぎます(上限10MB)"}
    mode = data.get("mode") or "name"
    if mode not in ("name", "url"):
        return 400, {"error": "modeは'name'または'url'を指定してください"}
    name_col = data.get("name_col")
    url_col = data.get("url_col")
    pref_col = data.get("pref_col")
    if mode == "url" and not url_col:
        return 400, {"error": "URLで検索する場合はurl_colが必須です"}
    filename = (data.get("filename") or "").strip() or None
    res = TL.run_csv_search(con, tenant_id, filename, csv_text, mode=mode,
                             name_col=name_col, url_col=url_col, pref_col=pref_col)
    if "error" in res:
        return 400, res
    return 200, res


def h_tenant_search_log_list(con, tenant_id):
    return 200, {"logs": TL.list_search_log(con, tenant_id)}


def h_tenant_search_log_detail(con, tenant_id, search_log_id):
    res = TL.get_search_log(con, tenant_id, search_log_id)
    if not res:
        return 404, {"error": "検索ログが見つかりません"}
    return 200, res


def h_tenant_search_log_save(con, tenant_id, search_log_id, data):
    existing_list_id = data.get("existing_list_id")
    if existing_list_id is not None and not isinstance(existing_list_id, int):
        return 400, {"error": "existing_list_idは整数で指定してください"}
    name = (data.get("name") or "").strip()
    res = TL.save_search_log_as_list(con, tenant_id, search_log_id, name=name,
                                      existing_list_id=existing_list_id)
    if "error" in res:
        return (404 if res["error"] == "検索ログが見つかりません" else 400), res
    return 200, res


def h_tenant_search_log_csv(con, tenant_id, search_log_id):
    """検索ログの内容をCSVでダウンロードする(MIKOMERUの検索ログ詳細
    [ダウンロード]ボタン相当)。"""
    log = TL.get_search_log(con, tenant_id, search_log_id)
    if not log:
        return 404, {"error": "検索ログが見つかりません"}
    buf = io.StringIO()
    w = csv.writer(buf)
    if log["kind"] == "filter":
        w.writerow(["会社名", "都道府県", "業種", "スコアランク"])
        for c in log["companies"]:
            w.writerow([c["name"], c.get("pref") or "", c.get("trades") or "", c.get("rank") or ""])
    else:
        w.writerow(["会社名(検索条件)", "ステータス", "所在地", "問い合わせページ"])
        for row in (log["csv_rows"] or []):
            status_label = {"success": "成功", "not_found": "会社不明"}.get(row["status"], row["status"])
            company = next((c for c in log["companies"] if c["id"] == row.get("company_id")), None)
            w.writerow([row.get("name") or "", status_label,
                        (company or {}).get("pref") or "", (company or {}).get("contact_url") or ""])
    return 200, {"csv": buf.getvalue()}


def h_tenant_lists_create(con, tenant_id, data):
    """existing_list_id(整数)を指定すると、新規リストではなくそのリストへ
    追加する(MIKOMERUの「リスト保存」モーダルの「既存のリストに追加する」相当)。
    その場合nameは無視してよい(既存リスト名は変えない)。"""
    existing_list_id = data.get("existing_list_id")
    if existing_list_id is not None and not isinstance(existing_list_id, int):
        return 400, {"error": "existing_list_idは整数で指定してください"}
    name = (data.get("name") or "").strip()
    if not existing_list_id and not name:
        return 400, {"error": "nameを入力するか、既存のリストを選択してください"}
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        return 400, {"error": "filtersはオブジェクトで指定してください"}
    res = TL.create_from_filter(con, tenant_id, name, filters, existing_list_id=existing_list_id)
    if "error" in res:
        return 400, res
    return 200, res


def h_tenant_lists_csv(con, tenant_id, data):
    existing_list_id = data.get("existing_list_id")
    if existing_list_id is not None and not isinstance(existing_list_id, int):
        return 400, {"error": "existing_list_idは整数で指定してください"}
    name = (data.get("name") or "").strip()
    csv_text = data.get("csv")
    if (not existing_list_id and not name) or not csv_text:
        return 400, {"error": "csvは必須です。nameを入力するか、既存のリストを選択してください"}
    if len(csv_text) > 10_000_000:  # 10MB上限(暴走・誤操作の被害抑制)
        return 400, {"error": "CSVが大きすぎます(上限10MB)"}
    discover_urls = bool(data.get("discover_urls"))
    res = TL.create_from_csv(con, tenant_id, name, csv_text, discover_urls=discover_urls,
                              existing_list_id=existing_list_id)
    if "error" in res:
        return 400, res
    return 200, res


def h_tenant_lists_list(con, tenant_id, qs=None):
    include_deleted = bool((qs or {}).get("include_deleted", ["0"])[0] == "1")
    return 200, {"lists": TL.list_lists(con, tenant_id, include_deleted=include_deleted)}


def h_tenant_list_rename(con, tenant_id, list_id, data):
    """MIKOMERUの保存済みリスト詳細画面「編集」相当(リスト名の変更のみ)。"""
    name = (data.get("name") or "").strip()
    if not name:
        return 400, {"error": "nameは必須です"}
    ok = TL.rename_list(con, tenant_id, list_id, name)
    if not ok:
        return 404, {"error": "リストが見つかりません"}
    return 200, {"ok": True}


def h_tenant_list_duplicate(con, tenant_id, list_id, data):
    """MIKOMERUの保存済みリスト詳細画面「複製」相当。"""
    name = (data.get("name") or "").strip()
    if not name:
        return 400, {"error": "nameは必須です"}
    res = TL.duplicate_list(con, tenant_id, list_id, name)
    if res is None:
        return 404, {"error": "リストが見つかりません"}
    return 200, res


def h_tenant_list_remove_members(con, tenant_id, list_id, data):
    """MIKOMERUの保存済みリスト詳細画面「リスト企業の個別削除」相当。"""
    company_ids = data.get("company_ids")
    if not isinstance(company_ids, list) or not company_ids \
            or not all(isinstance(x, int) for x in company_ids):
        return 400, {"error": "company_idsは整数の配列で指定してください"}
    removed = TL.remove_members(con, tenant_id, list_id, company_ids)
    row = con.execute("SELECT company_count FROM target_lists WHERE id=? AND tenant_id=?",
                       (list_id, tenant_id)).fetchone()
    return 200, {"ok": True, "removed": removed, "count": row["company_count"] if row else None}


def h_tenant_lists_set_deleted(con, tenant_id, data, deleted):
    """MIKOMERUの保存済みリスト一覧「削除」チェックボックス一括削除/復元相当。
    ソフト削除のため物理削除はしない(送信履歴の追跡を残すため)。"""
    list_ids = data.get("list_ids")
    if not isinstance(list_ids, list) or not list_ids \
            or not all(isinstance(x, int) for x in list_ids):
        return 400, {"error": "list_idsは整数の配列で指定してください"}
    changed = TL.set_lists_deleted(con, tenant_id, list_ids, deleted)
    return 200, {"ok": True, "changed": changed}


def h_tenant_list_detail(con, tenant_id, list_id, qs):
    limit = min(int(qs.get("limit", ["200"])[0]), 1000)
    offset = int(qs.get("offset", ["0"])[0])
    status_filter = qs.get("status", [None])[0]
    q = (qs.get("q", [""])[0] or "").strip() or None
    res = TL.get_list(con, tenant_id, list_id, limit=limit, offset=offset, status_filter=status_filter, q=q)
    if not res:
        return 404, {"error": "リストが見つかりません"}
    return 200, res


_MEMBER_EDITABLE_FIELDS = {"name", "contact_url", "phone", "email"}


def h_tenant_list_member_update(con, tenant_id, list_id, company_id, data):
    """リスト詳細画面の企業行を編集する(会社名・問い合わせURL・電話番号・メール
    アドレスのみ)。自社がCSV等で追加した非公開データのみ編集可(共有マスタは不可。
    詳しくはtarget_lists.update_member_company()参照)。"""
    fields = {}
    for k in _MEMBER_EDITABLE_FIELDS:
        if k not in data:
            continue
        v = data.get(k)
        if v is not None and not isinstance(v, str):
            return 400, {"error": f"{k}は文字列で指定してください"}
        fields[k] = (v or "").strip() or None
    if "name" in fields and not fields["name"]:
        return 400, {"error": "会社名は空にできません"}
    if not fields:
        return 400, {"error": "更新する項目がありません"}
    result = TL.update_member_company(con, tenant_id, list_id, company_id, fields)
    if result is None:
        return 404, {"error": "リストが見つかりません"}
    if "error" in result:
        return 400, result
    return 200, result


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


def h_tenant_send_log_executions(con, tenant_id, qs):
    """MIKOMERUの「自動送信ログ」一覧(T22)。会社別の明細ではなく、
    「いつ・誰が・どのリストへ送ったか」という実行単位の集計を返す。
    ?list_id=で対象リストを1件に絞れる(自動送信ページの送信対象リスト
    プルダウンと同じ選択肢から選ぶ想定)。?date_from=/?date_to=はYYYY-MM-DD。
    ?limit=でホームダッシュボードの「最近の営業履歴」のような直近N件表示に使う。"""
    list_id = qs.get("list_id", [None])[0]
    date_from = (qs.get("date_from", [""])[0] or "").strip() or None
    date_to = (qs.get("date_to", [""])[0] or "").strip() or None
    limit = qs.get("limit", [None])[0]
    execs = TL.list_send_executions(con, tenant_id,
                                     list_id=int(list_id) if list_id and list_id.isdigit() else None,
                                     date_from=date_from, date_to=date_to,
                                     limit=int(limit) if limit and limit.isdigit() else None)
    totals = {"success": 0, "failed": 0, "no_form": 0, "total": 0}
    for e in execs:
        for k in totals:
            totals[k] += e[k]
    return 200, {"executions": execs, "totals": totals}


def h_tenant_send_log_execution_note(con, tenant_id, list_id, data):
    note = (data.get("note") or "").strip() or None
    if not TL.update_send_note(con, tenant_id, list_id, note):
        return 404, {"error": "対象の実行(リスト)が見つかりません"}
    return 200, {"ok": True}


def h_tenant_send_log(con, tenant_id, qs):
    """テナント自身のフォーム自動送信履歴(form_send_log)。他テナント分は
    tenant_id=?で絞り込んでいるため見えない。?company_id=で1社分の履歴
    (何度目のどの結果か、時系列)だけに絞り込める。
    ?q=で会社名の部分一致検索、?status=SUCCESS,FAILED_UNSUPPORTEDのようにカンマ区切り
    で複数の結果ステータスに絞り込める(MIKOMERU同等の検索・結果フィルタ)。
    ?list_id=で自動送信ログ一覧(実行単位)の1行から「詳細」へ絞り込める(T22)。
    countsは(company_id/qの絞り込みは反映しつつ)statusでは絞り込む前の内訳件数
    ——一覧上部の集計バッジ用(チェックを外した項目の件数も見えている必要があるため)。"""
    limit = min(int(qs.get("limit", ["100"])[0]), 500)
    offset = int(qs.get("offset", ["0"])[0])
    company_id = qs.get("company_id", [None])[0]
    list_id = qs.get("list_id", [None])[0]
    name_q = (qs.get("q", [""])[0] or "").strip()
    statuses = [s for s in (qs.get("status", [""])[0] or "").split(",") if s]

    base_where = "l.tenant_id=?"
    base_params = [tenant_id]
    if company_id and company_id.isdigit():
        base_where += " AND l.company_id=?"
        base_params.append(int(company_id))
    if list_id and list_id.isdigit():
        base_where += " AND l.list_id=?"
        base_params.append(int(list_id))
    if name_q:
        base_where += " AND c.name LIKE ?"
        base_params.append(f"%{name_q}%")

    q = f"""SELECT l.id, l.company_id, c.name company_name, l.status, l.reason_code,
            l.contact_url, l.target_url, l.started_at, l.finished_at, l.retry_count,
            l.execution_seconds, l.note, l.manual_sent_at,
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
    row = con.execute("""SELECT l.company_id, l.list_id, l.contact_url, l.target_url, c.name company_name
        FROM form_send_log l LEFT JOIN companies c ON c.id = l.company_id
        WHERE l.id=? AND l.tenant_id=?""", (log_id, tenant_id)).fetchone()
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

    tn = con.execute("""SELECT sender_name, sender_email, sender_address, optout_url,
        sender_last_name, sender_first_name, sender_last_name_kana, sender_first_name_kana,
        sender_postal_code, sender_prefecture, sender_city, sender_block, sender_building,
        sender_phone FROM tenants WHERE id=?""", (tenant_id,)).fetchone()
    sender_name = (tn["sender_name"] if tn else None) or "AshiBase（足場ベース）"
    sender_email = (tn["sender_email"] if tn else None) or "info@ashibase.jp"
    sender_address = (tn["sender_address"] if tn else None) or ""
    optout_url = (tn["optout_url"] if tn else None) or "https://ashibase.jp/optout"
    sender_last_name = (tn["sender_last_name"] if tn else None) or sender_name
    sender_first_name = (tn["sender_first_name"] if tn else None) or ""
    sender_postal_code = (tn["sender_postal_code"] if tn else None) or ""
    sender_phone = (tn["sender_phone"] if tn else None) or ""
    structured_address = "".join(filter(None, [
        tn["sender_prefecture"] if tn else None, tn["sender_city"] if tn else None,
        tn["sender_block"] if tn else None, tn["sender_building"] if tn else None]))
    # senders.FormSender._deliver()と同じ方針(未設定の場合、姓欄には会社名を入れておくが
    # 名欄・フリガナ欄は空のままにする。以前は名欄にも会社名を複製しフリガナは固定文字列
    # "アシベース"を入れていたが、カスタムの送信者名を設定したテナントでは不自然になるため)
    furigana = f"{(tn['sender_last_name_kana'] if tn else None) or ''}" \
               f"{(tn['sender_first_name_kana'] if tn else None) or ''}"
    # senders.render_merge_tags()と同じマージタグ(##TO_COMPANY_NAME##/##FROM_FAMILY_NAME##)
    company_name = row["company_name"] or ""
    subject = (subject or "").replace("##TO_COMPANY_NAME##", company_name) \
                              .replace("##FROM_FAMILY_NAME##", sender_last_name)
    body = body.replace("##TO_COMPANY_NAME##", company_name) \
               .replace("##FROM_FAMILY_NAME##", sender_last_name)
    # senders.FormSender.footer()と同じ形式(実際に送るときに付与される署名)
    full_message = f"{body}\n\n{sender_name} / {sender_email}\n今後のご連絡が不要な場合: {optout_url}"
    values = {"company": sender_name, "name": sender_name, "email": sender_email,
              "phone": sender_phone, "address": structured_address or sender_address,
              "postal_code": sender_postal_code,
              "prefecture": (tn["sender_prefecture"] if tn else None) or "",
              "city": (tn["sender_city"] if tn else None) or "",
              "block": (tn["sender_block"] if tn else None) or "",
              "building": (tn["sender_building"] if tn else None) or "",
              "last_name": sender_last_name, "first_name": sender_first_name,
              "subject": subject, "message": full_message, "furigana": furigana}

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


def h_tenant_send_log_note(con, tenant_id, log_id, data):
    """MIKOMERUの送信ログ「備考」欄相当。営業メモ(架電済み、等)を自由記述で残せる。"""
    note = (data.get("note") or "").strip()
    if not db.update_form_send_log_note(con, tenant_id, log_id, note or None):
        return 404, {"error": "対象の送信ログが見つかりません"}
    return 200, {"ok": True}


def h_tenant_send_log_manual_sent(con, tenant_id, log_id, data):
    """MIKOMERUの「手動送信済み」チェック相当。自動入力アシスト後、人が実際に
    フォームを送信し終えたことを記録する(取り消しも可能)。"""
    manual_sent = bool(data.get("manual_sent"))
    if not db.set_form_send_log_manual_sent(con, tenant_id, log_id, manual_sent):
        return 404, {"error": "対象の送信ログが見つかりません"}
    return 200, {"ok": True}


def h_tenant_send_log_csv(con, tenant_id, qs):
    """自動送信ログのCSVダウンロード(MIKOMERU同等)。表示中の絞り込み(?q=/?status=/?list_id=)
    をそのまま反映する。件数上限は付けない(ダウンロード目的のため一覧表示より緩くする)。"""
    name_q = (qs.get("q", [""])[0] or "").strip()
    list_id = qs.get("list_id", [None])[0]
    statuses = [s for s in (qs.get("status", [""])[0] or "").split(",") if s]
    where = "l.tenant_id=?"
    params = [tenant_id]
    if list_id and list_id.isdigit():
        where += " AND l.list_id=?"
        params.append(int(list_id))
    if name_q:
        where += " AND c.name LIKE ?"
        params.append(f"%{name_q}%")
    if statuses:
        where += f" AND l.status IN ({','.join('?' * len(statuses))})"
        params += statuses
    rows = con.execute(f"""SELECT l.id, c.name company_name, l.contact_url, l.status,
            l.reason_code, l.note, l.manual_sent_at, l.started_at, l.finished_at
        FROM form_send_log l LEFT JOIN companies c ON c.id = l.company_id
        WHERE {where} ORDER BY l.id DESC""", params).fetchall()

    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "会社名", "お問い合わせURL", "結果", "詳細", "備考",
                "手動送信済み", "登録日時", "実行日時"])
    for r in rows:
        w.writerow([r["id"], r["company_name"] or "", r["contact_url"] or "", r["status"],
                    r["reason_code"] or "", r["note"] or "",
                    "済" if r["manual_sent_at"] else "", r["started_at"] or "",
                    r["finished_at"] or ""])
    return 200, {"csv": buf.getvalue()}


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


_EXCLUDE_CSV_NAME_COLS = {"name", "会社名", "企業名", "法人名", "商号"}


def h_tenant_exclusions_csv(con, tenant_id, data):
    """送信除外設定のCSV一括登録(MIKOMERUの「送信除外設定|登録」画面の
    「CSVで登録」タブ相当)。会社名の列だけを見て、共有マスタ or 自テナントの
    非公開データ(companies.owner_tenant_id)から商号一致で照合する。
    照合できなかった行は除外できないため件数だけ返す(黙って無視しない)。"""
    csv_text = data.get("csv")
    if not csv_text:
        return 400, {"error": "csvは必須です"}
    if len(csv_text) > 2_000_000:  # 2MB上限(暴走・誤操作の被害抑制)
        return 400, {"error": "CSVが大きすぎます(上限2MB)"}
    reason = (data.get("reason") or "").strip() or None

    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)[:5000]
    if not rows:
        return 400, {"error": "CSVにデータ行がありません"}

    matched = not_found = 0
    for row in rows:
        raw_name = None
        for k, v in row.items():
            if k and k.strip().lower() in _EXCLUDE_CSV_NAME_COLS and (v or "").strip():
                raw_name = v.strip()
                break
        if not raw_name:
            not_found += 1
            continue
        name_norm = db.normalize_name(raw_name)
        # 同じname_normで複数社が残っている場合(重複排除しきれていない別法人表記等)、
        # ORDER BY無しのLIMIT 1だとSQLite/Postgresで返る行が異なりうる。
        # idの昇順で決定的に選ぶ(バックエンドを問わず常に同じ会社を除外する)。
        c = con.execute("""SELECT id FROM companies WHERE name_norm=? AND dedup_of IS NULL
            AND (owner_tenant_id IS NULL OR owner_tenant_id=?) ORDER BY id LIMIT 1""",
            (name_norm, tenant_id)).fetchone()
        if not c:
            not_found += 1
            continue
        db.exclude_for_tenant(con, tenant_id, c["id"], reason=reason)
        matched += 1
    return 200, {"matched": matched, "not_found": not_found}


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


def h_tenant_templates_update(con, tenant_id, data):
    """登録済みテンプレートの編集(T39。従来は削除して作り直すしか無かった)。"""
    template_id = data.get("template_id")
    if not isinstance(template_id, int):
        return 400, {"error": "template_idは必須です"}
    name = (data.get("name") or "").strip()
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    if not name or not subject or not body:
        return 400, {"error": "name・subject・bodyはすべて必須です"}
    if not db.update_message_template(con, tenant_id, template_id, name, subject, body):
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
                                  postal_code=opt("postal_code"),
                                  prefecture=opt("prefecture"), city=opt("city"),
                                  block=opt("block"), building=opt("building"),
                                  phone=opt("phone"), department=opt("department"),
                                  position=opt("position"))
    return 200, {"ok": True, "template_id": tid}


def h_tenant_sender_templates_delete(con, tenant_id, data):
    template_id = data.get("template_id")
    if not isinstance(template_id, int):
        return 400, {"error": "template_idは必須です"}
    if not db.delete_sender_template(con, tenant_id, template_id):
        return 404, {"error": "テンプレートが見つかりません"}
    return 200, {"ok": True}


def h_tenant_sender_templates_update(con, tenant_id, data):
    """登録済みの送信元テンプレートの編集(T39)。既に「有効にする」済みの
    テンプレートを編集しても、tenants.sender_*へは自動反映されない
    (activate_sender_template()参照)。反映するには編集後に改めて
    「有効にする」を押す必要がある。"""
    template_id = data.get("template_id")
    if not isinstance(template_id, int):
        return 400, {"error": "template_idは必須です"}
    name = (data.get("name") or "").strip()
    sender_name = (data.get("sender_name") or "").strip()
    sender_email = (data.get("sender_email") or "").strip()
    if not name or not sender_name or not sender_email:
        return 400, {"error": "name・sender_name・sender_emailは必須です"}

    def opt(key):
        return (data.get(key) or "").strip() or None
    ok = db.update_sender_template(con, tenant_id, template_id, name, sender_name, sender_email,
                                    sender_address=(data.get("sender_address") or "").strip(),
                                    optout_url=opt("optout_url"),
                                    last_name=opt("last_name"), first_name=opt("first_name"),
                                    last_name_kana=opt("last_name_kana"),
                                    first_name_kana=opt("first_name_kana"),
                                    postal_code=opt("postal_code"),
                                    prefecture=opt("prefecture"), city=opt("city"),
                                    block=opt("block"), building=opt("building"),
                                    phone=opt("phone"), department=opt("department"),
                                    position=opt("position"))
    if not ok:
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


_STAFF_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _send_staff_verification_email(con, tenant_id, name, email, verify_token):
    """認証メールを実際にSendGrid経由で送る(T33)。応答を作る呼び出し元に
    例外を伝播させない(登録・再発行そのものは、メール送信の成否にかかわらず
    完了させる。_notify_completion()と同じ「ログにだけ残す」方針)。
    戻り値: 送信できたかどうか(bool)。"""
    import senders
    verify_url = f"{API_PUBLIC_URL}/verify/staff/{verify_token}"
    subject = "【AshiBase】担当者登録の確認"
    body = (f"{name} 様\n\n"
            f"AshiBaseへ担当者として登録されました。以下のURLを開いて、"
            f"メールアドレスの確認を完了してください"
            f"(有効期限: 登録から{offers.EMAIL_VERIFY_EXPIRY_HOURS}時間)。\n\n"
            f"{verify_url}\n\n"
            f"心当たりがない場合は、このメールを破棄してください。")
    default_sender = senders.Sender(name="AshiBase（足場ベース）", email="info@ashibase.jp",
                                     address="", optout_url="https://ashibase.jp/optout")
    mailer = senders.MailSender(con, dry_run=False)
    try:
        mailer._deliver(senders.Recipient(company_id=0, name=name, email=email),
                        default_sender, subject, body)
        return True
    except NotImplementedError:
        print(f"  [担当者認証メール] メール送信基盤が未設定のため送信できません(宛先: {email})")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [担当者認証メール] 送信に失敗しました(宛先: {email}): {e}")
        return False


def h_tenant_staff_register(con, tenant_id, data):
    """MIKOMERUの「担当者登録」相当。offers.register_staff()の薄いラッパー。
    認証用URLは実際にメール送信する(T33)。送信できなかった場合のみ、
    運用者が手動共有できるようverify_pathを応答に含める(存在しないふりを
    して黙って失敗させない)。"""
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role = (data.get("role") or "一般").strip() or "一般"
    if not name or not email or not password:
        return 400, {"error": "name・email・passwordは必須です"}
    if not _STAFF_EMAIL_RE.match(email):
        return 400, {"error": "メールアドレスの形式が正しくありません"}
    if len(password) < 8 or len(password) > 64:
        return 400, {"error": "パスワードは半角英数記号8〜64文字で指定してください"}
    res = offers.register_staff(con, tenant_id, name, email, password, role=role)
    if "error" in res:
        return 400, res
    email_sent = _send_staff_verification_email(con, tenant_id, name, email, res["verify_token"])
    out = {"ok": True, "staff_id": res["staff_id"], "email_sent": email_sent}
    if not email_sent:
        out["verify_path"] = f"/verify/staff/{res['verify_token']}"
    return 200, out


def h_tenant_staff_pending(con, tenant_id):
    return 200, {"pending": offers.list_pending_staff(con, tenant_id)}


def h_tenant_staff_resend(con, tenant_id, data):
    staff_id = data.get("staff_id")
    if not isinstance(staff_id, int):
        return 400, {"error": "staff_idは必須です"}
    token = offers.resend_staff_verification(con, tenant_id, staff_id)
    if not token:
        return 404, {"error": "承認待ちの担当者が見つかりません"}
    row = con.execute("SELECT name, email FROM staff WHERE id=?", (staff_id,)).fetchone()
    email_sent = _send_staff_verification_email(con, tenant_id, row["name"], row["email"], token)
    out = {"ok": True, "email_sent": email_sent}
    if not email_sent:
        out["verify_path"] = f"/verify/staff/{token}"
    return 200, out


def h_verify_staff_email(con, token):
    """GET /verify/staff/<token>(公開)。MIKOMERUの「認証完了」画面相当のHTMLを返す。"""
    ok = offers.verify_staff_email(con, token)
    title = "認証完了" if ok else "認証エラー"
    message = ("メールによる本登録認証が完了しました。管理者からログイン用の"
               "メールアドレス・パスワードを受け取り、ログインしてください。") if ok else (
               "このURLは無効か、24時間の有効期限が切れています。管理者に"
               "「承認待ち一覧」からの再発行を依頼してください。")
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>AshiBase — {title}</title>
<style>body{{font-family:sans-serif;background:#EFF1F2;display:flex;align-items:center;
  justify-content:center;height:100vh;margin:0}}
.card{{background:#fff;border-radius:8px;padding:32px 40px;max-width:420px;text-align:center;
  box-shadow:0 2px 12px rgba(0,0,0,.08)}}
h1{{font-size:18px;color:{"#1E7A4D" if ok else "#B4441F"}}}
p{{font-size:13px;color:#333;line-height:1.7}}</style></head>
<body><div class="card"><h1>{"✓" if ok else "×"} {title}</h1><p>{message}</p></div></body></html>"""


def _send_password_reset_email(con, name, email, reset_token):
    """パスワード再設定メールを実際にSendGrid経由で送る(T34)。呼び出し元に
    例外を伝播させない(_send_staff_verification_emailと同じ「ログにだけ残す」
    方針)。ここは戻り値を使わない — 呼び出し元(h_password_reset_request)は
    メール列挙攻撃を防ぐため送信成否に関わらず常に同じ応答を返すため。"""
    import senders
    reset_url = f"{API_PUBLIC_URL}/reset-password/{reset_token}"
    subject = "【AshiBase】パスワード再設定のご案内"
    body = (f"{name} 様\n\n"
            f"パスワード再設定のリクエストを受け付けました。以下のURLから新しい"
            f"パスワードを設定してください"
            f"(有効期限: 発行から{offers.PASSWORD_RESET_EXPIRY_HOURS}時間)。\n\n"
            f"{reset_url}\n\n"
            f"心当たりがない場合は、このメールを破棄してください"
            f"(このメールを開くだけでパスワードが変更されることはありません)。")
    default_sender = senders.Sender(name="AshiBase（足場ベース）", email="info@ashibase.jp",
                                     address="", optout_url="https://ashibase.jp/optout")
    mailer = senders.MailSender(con, dry_run=False)
    try:
        mailer._deliver(senders.Recipient(company_id=0, name=name, email=email),
                        default_sender, subject, body)
    except NotImplementedError:
        print(f"  [パスワード再設定メール] メール送信基盤が未設定のため送信できません(宛先: {email})")
    except Exception as e:  # noqa: BLE001
        print(f"  [パスワード再設定メール] 送信に失敗しました(宛先: {email}): {e}")


def h_password_reset_request(con, data):
    """POST /api/password-reset/request(公開・認証不要)。{"email"} →
    パスワード再設定メールを送る。メールアドレス列挙攻撃(このAPIの応答差から
    「どのメールアドレスが登録済みか」を第三者が探れてしまう)を防ぐため、
    該当アカウントの有無に関わらず常に同じ成功応答を返す。verify_pathの
    フォールバック(T33)とは異なり、ここは公開・匿名で誰でも呼べるエンドポイント
    なのでリセットURLを応答へ含めることは絶対にしない(含めてしまうと、他人の
    メールアドレスを入力するだけでアカウント乗っ取りが成立してしまう)。"""
    email = (data.get("email") or "").strip().lower()
    if not email:
        return 400, {"error": "emailは必須です"}
    res = offers.request_password_reset(con, email)
    if res:
        staff_id, name, reset_token = res
        _send_password_reset_email(con, name, email, reset_token)
    return 200, {"ok": True,
                 "message": "ご入力のメールアドレスが登録済みであれば、パスワード再設定用のメールを送信しました"}


def h_password_reset_confirm(con, data):
    """POST /api/password-reset/confirm(公開・認証不要)。
    {"token","new_password"} → 新しいパスワードを確定する。"""
    token = (data.get("token") or "").strip()
    new_password = data.get("new_password") or ""
    if not token:
        return 400, {"error": "tokenは必須です"}
    if len(new_password) < 8 or len(new_password) > 64:
        return 400, {"error": "パスワードは半角英数記号8〜64文字で指定してください"}
    if not offers.confirm_password_reset(con, token, new_password):
        return 400, {"error": "このURLは無効か、有効期限が切れています。再度パスワード再設定をお申し込みください"}
    return 200, {"ok": True}


def h_reset_password_page(token):
    """GET /reset-password/<token>(公開)。MIKOMERUの「新しいパスワードを設定する」
    画面相当。新パスワード入力→そのままこのページからJSで
    POST /api/password-reset/confirmを叩く(list_builder.htmlを経由しなくても
    リンクを開くだけで完結させるため、h_verify_staff_emailと同じ設計)。"""
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>AshiBase — パスワード再設定</title>
<style>body{{font-family:sans-serif;background:#EFF1F2;display:flex;align-items:center;
  justify-content:center;min-height:100vh;margin:0}}
.card{{background:#fff;border-radius:8px;padding:32px 40px;max-width:420px;width:100%;
  box-shadow:0 2px 12px rgba(0,0,0,.08);box-sizing:border-box}}
h1{{font-size:18px;color:#333;text-align:center;margin-top:0}}
label{{display:block;font-size:12px;color:#555;margin:14px 0 4px}}
input{{width:100%;box-sizing:border-box;padding:8px;font-size:14px;border:1px solid #ccc;
  border-radius:4px}}
button{{width:100%;margin-top:18px;padding:10px;font-size:14px;background:#4F8FEF;color:#fff;
  border:none;border-radius:4px;cursor:pointer}}
button:disabled{{background:#aaa;cursor:default}}
.msg{{font-size:13px;line-height:1.7;margin-top:14px;text-align:center}}
.msg.ok{{color:#1E7A4D}} .msg.err{{color:#B4441F}}</style></head>
<body><div class="card">
  <h1>新しいパスワードを設定</h1>
  <label>新しいパスワード(半角英数記号8〜64文字)</label>
  <input type="password" id="pw1">
  <label>新しいパスワード(確認)</label>
  <input type="password" id="pw2">
  <button id="btn">パスワードを変更する</button>
  <div id="msg" class="msg"></div>
</div>
<script>
document.getElementById("btn").addEventListener("click", async () => {{
  const pw1 = document.getElementById("pw1").value;
  const pw2 = document.getElementById("pw2").value;
  const msg = document.getElementById("msg");
  const btn = document.getElementById("btn");
  if (pw1 !== pw2) {{ msg.className = "msg err"; msg.textContent = "パスワードが一致しません"; return; }}
  if (pw1.length < 8 || pw1.length > 64) {{
    msg.className = "msg err"; msg.textContent = "パスワードは半角英数記号8〜64文字で指定してください"; return;
  }}
  btn.disabled = true;
  try {{
    const res = await fetch("/api/password-reset/confirm", {{
      method: "POST", headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{token: {json.dumps(token)}, new_password: pw1}}),
    }});
    const body = await res.json().catch(() => ({{}}));
    if (!res.ok) throw new Error(body.error || ("HTTP " + res.status));
    msg.className = "msg ok";
    msg.textContent = "パスワードを変更しました。ログイン画面から新しいパスワードでログインしてください。";
    btn.disabled = true;
  }} catch (e) {{
    msg.className = "msg err"; msg.textContent = e.message; btn.disabled = false;
  }}
}});
</script>
</body></html>"""


def h_login(con, data):
    """POST /api/login(公開)。MIKOMERUのメールアドレス+パスワードでの
    ログイン画面相当。成功時はそのままapi_keyを返し、フロントは既存の
    接続設定(#apiKey)と同じ経路で以後の全APIを叩く(セッション/Cookieは持たない)。"""
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        return 400, {"error": "email・passwordは必須です"}
    ok, res = offers.login_staff(con, email, password)
    if not ok:
        return 401, {"error": res}
    return 200, {"ok": True, **res}


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


_SENDER_OVERRIDE_KEYS = {"name", "email", "postal_code", "prefecture", "city", "block", "building",
                          "department", "position", "last_name", "first_name",
                          "last_name_kana", "first_name_kana", "phone"}


def _parse_sender_override(data):
    """自動送信フォームで送信元テンプレートの内容を送信直前にその場で上書きする値
    (MIKOMERU同等。sender_templates/tenantsへは保存しない)。未知のキーは無視し、
    文字列以外の値が来たら400にする。空のdict/未指定ならNoneを返す(=上書きなし)。"""
    raw = data.get("sender_override")
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "sender_overrideはオブジェクトで指定してください"
    override = {}
    for k, v in raw.items():
        if k not in _SENDER_OVERRIDE_KEYS:
            continue
        if v is None:
            continue
        if not isinstance(v, str):
            return None, f"sender_override.{k}は文字列で指定してください"
        v = v.strip()
        if v:
            override[k] = v
    return (override or None), None


def h_tenant_list_send(con, tenant_id, list_id, data, staff_id=None):
    """保存済みリストから実際にフォーム自動送信キャンペーンを走らせる。
    dry_runは既定でTrue(=実サイトへは何も送らない)。実送信するには
    明示的に dry_run:false を指定する必要がある(取り消せない操作のため)。
    UI側(list_builder.html)はMIKOMERU同様にドライランのトグル自体を持たず、
    常にdry_run:falseを送る(=顧客からは常に実送信。この関数自体はAPIとして
    dry_runを引き続き受け付ける——ops/テスト用途で内部的に使うため)。

    allow_no_solicit: MIKOMERUの「営業拒否サイトへの送信」相当(既定False=従来通り
    スキップする安全側)。sender_override: 送信元情報の一部/全部をこの送信だけ
    その場で上書きする(MIKOMERUの自動送信フォームの各入力欄に相当)。
    cancel_recent_days: MIKOMERUの「過去送信対象キャンセル」相当。指定日数以内に
    実送信済みの会社をこの送信の対象から除外する。

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
    track_clicks = bool(data.get("track_clicks"))
    allow_no_solicit = bool(data.get("allow_no_solicit"))

    cancel_recent_days = data.get("cancel_recent_days")
    if cancel_recent_days is not None:
        if not isinstance(cancel_recent_days, int) or isinstance(cancel_recent_days, bool) \
                or cancel_recent_days <= 0:
            return 400, {"error": "cancel_recent_daysは正の整数で指定してください"}

    sender_override, ov_err = _parse_sender_override(data)
    if ov_err:
        return 400, {"error": ov_err}

    sender_template_id = data.get("sender_template_id")
    if sender_template_id is not None:
        if not isinstance(sender_template_id, int):
            return 400, {"error": "sender_template_idは整数で指定してください"}
        owns = con.execute("SELECT 1 FROM sender_templates WHERE id=? AND tenant_id=?",
                            (sender_template_id, tenant_id)).fetchone()
        if not owns:
            return 404, {"error": "指定された送信元テンプレートが見つかりません"}

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
                                        when.isoformat(timespec="seconds"),
                                        track_clicks=track_clicks,
                                        sender_template_id=sender_template_id,
                                        allow_no_solicit=allow_no_solicit,
                                        cancel_recent_days=cancel_recent_days,
                                        sender_override=sender_override)
        return 200, {"scheduled": True, "scheduled_id": sid,
                     "scheduled_at": when.isoformat(timespec="seconds")}

    res = TL.send_list(con, tenant_id, list_id, subject, body, dry_run=dry_run,
                        track_clicks=track_clicks, sender_template_id=sender_template_id,
                        staff_id=staff_id, allow_no_solicit=allow_no_solicit,
                        cancel_recent_days=cancel_recent_days, sender_override=sender_override)
    if res is None:
        return 404, {"error": "リストが見つかりません"}
    if "error" in res:
        return 400, res
    return 200, res


def h_tenant_list_preview_message(con, tenant_id, list_id, data):
    """MIKOMERUの「プレビュー」相当。実際に送るのと同じマージタグ置換
    (senders.render_merge_tags())を使い、リスト内の企業1社をサンプルに
    件名・本文がどう置き換わるかを事前確認できる(送信は行わない)。"""
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    if not subject and not body:
        return 400, {"error": "件名または本文を入力してください"}
    lst = con.execute("SELECT id FROM target_lists WHERE id=? AND tenant_id=?",
                       (list_id, tenant_id)).fetchone()
    if not lst:
        return 404, {"error": "リストが見つかりません"}
    sample = con.execute("""SELECT c.name FROM target_list_members m
        JOIN companies c ON c.id = m.company_id
        WHERE m.list_id=? ORDER BY (c.contact_url IS NULL), m.company_id LIMIT 1""",
        (list_id,)).fetchone()
    sample_name = sample["name"] if sample else "(サンプル企業名)"

    tn = con.execute("""SELECT sender_name, sender_last_name FROM tenants WHERE id=?""",
                      (tenant_id,)).fetchone()
    sender_name = (tn["sender_name"] if tn else None) or "AshiBase（足場ベース）"
    sender_last_name = (tn["sender_last_name"] if tn else None) or sender_name

    import senders as S
    to = S.Recipient(company_id=0, name=sample_name)
    sender = S.Sender(name=sender_name, email="", address="", optout_url="",
                       last_name=sender_last_name)
    return 200, {"sample_company": sample_name,
                 "subject": S.render_merge_tags(subject, to, sender),
                 "body": S.render_merge_tags(body, to, sender)}


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
        note_match = _SEND_LOG_NOTE_PATH_RE.match(path)
        manual_sent_match = _SEND_LOG_MANUAL_SENT_PATH_RE.match(path)
        exec_note_match = _SEND_LOG_EXEC_NOTE_PATH_RE.match(path)
        preview_msg_match = _PREVIEW_MSG_PATH_RE.match(path)
        rename_match = _RENAME_PATH_RE.match(path)
        duplicate_match = _DUPLICATE_PATH_RE.match(path)
        remove_members_match = _REMOVE_MEMBERS_PATH_RE.match(path)
        member_update_match = _MEMBER_UPDATE_PATH_RE.match(path)
        search_log_save_match = _SEARCH_LOG_SAVE_PATH_RE.match(path)
        if path in ("/api/tenant/lists/preview", "/api/tenant/lists", "/api/tenant/lists/csv",
                     "/api/tenant/lists/delete", "/api/tenant/lists/restore",
                     "/api/tenant/search/filter", "/api/tenant/search/csv") \
                or send_match or outcome_match or autofill_match or note_match \
                or manual_sent_match or exec_note_match or preview_msg_match \
                or rename_match or duplicate_match or remove_members_match \
                or member_update_match or search_log_save_match:
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
                elif path == "/api/tenant/lists/delete":
                    st, res = h_tenant_lists_set_deleted(con, tenant["id"], data, True)
                elif path == "/api/tenant/lists/restore":
                    st, res = h_tenant_lists_set_deleted(con, tenant["id"], data, False)
                elif path == "/api/tenant/search/filter":
                    st, res = h_tenant_search_filter(con, tenant["id"], data)
                elif path == "/api/tenant/search/csv":
                    st, res = h_tenant_search_csv(con, tenant["id"], data)
                elif search_log_save_match:
                    st, res = h_tenant_search_log_save(con, tenant["id"],
                                                        int(search_log_save_match.group(1)), data)
                elif send_match:
                    st, res = h_tenant_list_send(con, tenant["id"], int(send_match.group(1)), data,
                                                  staff_id=tenant.get("_staff_id"))
                elif outcome_match:
                    st, res = h_tenant_list_member_outcome(con, tenant["id"],
                                                            int(outcome_match.group(1)), data)
                elif note_match:
                    st, res = h_tenant_send_log_note(con, tenant["id"],
                                                      int(note_match.group(1)), data)
                elif exec_note_match:
                    st, res = h_tenant_send_log_execution_note(con, tenant["id"],
                                                                int(exec_note_match.group(1)), data)
                elif manual_sent_match:
                    st, res = h_tenant_send_log_manual_sent(con, tenant["id"],
                                                             int(manual_sent_match.group(1)), data)
                elif preview_msg_match:
                    st, res = h_tenant_list_preview_message(con, tenant["id"],
                                                             int(preview_msg_match.group(1)), data)
                elif rename_match:
                    st, res = h_tenant_list_rename(con, tenant["id"], int(rename_match.group(1)), data)
                elif duplicate_match:
                    st, res = h_tenant_list_duplicate(con, tenant["id"], int(duplicate_match.group(1)), data)
                elif remove_members_match:
                    st, res = h_tenant_list_remove_members(con, tenant["id"],
                                                            int(remove_members_match.group(1)), data)
                elif member_update_match:
                    st, res = h_tenant_list_member_update(
                        con, tenant["id"], int(member_update_match.group(1)),
                        int(member_update_match.group(2)), data)
                else:
                    st, res = h_tenant_send_log_autofill_queue(con, tenant["id"],
                                                                int(autofill_match.group(1)))
                return self._json(st, res)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:200]})
            finally:
                con.close()

        if path in ("/api/tenant/exclusions", "/api/tenant/exclusions/remove",
                     "/api/tenant/exclusions/csv"):
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if path == "/api/tenant/exclusions":
                    st, res = h_tenant_exclusions_add(con, tenant["id"], data)
                elif path == "/api/tenant/exclusions/csv":
                    st, res = h_tenant_exclusions_csv(con, tenant["id"], data)
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

        if path in ("/api/tenant/templates", "/api/tenant/templates/delete",
                    "/api/tenant/templates/update"):
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if path == "/api/tenant/templates":
                    st, res = h_tenant_templates_add(con, tenant["id"], data)
                elif path == "/api/tenant/templates/delete":
                    st, res = h_tenant_templates_delete(con, tenant["id"], data)
                else:
                    st, res = h_tenant_templates_update(con, tenant["id"], data)
                return self._json(st, res)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:200]})
            finally:
                con.close()

        if path in ("/api/tenant/sender-templates", "/api/tenant/sender-templates/delete",
                    "/api/tenant/sender-templates/activate", "/api/tenant/sender-templates/update"):
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if path == "/api/tenant/sender-templates":
                    st, res = h_tenant_sender_templates_add(con, tenant["id"], data)
                elif path == "/api/tenant/sender-templates/delete":
                    st, res = h_tenant_sender_templates_delete(con, tenant["id"], data)
                elif path == "/api/tenant/sender-templates/update":
                    st, res = h_tenant_sender_templates_update(con, tenant["id"], data)
                else:
                    st, res = h_tenant_sender_templates_activate(con, tenant["id"], data)
                return self._json(st, res)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:200]})
            finally:
                con.close()

        if path in ("/api/tenant/staff", "/api/tenant/staff/revoke",
                    "/api/tenant/staff/register", "/api/tenant/staff/resend"):
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if path == "/api/tenant/staff":
                    st, res = h_tenant_staff_add(con, tenant["id"], data)
                elif path == "/api/tenant/staff/revoke":
                    st, res = h_tenant_staff_revoke(con, tenant["id"], data)
                elif path == "/api/tenant/staff/register":
                    st, res = h_tenant_staff_register(con, tenant["id"], data)
                else:
                    st, res = h_tenant_staff_resend(con, tenant["id"], data)
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
            elif path == "/api/login":
                st, res = h_login(con, data)
            elif path == "/api/password-reset/request":
                st, res = h_password_reset_request(con, data)
            elif path == "/api/password-reset/confirm":
                st, res = h_password_reset_confirm(con, data)
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

        search_log_csv_match = _SEARCH_LOG_CSV_PATH_RE.match(u.path)
        search_log_detail_match = _SEARCH_LOG_DETAIL_PATH_RE.match(u.path)
        if u.path == "/api/tenant/search-log" or search_log_csv_match or search_log_detail_match:
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if u.path == "/api/tenant/search-log":
                    st, res = h_tenant_search_log_list(con, tenant["id"])
                elif search_log_csv_match:
                    st, res = h_tenant_search_log_csv(con, tenant["id"], int(search_log_csv_match.group(1)))
                else:
                    st, res = h_tenant_search_log_detail(con, tenant["id"], int(search_log_detail_match.group(1)))
                return self._json(st, res)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:200]})
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
                or u.path == "/api/tenant/send-log/csv"
                or u.path == "/api/tenant/send-log/executions"
                or u.path == "/api/tenant/autofill/pending"
                or u.path == "/api/tenant/scheduled-sends"
                or u.path == "/api/tenant/exclusions"
                or u.path == "/api/tenant/companies/search"
                or u.path == "/api/tenant/templates"
                or u.path == "/api/tenant/sender-templates"
                or u.path == "/api/tenant/staff"
                or u.path == "/api/tenant/staff/pending"
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
                elif u.path == "/api/tenant/send-log/csv":
                    st, res = h_tenant_send_log_csv(con, tenant["id"], qs)
                elif u.path == "/api/tenant/send-log/executions":
                    st, res = h_tenant_send_log_executions(con, tenant["id"], qs)
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
                elif u.path == "/api/tenant/staff/pending":
                    st, res = h_tenant_staff_pending(con, tenant["id"])
                elif u.path == "/api/tenant/announcements":
                    st, res = h_tenant_announcements_list(con)
                elif u.path == "/api/tenant/activity-log":
                    st, res = h_tenant_activity_log(con, tenant["id"], qs)
                elif u.path == "/api/tenant/kill-switch":
                    st, res = h_tenant_kill_switch_status(con, tenant["id"])
                elif u.path == "/api/tenant/dashboard":
                    st, res = h_tenant_dashboard(con, tenant["id"])
                elif u.path == "/api/tenant/lists":
                    st, res = h_tenant_lists_list(con, tenant["id"], qs)
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
            if u.path.startswith("/track/click/"):
                token = u.path.split("/track/click/")[1]
                url = h_track_click(con, token)
                if not url:
                    return self._json(404, {"error": "このリンクは無効です"})
                self.send_response(302)
                self.send_header("Location", url)
                self.end_headers()
                return
            if u.path == "/api/optout":
                st, res = h_optout(con, {k: v[0] for k, v in qs.items()})
                return self._json(st, res)
            verify_staff_match = _VERIFY_STAFF_PATH_RE.match(u.path)
            if verify_staff_match:
                body = h_verify_staff_email(con, verify_staff_match.group(1)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            reset_password_match = _RESET_PASSWORD_PATH_RE.match(u.path)
            if reset_password_match:
                body = h_reset_password_page(reset_password_match.group(1)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
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

    print("\n── URLアクセスの記録(MIKOMERUの「URLアクセスの記録」相当) ──")
    st, loc = get("/track/click/nonexistent-token-xyz", follow=False)
    t("存在しないトークンは404", st == 404)
    track_token = db.create_click_token(con, tid, "https://example.co.jp/tracked-page")
    st, loc = get(f"/track/click/{track_token}", follow=False)
    t("有効なトークンは本来のURLへ302リダイレクト",
      st == 302 and loc == b"https://example.co.jp/tracked-page")
    t("クリックがtouches.email_click_countに記録される",
      con.execute("SELECT email_click_count FROM touches WHERE id=?", (tid,)).fetchone()[0] == 1)
    st, loc = get(f"/track/click/{track_token}", follow=False)
    t("同じトークンを2回踏むとカウントが2になる(重複クリックも記録)",
      con.execute("SELECT email_click_count FROM touches WHERE id=?", (tid,)).fetchone()[0] == 2)
    con.execute("DELETE FROM email_tracking_tokens WHERE token=?", (track_token,))
    con.execute("UPDATE touches SET email_click_count=0, email_clicked_at=NULL WHERE id=?", (tid,))
    con.commit()

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

    print("\n── 送信文章プレビュー(MIKOMERU同等。マージタグ置換の事前確認) ──")
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/preview-message",
                      {"subject": "##TO_COMPANY_NAME##様へ", "body": "いつも##FROM_FAMILY_NAME##が"},
                      token=key_b)
    t("他テナントのリストではプレビューできない(404)", st == 404)
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/preview-message",
                      {"subject": "##TO_COMPANY_NAME##様へ", "body": "いつも##FROM_FAMILY_NAME##が"},
                      token=key_a)
    t("POST .../preview-message でマージタグが置換される",
      st == 200 and r.get("sample_company") and r["sample_company"] in r.get("subject", "")
      and "##TO_COMPANY_NAME##" not in r.get("subject", ""))
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/preview-message", {"subject": "", "body": ""},
                      token=key_a)
    t("件名・本文がどちらも空だと400", st == 400)

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

    print("\n── 保存済みリスト管理(MIKOMERU同等UI: 編集/複製/個別削除/ソフト削除/復元) ──")
    # list_a_id/list_b_idは以降の送信テスト等で厳密な状態を前提にされているため、
    # ここでの破壊的操作(除外・削除)は専用のlist_c_idに対して行い、既存の流れに影響させない
    st, r = post_auth("/api/tenant/lists", {"name": "テストC_神奈川県", "filters": {"prefs": ["神奈川県"]}},
                      token=key_a)
    t("POST /api/tenant/lists: テスト用リストCを作成", st == 200 and bool(r.get("list_id")))
    list_c_id = r.get("list_id")
    member_count_before = r.get("count")  # フィルタ絞込は200件超あるため、ページングされる
                                           # GET /api/tenant/lists/<id> のmembers件数ではなく
                                           # 作成直後のcountをそのまま真の総数として使う
    t("リストCにメンバーが1件以上いる(以降のテストの前提)", isinstance(member_count_before, int) and member_count_before > 0)
    st, r_before = get_auth(f"/api/tenant/lists/{list_c_id}", token=key_a)
    some_company_id = r_before["members"][0]["id"]

    st, r = post_auth(f"/api/tenant/lists/{list_c_id}/rename", {"name": "リストC(改名)"}, token=key_a)
    t("POST /api/tenant/lists/<id>/renameでリスト名を変更できる", st == 200 and r.get("ok"))
    st, r = get_auth("/api/tenant/lists", token=key_a)
    t("変更後の名前がGET一覧に反映される",
      st == 200 and any(l["id"] == list_c_id and l["name"] == "リストC(改名)" for l in r.get("lists", [])))
    st, r = post_auth(f"/api/tenant/lists/{list_b_id}/rename", {"name": "乗っ取り"}, token=key_a)
    t("他テナントのリストはrenameできない(404)", st == 404)

    st, r = post_auth(f"/api/tenant/lists/{list_c_id}/duplicate", {"name": "リストCの複製"}, token=key_a)
    t("POST /api/tenant/lists/<id>/duplicateで複製できる",
      st == 200 and r.get("list_id") and r.get("list_id") != list_c_id)
    dup_list_id = r.get("list_id")
    t("複製されたリストの件数が元と一致する", r.get("count") == member_count_before)
    st, r = post_auth(f"/api/tenant/lists/{list_b_id}/duplicate", {"name": "乗っ取り複製"}, token=key_a)
    t("他テナントのリストはduplicateできない(404)", st == 404)

    st, r = post_auth(f"/api/tenant/lists/{list_c_id}/remove-members",
                      {"company_ids": [some_company_id]}, token=key_a)
    t("POST /api/tenant/lists/<id>/remove-membersで企業を除外できる",
      st == 200 and r.get("removed") == 1 and r.get("count") == member_count_before - 1)
    st, r = get_auth(f"/api/tenant/lists/{list_c_id}", token=key_a)
    t("除外後はメンバー一覧に出てこない", st == 200 and all(m["id"] != some_company_id for m in r.get("members", [])))
    st, r2 = get_auth("/api/tenant/lists", token=key_a)
    t("除外後は一覧のcompany_countも減る",
      st == 200 and any(l["id"] == list_c_id and l["company_count"] == member_count_before - 1
                         for l in r2.get("lists", [])))
    st, r = post_auth(f"/api/tenant/lists/{list_c_id}/remove-members",
                      {"company_ids": [some_company_id]}, token=key_b)
    t("他テナントのリストからは除外できない(removed=0)", st == 200 and r.get("removed") == 0)

    print("\n── リスト内の会社名検索・企業情報の編集(T26) ──")
    st, r = get_auth(f"/api/tenant/lists/{list_c_id}", token=key_a)
    shared_master_company_id = r["members"][0]["id"]
    q_target_name = r["members"][0]["name"]
    t("全社共有マスタの企業はeditable=false", r["members"][0]["editable"] is False)
    st, r = get_auth(f"/api/tenant/lists/{list_c_id}?q=" + urllib.parse.quote(q_target_name[:4]), token=key_a)
    t("GET .../lists/<id>?q=で会社名の部分一致検索ができる",
      st == 200 and any(m["id"] == shared_master_company_id for m in r["members"]))
    st, r = get_auth(f"/api/tenant/lists/{list_c_id}?q=" + urllib.parse.quote("絶対に一致しない架空の社名XYZ"),
                     token=key_a)
    t("該当しないqだと0件になる", st == 200 and len(r["members"]) == 0)

    st, r = post_auth(f"/api/tenant/lists/{list_c_id}/members/{shared_master_company_id}",
                      {"contact_url": "https://hijack.example.com/"}, token=key_a)
    t("全社共有マスタの企業は編集できない(400)", st == 400)

    csv_text_own = "会社名,URL\nT26編集テスト株式会社,https://t26-before.example.co.jp/\n"
    st, r = post_auth("/api/tenant/lists/csv",
                      {"name": "T26編集テスト用リスト", "csv": csv_text_own}, token=key_a)
    t("編集テスト用に自社の非公開企業をCSVで1件追加", st == 200 and r.get("new_companies") == 1)
    own_list_id = r["list_id"]
    st, r = get_auth(f"/api/tenant/lists/{own_list_id}", token=key_a)
    own_company_id = r["members"][0]["id"]
    t("追加した企業はeditable=true(自テナントの非公開データ)", r["members"][0]["editable"] is True)

    st, r = post_auth(f"/api/tenant/lists/{own_list_id}/members/{own_company_id}",
                      {"name": "", "contact_url": "https://t26-after.example.co.jp/"}, token=key_a)
    t("会社名を空にしようとすると400", st == 400)
    st, r = post_auth(f"/api/tenant/lists/{own_list_id}/members/{own_company_id}", {}, token=key_a)
    t("更新項目が無いと400", st == 400)
    st, r = post_auth(f"/api/tenant/lists/{own_list_id}/members/{own_company_id}",
                      {"contact_url": "https://t26-after.example.co.jp/", "phone": "03-9999-0000"},
                      token=key_a)
    t("自社の非公開企業は編集できる",
      st == 200 and r["company"]["contact_url"] == "https://t26-after.example.co.jp/"
      and r["company"]["phone"] == "03-9999-0000")
    st, r = get_auth(f"/api/tenant/lists/{own_list_id}", token=key_a)
    t("編集内容がリスト表示にも反映される",
      st == 200 and r["members"][0]["contact_url"] == "https://t26-after.example.co.jp/")
    st, r = post_auth(f"/api/tenant/lists/{own_list_id}/members/{own_company_id}",
                      {"contact_url": "https://hijack.example.com/"}, token=key_b)
    t("他テナントは編集できない(404)", st == 404)
    con.execute("DELETE FROM target_list_members WHERE list_id=?", (own_list_id,))
    con.execute("DELETE FROM target_lists WHERE id=?", (own_list_id,))
    con.execute("DELETE FROM companies WHERE id=?", (own_company_id,))
    con.commit()

    st, r = post_auth("/api/tenant/lists/delete", {"list_ids": [dup_list_id]}, token=key_a)
    t("POST /api/tenant/lists/deleteでソフト削除できる", st == 200 and r.get("changed") == 1)
    st, r = get_auth("/api/tenant/lists", token=key_a)
    t("削除後は既定の一覧に出てこない", st == 200 and all(l["id"] != dup_list_id for l in r.get("lists", [])))
    st, r = get_auth("/api/tenant/lists?include_deleted=1", token=key_a)
    t("include_deleted=1で削除済みも見える(復元列相当のdeleted_atが入る)",
      st == 200 and any(l["id"] == dup_list_id and l["deleted_at"] for l in r.get("lists", [])))
    st, r = post_auth("/api/tenant/lists/delete", {"list_ids": [dup_list_id]}, token=key_b)
    t("他テナントのリストはdeleteできない(changed=0)", st == 200 and r.get("changed") == 0)
    st, r = post_auth("/api/tenant/lists/restore", {"list_ids": [dup_list_id]}, token=key_a)
    t("POST /api/tenant/lists/restoreで復元できる", st == 200 and r.get("changed") == 1)
    st, r = get_auth("/api/tenant/lists", token=key_a)
    t("復元後は既定の一覧に戻る", st == 200 and any(l["id"] == dup_list_id for l in r.get("lists", [])))

    st, r = post_auth("/api/tenant/lists",
                      {"filters": {"prefs": ["神奈川県"]}, "existing_list_id": list_c_id}, token=key_a)
    t("POST /api/tenant/listsにexisting_list_idを渡すと新規作成せず既存リストへ追加",
      st == 200 and r.get("list_id") == list_c_id)
    st, r = get_auth("/api/tenant/lists", token=key_a)
    t("既存リストへ追加すると同じ条件の絞込結果が反映される(先に除外した企業も条件に合えば戻る)",
      st == 200 and any(l["id"] == list_c_id and l["company_count"] == member_count_before
                         for l in r.get("lists", [])))

    con.execute("DELETE FROM target_list_members WHERE list_id IN (?,?)", (list_c_id, dup_list_id))
    con.execute("DELETE FROM target_lists WHERE id IN (?,?)", (list_c_id, dup_list_id))
    con.commit()

    print("\n── 送信除外設定 CSV一括登録 ──")
    ex_csv_company = con.execute("""SELECT name FROM companies
        WHERE dedup_of IS NULL AND owner_tenant_id IS NULL LIMIT 1""").fetchone()
    ex_csv_text = f"会社名\n{ex_csv_company['name']}\n存在しない架空企業名XYZ999\n"
    st, r = post_auth("/api/tenant/exclusions/csv", {"csv": ex_csv_text, "reason": "CSV一括テスト"}, token=key_a)
    t("POST /api/tenant/exclusions/csvで一括除外できる(該当1件・不一致1件)",
      st == 200 and r.get("matched") == 1 and r.get("not_found") == 1)
    st, r = get_auth("/api/tenant/exclusions", token=key_a)
    t("CSV一括除外した企業がGET /api/tenant/exclusionsに出る",
      st == 200 and any(e["name"] == ex_csv_company["name"] for e in r.get("exclusions", [])))
    st, r = post_auth("/api/tenant/exclusions/csv", {"csv": ""}, token=key_a)
    t("空のcsvは400", st == 400)
    con.execute("DELETE FROM tenant_exclusions WHERE tenant_id=? AND reason='CSV一括テスト'", (tid_a,))
    con.commit()

    print("\n── リスト取得(検索)・CSV検索・CSV検索ログ ──")
    st, r = post_auth("/api/tenant/search/filter", {"filters": {"prefs": ["東京都"]}}, token=key_a)
    t("POST /api/tenant/search/filterで検索できる(件数とサンプルが返る)",
      st == 200 and r.get("count_before_cap", 0) > 0 and len(r.get("sample", [])) > 0)
    st, r = get_auth("/api/tenant/search-log", token=key_a)
    t("フィルタ検索はsearch_logに記録されない(MIKOMERUのリスト取得側にはログが無いため)",
      st == 200 and len(r.get("logs", [])) == 0)

    sc_company = con.execute("""SELECT name, pref FROM companies
        WHERE dedup_of IS NULL AND owner_tenant_id IS NULL AND pref IS NOT NULL LIMIT 1""").fetchone()
    # 2行目は会社名列が空(=会社名を特定できない行)なので「会社不明」扱いになる。
    # 存在しない社名を入れても(AshiBaseの意図的な設計上)非公開企業として新規作成
    # されてしまい「会社不明」にはならないため、skipped_rowsを狙って再現するには
    # 会社名そのものを空にする必要がある
    csv_search_text = f"会社名,都道府県\n{sc_company['name']},{sc_company['pref']}\n,東京都\n"
    st, r = post_auth("/api/tenant/search/csv",
                      {"csv": csv_search_text, "mode": "name", "filename": "csvsearch.csv"}, token=key_a)
    t("POST /api/tenant/search/csv(会社名で検索)で検索できる",
      st == 200 and r.get("count") == 1 and r.get("skipped_rows") == 1
      and isinstance(r.get("search_log_id"), int))
    csv_search_log_id = r.get("search_log_id")

    st, r = post_auth("/api/tenant/search/csv", {"csv": csv_search_text, "mode": "url"}, token=key_a)
    t("URLで検索する場合はurl_colが必須(400)", st == 400)

    st, r = get_auth("/api/tenant/search-log", token=key_a)
    t("GET /api/tenant/search-logにCSV検索の実行が記録される",
      st == 200 and any(l["id"] == csv_search_log_id for l in r.get("logs", [])))
    st, r = get_auth(f"/api/tenant/search-log/{csv_search_log_id}", token=key_a)
    t("GET /api/tenant/search-log/<id>で企業一覧・元CSV行付きの詳細が取れる",
      st == 200 and len(r.get("companies", [])) == 1 and len(r.get("csv_rows", [])) == 2)
    st, r = get_auth(f"/api/tenant/search-log/{csv_search_log_id}", token=key_b)
    t("他テナントの検索ログは見えない(404)", st == 404)

    st, r = post_auth(f"/api/tenant/search-log/{csv_search_log_id}/save-as-list",
                      {"name": "CSV検索ログからの保存テスト"}, token=key_a)
    t("POST /api/tenant/search-log/<id>/save-as-listでリスト保存できる",
      st == 200 and r.get("count") == 1 and isinstance(r.get("list_id"), int))
    search_log_list_id = r.get("list_id")
    st, r = post_auth(f"/api/tenant/search-log/{csv_search_log_id}/save-as-list", {}, token=key_a)
    t("nameもexisting_list_idも無いと400", st == 400)
    st, r = post_auth(f"/api/tenant/search-log/{csv_search_log_id}/save-as-list",
                      {"name": "乗っ取り"}, token=key_b)
    t("他テナントの検索ログは保存できない(404)", st == 404)

    st, r = get_auth(f"/api/tenant/search-log/{csv_search_log_id}/csv", token=key_a)
    t("GET /api/tenant/search-log/<id>/csvでダウンロードできる",
      st == 200 and "csv" in r and sc_company["name"] in r["csv"])

    con.execute("DELETE FROM target_list_members WHERE list_id=?", (search_log_list_id,))
    con.execute("DELETE FROM target_lists WHERE id=?", (search_log_list_id,))
    con.execute("DELETE FROM search_log WHERE tenant_id IN (?,?)", (tid_a, tid_b))
    con.commit()

    print("\n── 自動送信のsender_template_id(MIKOMERUの「送信元テンプレートから選択」相当) ──")
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/send",
                      {"subject": "x", "body": "y", "sender_template_id": "not-an-int"}, token=key_a)
    t("sender_template_idが整数でないと400", st == 400)
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/send",
                      {"subject": "x", "body": "y", "sender_template_id": 999999999}, token=key_a)
    t("存在しないsender_template_idは404", st == 404)

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

    print("\n── 自動送信の新パラメータ(T23: 営業拒否バイパス/送信元上書き/過去送信対象キャンセル) ──")
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/send",
                      {"subject": "x", "body": "y", "allow_no_solicit": True}, token=key_a)
    t("allow_no_solicit:trueを指定しても送信自体は正常に受け付けられる", st == 200)

    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/send",
                      {"subject": "x", "body": "y", "sender_override": "not-an-object"}, token=key_a)
    t("sender_overrideがオブジェクトでないと400", st == 400)
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/send",
                      {"subject": "x", "body": "y", "sender_override": {"department": 123}}, token=key_a)
    t("sender_overrideの値が文字列でないと400", st == 400)
    st, r = post_auth(f"/api/tenant/lists/{list_a_id}/send",
                      {"subject": "x", "body": "y",
                       "sender_override": {"department": "工事部", "unknown_key": "無視される"}},
                      token=key_a)
    t("sender_overrideの未知のキーは無視され、既知のキーだけで正常に受け付けられる", st == 200)

    for bad in (-1, 0, "30", True):
        st, r = post_auth(f"/api/tenant/lists/{list_a_id}/send",
                          {"subject": "x", "body": "y", "cancel_recent_days": bad}, token=key_a)
        t(f"cancel_recent_days={bad!r}は正の整数でないと400", st == 400)

    # cancel_recent_days: 直近に実送信済みの会社が今回の送信対象から除外されることを確認。
    # 実企業(id=1等)は他のテストが残した過去のtouches(古いnote等)を持つ場合があり、
    # 「直近に送信済みか」の判定が汚染されるため、専用の合成企業を使って確実にクリーンな
    # 状態から検証する(senders.py testの company_id=999997 と同じ考え方)。
    con.execute("DELETE FROM touches WHERE company_id IN (999995, 999996)")
    con.execute("DELETE FROM companies WHERE id IN (999995, 999996)")
    con.execute("""INSERT INTO companies (id, name, contact_url) VALUES
        (999995, 'テスト_cancel_recent_days_A', 'https://example.co.jp/contact/'),
        (999996, 'テスト_cancel_recent_days_B', 'https://example.co.jp/contact/')""")
    con.commit()
    cr_companies = [999995, 999996]
    now_cr = datetime.now().isoformat(timespec="seconds")
    cur = con.execute("""INSERT INTO target_lists (tenant_id,name,source,company_count,created_at)
        VALUES (?,?,?,?,?)""", (tid_a, "テストA_cancel_recent_days", "filter", 2, now_cr))
    cr_list_id = cur.lastrowid
    con.executemany("""INSERT INTO target_list_members (list_id, company_id, send_status,
        created_at, updated_at) VALUES (?,?,'PENDING',?,?)""",
        [(cr_list_id, cid, now_cr, now_cr) for cid in cr_companies])
    # 1社は「直近に実送信済み」として下ごしらえする(別campaignでの過去の送信を模す)。
    # note に provider_id=mock_ を含めない=ドライランではなく実送信だったという印
    cur2 = con.execute("""INSERT INTO campaigns (name, started_at, target_rule, offer_id)
        VALUES (?,?,?,?)""", ("cancel_recent_days下ごしらえ", now_cr, "1=0",
                               con.execute("SELECT id FROM offers WHERE tenant_id=?",
                                           (tid_a,)).fetchone()[0]))
    prior_campaign_id = cur2.lastrowid
    con.execute("""INSERT INTO touches (campaign_id, company_id, channel, variant, step,
        subject, body, sent_at, note) VALUES (?,?,'フォーム','A',1,'過去','過去',?,'provider_id=form_prior')""",
        (prior_campaign_id, cr_companies[0], now_cr))
    con.commit()

    res_cr = TL.send_list(con, tid_a, cr_list_id, "件名", "本文", dry_run=True, cancel_recent_days=30)
    t("cancel_recent_days指定時、直近実送信済みの1社が対象から除外される",
      res_cr is not None and "error" not in res_cr
      and res_cr.get("target_count") == 1 and res_cr.get("cancelled_recent") == 1)

    res_cr_off = TL.send_list(con, tid_a, cr_list_id, "件名", "本文", dry_run=True)
    t("cancel_recent_days未指定なら従来通り2社とも対象のまま",
      res_cr_off is not None and "error" not in res_cr_off
      and res_cr_off.get("target_count") == 2 and res_cr_off.get("cancelled_recent") == 0)

    con.execute("DELETE FROM touches WHERE campaign_id IN (?,?)",
                 (prior_campaign_id, res_cr["campaign_id"]))
    con.execute("DELETE FROM campaigns WHERE id IN (?,?)", (prior_campaign_id, res_cr["campaign_id"]))
    con.execute("DELETE FROM target_list_members WHERE list_id=?", (cr_list_id,))
    con.execute("DELETE FROM target_lists WHERE id=?", (cr_list_id,))
    con.execute("DELETE FROM touches WHERE company_id IN (999995, 999996)")
    con.execute("DELETE FROM companies WHERE id IN (999995, 999996)")
    con.commit()

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
                      {"subject": "予約テスト", "body": "本文", "scheduled_at": future_at,
                       "track_clicks": True}, token=key_a)
    t("未来の日時なら予約登録され、即時送信はされない",
      st == 200 and r.get("scheduled") is True and isinstance(r.get("scheduled_id"), int))
    scheduled_id = r.get("scheduled_id")
    st, r = get_auth("/api/tenant/scheduled-sends", token=key_a)
    t("track_clicks:trueで予約すると一覧にも反映される",
      st == 200 and any(s["id"] == scheduled_id and s["track_clicks"] == 1
                         for s in r.get("scheduled", [])))

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

    print("\n── 送信ログの備考・手動送信済み(MIKOMERU同等) ──")
    st, r = post_auth(f"/api/tenant/send-log/{log_id}/note", {"note": "架電済み"}, token=key_b)
    t("他テナントの送信ログの備考は更新できない(404)", st == 404)
    st, r = post_auth(f"/api/tenant/send-log/{log_id}/note", {"note": "架電済み"}, token=key_a)
    t("POST .../note で備考を更新できる", st == 200 and r.get("ok"))
    st, r = get_auth("/api/tenant/send-log", token=key_a)
    t("更新した備考がGETに反映される",
      st == 200 and next(x for x in r["log"] if x["id"] == log_id)["note"] == "架電済み")
    st, r = post_auth(f"/api/tenant/send-log/{log_id}/note", {"note": ""}, token=key_a)
    t("空文字で備考をクリアできる", st == 200 and r.get("ok"))
    st, r = get_auth("/api/tenant/send-log", token=key_a)
    t("クリア後はnoteがNoneになる",
      st == 200 and next(x for x in r["log"] if x["id"] == log_id)["note"] is None)

    st, r = post_auth(f"/api/tenant/send-log/{log_id}/manual-sent", {"manual_sent": True},
                      token=key_b)
    t("他テナントの送信ログは手動送信済みにできない(404)", st == 404)
    st, r = post_auth(f"/api/tenant/send-log/{log_id}/manual-sent", {"manual_sent": True},
                      token=key_a)
    t("POST .../manual-sent で手動送信済みにできる", st == 200 and r.get("ok"))
    st, r = get_auth("/api/tenant/send-log", token=key_a)
    t("手動送信済みがGETに反映される(manual_sent_atが立つ)",
      st == 200 and next(x for x in r["log"] if x["id"] == log_id)["manual_sent_at"] is not None)
    st, r = post_auth(f"/api/tenant/send-log/{log_id}/manual-sent", {"manual_sent": False},
                      token=key_a)
    t("manual_sent=falseで取り消せる", st == 200 and r.get("ok"))
    st, r = get_auth("/api/tenant/send-log", token=key_a)
    t("取り消し後はmanual_sent_atがNoneに戻る",
      st == 200 and next(x for x in r["log"] if x["id"] == log_id)["manual_sent_at"] is None)

    st, r = get_auth("/api/tenant/send-log/csv")
    t("認証ヘッダなしのGET /api/tenant/send-log/csvは401", st == 401)
    st, r = get_auth("/api/tenant/send-log/csv", token=key_a)
    t("GET /api/tenant/send-log/csv でCSVが取れる",
      st == 200 and "csv" in r and "ID,会社名" in r["csv"] and str(log_id) in r["csv"])

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

    print("\n── 自動送信ログ一覧(実行単位の集計。T22, MIKOMERUの「自動送信ログ」一覧相当) ──")
    t22_staff_id, t22_staff_key = OF.add_staff(con, tid_a, "T22担当者", email=None)
    t22_company = con.execute(
        "SELECT id FROM companies WHERE contact_url IS NOT NULL AND dedup_of IS NULL LIMIT 1").fetchone()[0]
    now_t22 = datetime.now().isoformat(timespec="seconds")
    cur = con.execute("""INSERT INTO target_lists (tenant_id,name,source,company_count,created_at)
        VALUES (?,?,?,?,?)""", (tid_a, "T22実行テスト用リスト", "filter", 1, now_t22))
    t22_list_id = cur.lastrowid
    con.execute("""INSERT INTO target_list_members (list_id, company_id, send_status, created_at, updated_at)
        VALUES (?,?,'PENDING',?,?)""", (t22_list_id, t22_company, now_t22, now_t22))
    con.commit()

    st, r = post_auth(f"/api/tenant/lists/{t22_list_id}/send",
                      {"subject": "T22件名", "body": "T22本文"}, token=t22_staff_key)
    t("担当者キーでも送信でき、campaign_idが作られる", st == 200 and isinstance(r.get("campaign_id"), int))
    t22_campaign_id = r.get("campaign_id")
    t("send_list()実行後、target_listsに担当者IDがスナップショットされる",
      con.execute("SELECT sent_by_staff_id FROM target_lists WHERE id=?",
                  (t22_list_id,)).fetchone()[0] == t22_staff_id)

    # dry_runはform_send_logへ書かないため(前述の「自動送信ログ」節と同じ理由)、
    # 集計対象のform_send_log/クリック数は手で補う
    con.execute("""INSERT INTO form_send_log (company_id, tenant_id, list_id, target_url, started_at,
        status, reason_code) VALUES (?,?,?,'https://example.co.jp',?,'SUCCESS','success_text_matched')""",
        (t22_company, tid_a, t22_list_id, now_t22))
    con.execute("""INSERT INTO form_send_log (company_id, tenant_id, list_id, target_url, started_at,
        status, reason_code) VALUES (?,?,?,'https://example.co.jp',?,'FAILED_UNSUPPORTED','form_not_found')""",
        (t22_company, tid_a, t22_list_id, now_t22))
    con.execute("UPDATE touches SET email_click_count=3, email_clicked_at=? WHERE campaign_id=?",
                (now_t22, t22_campaign_id))
    con.commit()

    st, r = get_auth("/api/tenant/send-log/executions")
    t("認証ヘッダなしのGET .../executionsは401", st == 401)

    st, r = get_auth(f"/api/tenant/send-log/executions?list_id={t22_list_id}", token=key_a)
    t("GET .../executions?list_id=で1件の実行に絞り込める",
      st == 200 and len(r.get("executions", [])) == 1)
    ex = r["executions"][0]

    st, r = get_auth("/api/tenant/send-log/executions?limit=0", token=key_a)
    t("GET .../executions?limit=0はlimit指定なし扱いで全件返す(ホームの直近表示用パラメータ)",
      st == 200 and len(r.get("executions", [])) >= 1)
    st, r = get_auth("/api/tenant/send-log/executions?limit=1", token=key_a)
    t("GET .../executions?limit=1で直近1件のみ返す(T24: ホームの最近の営業履歴と同じ絞り込み)",
      st == 200 and len(r.get("executions", [])) == 1)
    t("担当者名が反映される", ex["staff_name"] == "T22担当者")
    t("会社名(テナント名)が反映される", ex["company_name"] == "test-tenant-A")
    t("送信元テンプレート未指定時はテナントのsender_nameから姓を補う",
      ex["sender_last_name"] == "test-tenant-A" and ex["sender_email"] == "a@example.co.jp")
    t("送信文章(件名)が反映される", ex["subject"] == "T22件名")
    t("成功/失敗/フォームなし/総数が正しく集計される",
      ex["success"] == 1 and ex["no_form"] == 1 and ex["failed"] == 0 and ex["total"] == 2)
    t("URLクリック数が集計される", ex["click_count"] == 3)
    t("最新クリック日時が反映される", ex["last_clicked_at"] == now_t22)

    st, r = get_auth(f"/api/tenant/send-log/executions?list_id={t22_list_id}", token=key_b)
    t("他テナントからは見えない(0件)", st == 200 and len(r.get("executions", [])) == 0)

    st, r = post_auth(f"/api/tenant/send-log/executions/{t22_list_id}/note",
                      {"note": "テスト実行メモ"}, token=key_b)
    t("他テナントは実行の備考を更新できない(404)", st == 404)
    st, r = post_auth(f"/api/tenant/send-log/executions/{t22_list_id}/note",
                      {"note": "テスト実行メモ"}, token=key_a)
    t("POST .../executions/<list_id>/note で実行単位の備考を更新できる", st == 200 and r.get("ok"))
    st, r = get_auth(f"/api/tenant/send-log/executions?list_id={t22_list_id}", token=key_a)
    t("更新した備考が反映される", st == 200 and r["executions"][0]["send_note"] == "テスト実行メモ")

    st, r = get_auth(f"/api/tenant/send-log?list_id={t22_list_id}", token=key_a)
    t("GET /api/tenant/send-log?list_id=で会社別の明細(詳細ページ用)に絞り込める",
      st == 200 and len(r.get("log", [])) == 2)

    con.execute("DELETE FROM form_send_log WHERE list_id=?", (t22_list_id,))
    con.execute("DELETE FROM touches WHERE campaign_id=?", (t22_campaign_id,))
    con.execute("DELETE FROM campaigns WHERE id=?", (t22_campaign_id,))
    con.execute("DELETE FROM target_list_members WHERE list_id=?", (t22_list_id,))
    con.execute("DELETE FROM target_lists WHERE id=?", (t22_list_id,))
    con.execute("DELETE FROM staff WHERE id=?", (t22_staff_id,))
    con.commit()

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
    # list_a_idは既にdry_run送信済みだが、送信上限等により全社が送信対象になるとは
    # 限らないため、実際にtouchesが入っている(=文面を復元できる)会社を選ぶ
    # (ORDER BY無しのLIMIT 1はSQLiteとPostgresで返る行が異なりうるため、
    # touchesとJOINして「送信済みの1社」を確実に選ぶ)
    af_company = con.execute("""SELECT c.id FROM target_list_members m
        JOIN companies c ON c.id=m.company_id
        JOIN touches t ON t.company_id=c.id AND t.campaign_id=(
            SELECT campaign_id FROM target_lists WHERE id=?)
        WHERE m.list_id=? LIMIT 1""", (list_a_id, list_a_id)).fetchone()
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

    st, r = post_auth("/api/tenant/templates/update",
                      {"template_id": tmpl_id, "name": "初回案内", "subject": "件名(編集後)",
                       "body": "本文(編集後)"}, token=key_b)
    t("他テナントのテンプレートは編集できない(404)", st == 404)
    st, r = post_auth("/api/tenant/templates/update",
                      {"template_id": tmpl_id, "name": "初回案内", "subject": "件名(編集後)",
                       "body": "本文(編集後)"}, token=key_a)
    t("POST /api/tenant/templates/update で編集できる(T39。従来は削除して作り直すしか無かった)",
      st == 200 and r.get("ok"))
    st, r = get_auth("/api/tenant/templates", token=key_a)
    t("編集後の内容がGETで返る",
      st == 200 and any(x["id"] == tmpl_id and x["subject"] == "件名(編集後)"
                         and x["body"] == "本文(編集後)" for x in r.get("templates", [])))
    st, r = post_auth("/api/tenant/templates/update",
                      {"template_id": tmpl_id, "name": "", "subject": "x", "body": "y"}, token=key_a)
    t("nameが空だと400", st == 400)

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

    st, r = post_auth("/api/tenant/sender-templates",
                      {"name": "支社", "sender_name": "テスト株式会社 支社",
                       "sender_email": "branch@test-a.example.co.jp",
                       "prefecture": "大阪府", "city": "大阪市中央区",
                       "block": "1-2-3", "building": "支社ビル4F", "phone": "06-1234-5678"},
                      token=key_a)
    t("POST /api/tenant/sender-templates で都道府県/市区町村/丁目番地/ビル名/電話番号も保存",
      st == 200 and bool(r.get("template_id")))
    stmpl_id2 = r.get("template_id")
    st, r = get_auth("/api/tenant/sender-templates", token=key_a)
    tmpl2 = next((x for x in r.get("templates", []) if x["id"] == stmpl_id2), None)
    t("GET /api/tenant/sender-templates に住所構造化項目が出る",
      tmpl2 is not None and tmpl2["sender_prefecture"] == "大阪府"
      and tmpl2["sender_city"] == "大阪市中央区" and tmpl2["sender_block"] == "1-2-3"
      and tmpl2["sender_building"] == "支社ビル4F" and tmpl2["sender_phone"] == "06-1234-5678")
    st, r = post_auth("/api/tenant/sender-templates/activate", {"template_id": stmpl_id2}, token=key_a)
    t("支社テンプレートも有効化できる", st == 200 and r.get("ok"))
    tenant_row2 = con.execute("SELECT sender_prefecture, sender_city, sender_block, "
                               "sender_building, sender_phone FROM tenants WHERE id=?",
                               (tid_a,)).fetchone()
    t("有効化すると住所構造化項目・電話番号もtenants.sender_*へ反映される",
      tenant_row2["sender_prefecture"] == "大阪府" and tenant_row2["sender_city"] == "大阪市中央区"
      and tenant_row2["sender_phone"] == "06-1234-5678")
    st, r = post_auth("/api/tenant/sender-templates/delete", {"template_id": stmpl_id2}, token=key_a)
    t("支社テンプレートの削除", st == 200 and r.get("ok"))
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

    st, r = post_auth("/api/tenant/sender-templates/update",
                      {"template_id": stmpl_id, "name": "本社(編集後)",
                       "sender_name": "テスト株式会社 編集後部署",
                       "sender_email": "sales@test-a.example.co.jp"}, token=key_b)
    t("他テナントの送信元テンプレートは編集できない(404)", st == 404)
    st, r = post_auth("/api/tenant/sender-templates/update",
                      {"template_id": stmpl_id, "name": "本社(編集後)",
                       "sender_name": "テスト株式会社 編集後部署",
                       "sender_email": "sales@test-a.example.co.jp",
                       "sender_address": "東京都千代田区9-9-9"}, token=key_a)
    t("POST /api/tenant/sender-templates/update で編集できる(T39)", st == 200 and r.get("ok"))
    st, r = get_auth("/api/tenant/sender-templates", token=key_a)
    edited = next((x for x in r.get("templates", []) if x["id"] == stmpl_id), None)
    t("編集後の内容がGETで返る",
      edited is not None and edited["name"] == "本社(編集後)"
      and edited["sender_name"] == "テスト株式会社 編集後部署"
      and edited["sender_address"] == "東京都千代田区9-9-9")

    tenant_row_after_edit = con.execute("SELECT sender_name FROM tenants WHERE id=?", (tid_a,)).fetchone()
    t("既に有効化済みのテンプレートを編集しても、tenants側へは自動反映されない"
      "(activate_sender_template()は1回だけのコピーのため)",
      tenant_row_after_edit["sender_name"] != "テスト株式会社 編集後部署")

    st, r = post_auth("/api/tenant/sender-templates/activate", {"template_id": stmpl_id}, token=key_a)
    tenant_row_reactivated = con.execute("SELECT sender_name FROM tenants WHERE id=?", (tid_a,)).fetchone()
    t("編集後に改めて有効化すると、編集後の内容が反映される",
      st == 200 and tenant_row_reactivated["sender_name"] == "テスト株式会社 編集後部署")

    st, r = post_auth("/api/tenant/sender-templates/update",
                      {"template_id": stmpl_id, "name": "", "sender_name": "x",
                       "sender_email": "y@example.co.jp"}, token=key_a)
    t("nameが空だと400", st == 400)

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

    print("\n── 送信完了通知(MIKOMERU同等。宛先解決のみ検証。実送信の中身自体はsenders.py test参照) ──")
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

    print("\n── 担当者登録・メール認証・ログイン(T21, MIKOMERUの「担当者を登録する」相当) ──")
    st, r = post_auth("/api/tenant/staff/register",
                      {"email": "t21@test-a.example.co.jp", "password": "pass1234"}, token=key_a)
    t("nameが無いと400", st == 400)
    st, r = post_auth("/api/tenant/staff/register",
                      {"name": "T21太郎", "email": "not-an-email", "password": "pass1234"}, token=key_a)
    t("メール形式が不正だと400", st == 400)
    st, r = post_auth("/api/tenant/staff/register",
                      {"name": "T21太郎", "email": "t21@test-a.example.co.jp", "password": "short"},
                      token=key_a)
    t("パスワードが短いと400", st == 400)

    st, r = post_auth("/api/tenant/staff/register",
                      {"name": "T21太郎", "email": "t21@test-a.example.co.jp",
                       "password": "pass1234", "role": "管理者"}, token=key_a)
    t("POST /api/tenant/staff/register で登録でき、SENDGRID_API_KEY未設定時は"
      "email_sent=falseでverify_pathが返る(T33)",
      st == 200 and bool(r.get("staff_id")) and r.get("email_sent") is False
      and r.get("verify_path", "").startswith("/verify/staff/"))
    t21_staff_id = r.get("staff_id")
    t21_verify_path = r.get("verify_path")

    print("\n── 担当者認証メールの実送信(T33。SENDGRID_API_KEYが有効な場合の挙動をモックで検証) ──")
    t33_sent_to = []

    def _fake_verify_deliver(self, to, sender, subject, body):
        t33_sent_to.append(to.email)
        assert "/verify/staff/" in body, "メール本文に認証URLが含まれていない"
        return _senders_mod.SendResult(ok=True, provider_id="fake-msg")

    _senders_mod.MailSender._deliver = _fake_verify_deliver
    try:
        st, r = post_auth("/api/tenant/staff/register",
                          {"name": "T33花子", "email": "t33@test-a.example.co.jp",
                           "password": "pass1234"}, token=key_a)
        t("メール送信が成功するとemail_sent=trueで、verify_pathは応答に含まれない",
          st == 200 and r.get("email_sent") is True and "verify_path" not in r
          and t33_sent_to == ["t33@test-a.example.co.jp"])
        t33_staff_id = r.get("staff_id")

        t33_sent_to.clear()
        st, r = post_auth("/api/tenant/staff/resend", {"staff_id": t33_staff_id}, token=key_a)
        t("POST /api/tenant/staff/resendも同様にメール送信を試み、成功時はverify_pathを含まない",
          st == 200 and r.get("email_sent") is True and "verify_path" not in r
          and t33_sent_to == ["t33@test-a.example.co.jp"])
    finally:
        _senders_mod.MailSender._deliver = orig_deliver
        con.execute("DELETE FROM staff WHERE email='t33@test-a.example.co.jp'")
        con.commit()

    st, r = post_auth("/api/tenant/staff/register",
                      {"name": "別名", "email": "t21@test-a.example.co.jp", "password": "pass1234"},
                      token=key_a)
    t("同じメールアドレスで再登録するとエラー", st == 400 and "既に登録" in r.get("error", ""))

    st, r = get_auth("/api/tenant/staff/pending", token=key_a)
    t("承認待ち一覧に出てくる",
      st == 200 and any(s["id"] == t21_staff_id and s["name"] == "T21太郎" for s in r.get("pending", [])))
    st, r = get_auth("/api/tenant/staff/pending", token=key_b)
    t("承認待ち一覧もテナント分離される",
      st == 200 and all(s["id"] != t21_staff_id for s in r.get("pending", [])))

    st, r = get_auth("/api/tenant/staff", token=key_a)
    t("未認証の担当者は通常の一覧には出てこない",
      st == 200 and all(s["id"] != t21_staff_id for s in r.get("staff", [])))

    st, r = post("/api/login", {"email": "t21@test-a.example.co.jp", "password": "pass1234"})
    t("メール未認証だとログインできない(401)", st == 401 and "メール認証" in r.get("error", ""))

    st, r = post("/api/login", {"email": "t21@test-a.example.co.jp", "password": "wrong-password"})
    t("パスワードが違うとログインできない(401)", st == 401)

    st, body = get(t21_verify_path)
    t("GET /verify/staff/<token> で認証完了ページが返る",
      st == 200 and "認証完了".encode() in body)

    st, r = post("/api/login", {"email": "t21@test-a.example.co.jp", "password": "pass1234"})
    t("メール認証後はログインできる",
      st == 200 and r.get("api_key", "").startswith("tk_") and r.get("tenant_name")
      and r.get("staff_name") == "T21太郎")
    t21_login_key = r.get("api_key")

    st, r = get_auth("/api/tenant/lists", token=t21_login_key)
    t("ログインで得たapi_keyでも通常のテナントAPIが使える", st == 200)

    st, r = get_auth("/api/tenant/staff", token=key_a)
    t("メール認証後は通常の担当者一覧に出てくる",
      st == 200 and any(s["id"] == t21_staff_id and s.get("role") == "管理者" for s in r.get("staff", [])))
    st, r = get_auth("/api/tenant/staff/pending", token=key_a)
    t("認証後は承認待ち一覧から消える", st == 200 and all(s["id"] != t21_staff_id for s in r.get("pending", [])))

    st, body = get(t21_verify_path)
    t("同じトークンを再度開くと認証エラーになる(使い捨て)",
      st == 200 and "認証エラー".encode() in body)

    st, r = post_auth("/api/tenant/staff/register",
                      {"name": "T21花子", "email": "t21b@test-a.example.co.jp", "password": "pass1234"},
                      token=key_a)
    t21b_staff_id = r.get("staff_id")
    st, r = post_auth("/api/tenant/staff/resend", {"staff_id": t21b_staff_id}, token=key_b)
    t("他テナントは再発行できない(404)", st == 404)
    st, r = post_auth("/api/tenant/staff/resend", {"staff_id": t21b_staff_id}, token=key_a)
    t("POST /api/tenant/staff/resend で新しいverify_pathが返る",
      st == 200 and r.get("verify_path", "").startswith("/verify/staff/"))
    st, body = get(r.get("verify_path"))
    t("再発行後のリンクでも認証できる", st == 200 and "認証完了".encode() in body)
    st, r = post_auth("/api/tenant/staff/resend", {"staff_id": t21b_staff_id}, token=key_a)
    t("認証済みの担当者を再発行しようとすると404", st == 404)

    print("\n── パスワードリセット(T34, MIKOMERUの「パスワードをお忘れの方」相当) ──")
    st, r = post("/api/password-reset/request", {"email": "not-registered@test-a.example.co.jp"})
    t("未登録のメールアドレスでも200で汎用メッセージが返る(メールアドレス列挙攻撃対策)",
      st == 200 and r.get("ok") is True and "verify_path" not in r and "reset_path" not in r
      and "token" not in json.dumps(r))

    t34_captured = []

    def _fake_reset_deliver(self, to, sender, subject, body):
        t34_captured.append((to.email, body))
        return _senders_mod.SendResult(ok=True, provider_id="fake-msg")

    _senders_mod.MailSender._deliver = _fake_reset_deliver
    try:
        st, r = post("/api/password-reset/request", {"email": "t21@test-a.example.co.jp"})
        t("登録済み・認証済みのメールアドレスなら同じ200応答で、実際にメールが送られる",
          st == 200 and r.get("ok") is True and "reset_path" not in r
          and len(t34_captured) == 1 and t34_captured[0][0] == "t21@test-a.example.co.jp")
        reset_body = t34_captured[0][1]
        t34_reset_match = re.search(r"/reset-password/([A-Za-z0-9_-]+)", reset_body)
        t("メール本文にパスワード再設定URLが含まれる", bool(t34_reset_match))
        t34_reset_token = t34_reset_match.group(1)
    finally:
        _senders_mod.MailSender._deliver = orig_deliver

    st, body = get(f"/reset-password/{t34_reset_token}")
    t("GET /reset-password/<token> でフォームページが返る",
      st == 200 and "新しいパスワード".encode() in body)

    st, r = post("/api/password-reset/confirm", {"token": "no-such-token", "new_password": "newpass123"})
    t("無効なトークンでの確定は400", st == 400)

    st, r = post("/api/password-reset/confirm", {"token": t34_reset_token, "new_password": "short"})
    t("短すぎる新パスワードは400", st == 400)

    st, r = post("/api/password-reset/confirm", {"token": t34_reset_token, "new_password": "newpass123"})
    t("有効なトークン+新パスワードで確定できる", st == 200 and r.get("ok") is True)

    st, r = post("/api/login", {"email": "t21@test-a.example.co.jp", "password": "pass1234"})
    t("リセット後は旧パスワードでログインできなくなる", st == 401)
    st, r = post("/api/login", {"email": "t21@test-a.example.co.jp", "password": "newpass123"})
    t("リセット後は新パスワードでログインできる",
      st == 200 and r.get("api_key", "").startswith("tk_"))

    st, r = post("/api/password-reset/confirm", {"token": t34_reset_token, "new_password": "anotherpass1"})
    t("使用済みのトークンは再利用できない(使い捨て)", st == 400)

    st, r = post_auth("/api/tenant/staff",
                      {"name": "従来方式担当者", "email": "legacy@test-a.example.co.jp"}, token=key_a)
    t("従来方式(パスワード無し即時追加)は引き続き動く(回帰確認)",
      st == 200 and r.get("api_key", "").startswith("tk_"))
    st, r2 = get_auth("/api/tenant/lists", token=r.get("api_key"))
    t("従来方式で追加したapi_keyは認証不要で即使える(後方互換)", st == 200)

    con.execute("DELETE FROM staff WHERE tenant_id IN (?,?)", (tid_a, tid_b))
    con.commit()

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

    print("\n── can_contact()のテナント別スコープ(T43) ──")
    # 共有マスタの企業を複数テナントが独立に営業する以上、Aテナントの接触履歴が
    # Bテナントの接触可否まで塞いではいけない(それぞれ別の商談関係として扱う)。
    con.execute("DELETE FROM tenants WHERE name LIKE 'test-scope-%'")
    con.commit()
    tid_scope_a, _ = OF.add_tenant(con, "test-scope-A", "sa@example.co.jp")
    tid_scope_b, _ = OF.add_tenant(con, "test-scope-B", "sb@example.co.jp")
    offer_scope_a = con.execute("SELECT id FROM offers WHERE tenant_id=?", (tid_scope_a,)).fetchone()["id"]

    comp1 = con.execute("""SELECT id FROM companies
        WHERE id NOT IN (SELECT company_id FROM suppression) AND dedup_of IS NULL
        LIMIT 1""").fetchone()["id"]
    cur = con.execute("INSERT INTO campaigns (name, started_at, target_rule, offer_id) VALUES (?,?,?,?)",
                       ("test-scope-lifetime", datetime.now().isoformat(timespec="seconds"),
                        "ALL", offer_scope_a))
    scope_cid = cur.lastrowid
    now_s = datetime.now().isoformat(timespec="seconds")
    for step in range(1, 7):  # C.MAX_LIFETIME_TOUCHES=6件、Aテナントの接触として作る
        con.execute("""INSERT INTO touches (campaign_id, company_id, channel, variant, step,
                       body, sent_at, note) VALUES (?,?,?,?,?,?,?,?)""",
                    (scope_cid, comp1, "フォーム", "A", step, "本文", now_s, "provider_id=form_x"))
    con.commit()
    allowed_a, why_a = db.can_contact(con, comp1, tenant_id=tid_scope_a)
    t("Aテナント自身の生涯接触上限には引っかかる", not allowed_a and "生涯接触上限" in why_a, f"{allowed_a}/{why_a}")
    allowed_b, why_b = db.can_contact(con, comp1, tenant_id=tid_scope_b)
    t("Aテナントの接触履歴はBテナントには影響しない(Bは送信可のまま)", allowed_b is True, f"{allowed_b}/{why_b}")
    allowed_global, why_global = db.can_contact(con, comp1)
    t("tenant_id未指定(houseエンジン)は従来通り全テナント合算で判定する",
      not allowed_global and "生涯接触上限" in why_global, f"{allowed_global}/{why_global}")

    comp2 = con.execute("""SELECT id FROM companies
        WHERE id NOT IN (SELECT company_id FROM suppression) AND dedup_of IS NULL AND id<>?
        LIMIT 1""", (comp1,)).fetchone()["id"]
    cur2 = con.execute("INSERT INTO campaigns (name, started_at, target_rule, offer_id) VALUES (?,?,?,?)",
                        ("test-scope-warm", datetime.now().isoformat(timespec="seconds"),
                         "ALL", offer_scope_a))
    scope_cid2 = cur2.lastrowid
    con.execute("""INSERT INTO touches (campaign_id, company_id, channel, variant, step,
                   body, sent_at, responded, note) VALUES (?,?,?,?,?,?,?,?,?)""",
                (scope_cid2, comp2, "フォーム", "A", 1, "本文", now_s, 1, "provider_id=form_y"))
    con.commit()
    allowed_a2, why_a2 = db.can_contact(con, comp2, tenant_id=tid_scope_a)
    t("Aテナントへの反応済み(warm)はAテナント自身の接触をブロックする",
      not allowed_a2 and "反応済み" in why_a2, f"{allowed_a2}/{why_a2}")
    allowed_b2, why_b2 = db.can_contact(con, comp2, tenant_id=tid_scope_b)
    t("Aテナントへの反応(商談化)はBテナントの新規接触を妨げない", allowed_b2 is True, f"{allowed_b2}/{why_b2}")

    con.execute("DELETE FROM touches WHERE campaign_id IN (?,?)", (scope_cid, scope_cid2))
    con.execute("DELETE FROM campaigns WHERE id IN (?,?)", (scope_cid, scope_cid2))
    con.execute("DELETE FROM offers WHERE tenant_id IN (?,?)", (tid_scope_a, tid_scope_b))
    con.execute("DELETE FROM tenants WHERE id IN (?,?)", (tid_scope_a, tid_scope_b))
    con.commit()

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
