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

  ── 送信先リスト(SaaSとして他社に販売する側。テナントごとのapi_keyで認証) ──
  POST /api/tenant/lists/preview  {"filters"} → 該当件数のプレビュー(保存しない)
  POST /api/tenant/lists          {"name","filters"} → フィルタ型リストを保存
  POST /api/tenant/lists/csv      {"name","csv"} → 顧客持込CSVを取り込む
  GET  /api/tenant/lists          自テナントのリスト一覧
  GET  /api/tenant/lists/<id>     リスト詳細(自テナントのものだけ。他社分は404)
  ※ Authorization: Bearer <tenant.api_key>。テナントIDはこのキーからサーバ側で
    解決し、リクエストボディのtenant_idは一切信用しない(offers.resolve_tenant_by_key)
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
    res = TL.create_from_csv(con, tenant_id, name, csv_text)
    if "error" in res:
        return 400, res
    return 200, res


def h_tenant_lists_list(con, tenant_id):
    return 200, {"lists": TL.list_lists(con, tenant_id)}


def h_tenant_list_detail(con, tenant_id, list_id, qs):
    limit = min(int(qs.get("limit", ["200"])[0]), 1000)
    offset = int(qs.get("offset", ["0"])[0])
    res = TL.get_list(con, tenant_id, list_id, limit=limit, offset=offset)
    if not res:
        return 404, {"error": "リストが見つかりません"}
    return 200, res


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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Signature")
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

        if path in ("/api/tenant/lists/preview", "/api/tenant/lists", "/api/tenant/lists/csv"):
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if path == "/api/tenant/lists/preview":
                    st, res = h_tenant_lists_preview(con, tenant["id"], data)
                elif path == "/api/tenant/lists":
                    st, res = h_tenant_lists_create(con, tenant["id"], data)
                else:
                    st, res = h_tenant_lists_csv(con, tenant["id"], data)
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

        if u.path in ("/api/ops/status", "/api/ops/metrics"):
            if not verify_ops_bearer(self.headers.get("Authorization")):
                return self._json(401, {"error": "unauthorized"})
            con = self._con()
            try:
                if u.path == "/api/ops/status":
                    return self._json(200, R.status_dict(con))
                campaign = qs.get("campaignId", [None])[0]
                return self._json(200, metrics.compute(con, int(campaign) if campaign else None))
            finally:
                con.close()

        if u.path == "/api/tenant/lists" or u.path.startswith("/api/tenant/lists/"):
            con = self._con()
            try:
                tenant = verify_tenant_bearer(con, self.headers.get("Authorization"))
                if not tenant:
                    return self._json(401, {"error": "unauthorized"})
                if u.path == "/api/tenant/lists":
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

    st, r = get_auth("/api/tenant/lists", token=key_a)
    t("GET /api/tenant/lists は自テナント分のみ返す",
      st == 200 and all(l["id"] != list_b_id for l in r.get("lists", [])))

    st, r = get_auth(f"/api/tenant/lists/{list_a_id}", token=key_a)
    t("GET /api/tenant/lists/<id> で自分のリストは見える", st == 200 and "members" in r)

    st, r = get_auth(f"/api/tenant/lists/{list_b_id}", token=key_a)
    t("他テナントのリストIDを指定しても404(横断閲覧できない)", st == 404)

    st, r = get_auth(f"/api/tenant/lists/{list_a_id}", token=key_b)
    t("逆方向も同様に404", st == 404)

    st, r = post_auth("/api/tenant/lists/preview", {"filters": {"prefs": ["福岡県"]}}, token=key_a)
    t("他テナントがCSVで持ち込んだ非公開企業はフィルタにも出てこない",
      st == 200 and not any(s["name"] == "テナントB専用企業" for s in r.get("sample", [])))

    con.execute("DELETE FROM target_list_members WHERE list_id IN (?,?)", (list_a_id, list_b_id))
    con.execute("DELETE FROM target_lists WHERE id IN (?,?)", (list_a_id, list_b_id))
    con.execute("DELETE FROM companies WHERE owner_tenant_id IN (?,?)", (tid_a, tid_b))
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

    srv.shutdown()
    # 後片付け
    con.execute("DELETE FROM suppression WHERE company_id=?", (cid,))
    con.execute("DELETE FROM idempotency WHERE key LIKE '%test-api%'")
    con.commit()
    print(f"\n  成功 {sum(ok)} / {len(ok)}")
    return all(ok)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8787
    if cmd == "test":
        sys.exit(0 if self_test() else 1)
    serve(port)
