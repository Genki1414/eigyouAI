"""
offers.py — オファーとテナントの分離
このエンジンは3つの立場で使われる:
  (1) 自社の他事業（AshiBase資材管理・与信・入札AI等）の契約を取る装置
  (2) 他社に売る商品（＝他社のオファーを回す）
  (3) 事業譲渡の対象（＝買い手が自社商材を流す）

いずれも「オファーが差し替わっても同じ機構が回る」ことが要件になる。
オファーが compose.py にハードコードされていると、上の3つすべてが成立しない。

構造:
  tenant（誰の営業か） 1 ── n offer（何を売るか） 1 ── n campaign（いつ誰に打つか）

オファーが持つもの:
  - 何を訴求するか（offer_text）
  - 誰に効くか（target_rule: スコア条件・業種条件）
  - いくらの商材か（price_yen → LTV計算とCAC許容上限が変わる）
  - 禁止表現（NGワード。業界や法規制で言えないこと）

  python3 offers.py init          # 標準オファーを投入
  python3 offers.py list
  python3 offers.py add --tenant 1 --name "与信スコア" --price 29800
  python3 offers.py add-tenant --name "○○建材株式会社" --sender-email info@example.co.jp
                                   # 他社に販売するテナントを追加。api_keyを1回だけ表示する
"""
import argparse
import json
import secrets
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT DEFAULT 'own',        -- own(自社) / client(販売先) / acquirer(譲渡先)
  sender_name TEXT,               -- 特定電子メール法の送信者表示（テナントごとに異なる）
  sender_email TEXT,
  sender_address TEXT,
  optout_url TEXT,
  api_key TEXT UNIQUE,            -- target_lists.py等、テナント自身が叩くAPIの認証キー
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  offer_text TEXT NOT NULL,       -- AIに渡すオファー説明
  landing_url TEXT,
  price_yen INTEGER DEFAULT 0,    -- 0=無料オファー（入口用）
  target_rule TEXT,               -- SQL条件断片。誰に打つか
  ng_words TEXT,                  -- JSON配列。使ってはいけない表現
  active INTEGER DEFAULT 1,
  created_at TEXT,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);

-- テナント配下の担当者。1つのapi_keyをテナント全体で使い回すのではなく、
-- 担当者ごとに個別のapi_keyを発行できるようにする(退職時に個別失効できる)。
-- 認証で解決されるtenant_idはどの担当者でも同じ(=データはテナント単位で共有)。
CREATE TABLE IF NOT EXISTS staff (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  email TEXT,
  api_key TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);
"""

# 自社の既存事業を、そのままオファーとして定義する
SEED_TENANT = dict(
    name="自社（AshiBase）", kind="own",
    sender_name="AshiBase（足場ベース）", sender_email="info@ashibase.jp",
    sender_address="（本番: 登記上の住所）", optout_url="https://ashibase.jp/optout")

SEED_OFFERS = [
    dict(name="AI積算ツール（無料）", price_yen=0,
         landing_url="https://ashibase.jp/sekisan",
         target_rule="rank IN ('S','A')",
         offer_text=("足場の積算・部材拾いができるAIツールを無料開放。図面を送るだけで必要部材と"
                     "数量が自動で出る。登録はメールアドレスのみ、費用は一切かからない。"),
         ng_words=["絶対", "必ず儲かる", "業界No.1", "AI が全部やります"]),

    dict(name="AshiBase 資材管理", price_yen=14800,
         landing_url="https://ashibase.jp/shizai",
         target_rule="rank IN ('S','A') AND est_employees >= 8",
         offer_text=("足場資材の在庫・現場別の持ち出しと返却を管理するツール。"
                     "どの現場にいくら出ているかが把握でき、紛失と過剰発注が減る。"),
         ng_words=["絶対", "必ず", "在庫ゼロ"]),

    dict(name="建設会社 与信スコア", price_yen=29800,
         landing_url="https://ashibase.jp/yoshin",
         target_rule="rank IN ('S','A') AND prime_ratio >= 0.4",
         offer_text=("取引先の支払い実績と経営状況をスコア化して提供。"
                     "新規の元請・下請と取引を始める前に、焦げ付きの可能性を確認できる。"),
         ng_words=["倒産する", "危険な会社", "ブラックリスト", "信用できない"]),

    dict(name="AI入札部", price_yen=49800,
         landing_url="https://ashibase.jp/nyusatsu",
         target_rule="rank='S' AND prime_ratio >= 0.5 AND est_employees >= 15",
         offer_text=("公共工事の入札情報の収集から、参加要否の判断材料の整理までをAIが担当する。"
                     "入札担当を置かずに案件を拾い続けられる。"),
         ng_words=["落札できます", "必ず受注", "確実に勝てる"]),
]


def init(con):
    con.executescript(SCHEMA)
    now = datetime.now().isoformat(timespec="seconds")
    cur = con.execute("""INSERT INTO tenants (name,kind,sender_name,sender_email,sender_address,optout_url,created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (SEED_TENANT["name"], SEED_TENANT["kind"], SEED_TENANT["sender_name"],
         SEED_TENANT["sender_email"], SEED_TENANT["sender_address"],
         SEED_TENANT["optout_url"], now))
    tid = cur.lastrowid
    for o in SEED_OFFERS:
        con.execute("""INSERT INTO offers (tenant_id,name,offer_text,landing_url,price_yen,
            target_rule,ng_words,created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (tid, o["name"], o["offer_text"], o["landing_url"], o["price_yen"],
             o["target_rule"], json.dumps(o["ng_words"], ensure_ascii=False), now))
    con.commit()
    print(f"テナント1件・オファー{len(SEED_OFFERS)}件を投入")


def generate_api_key():
    return "tk_" + secrets.token_urlsafe(32)


def add_tenant(con, name, sender_email, kind="client", sender_name=None, sender_address="",
               optout_url=None):
    """他社に販売するテナントを追加する。api_keyはここで1回だけ生成し、
    呼び出し側(CLI)が画面に表示する。DBには平文で保持する(現状の他の秘密情報
    の扱いと同水準。将来ハッシュ化するなら移行スクリプトが要る)。"""
    now = datetime.now().isoformat(timespec="seconds")
    api_key = generate_api_key()
    cur = con.execute("""INSERT INTO tenants
        (name,kind,sender_name,sender_email,sender_address,optout_url,api_key,created_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (name, kind, sender_name or name, sender_email, sender_address,
         optout_url or f"mailto:{sender_email}",
         api_key, now))
    tid = cur.lastrowid
    # target_lists.send_list()がcampaigns.offer_id経由でテナントを解決するため、
    # 最低1件のオファーが無いと送信できない。target_ruleは呼び出し側で使わない
    # (送信先リストの企業を直接指定するため)ので「絶対に一致しない」条件にしておく。
    con.execute("""INSERT INTO offers (tenant_id,name,offer_text,price_yen,target_rule,
        ng_words,created_at) VALUES (?,?,?,?,?,?,?)""",
        (tid, "デフォルト", "送信先リストからの直接送信用", 0, "1=0", "[]", now))
    con.commit()
    return tid, api_key


def resolve_tenant_by_key(con, api_key):
    """Authorization: Bearer <api_key> からテナントを解決する。
    target_lists.py・api.pyの/api/tenant/*系エンドポイントが使う。
    クライアントが指定したtenant_idは一切信用せず、必ずここを経由すること。

    テナント自身のapi_keyだけでなく、staff.api_key(担当者ごとの個別キー)
    も見る。どちらで認証しても解決されるtenant_idは同じで、データは
    テナント単位で共有される(担当者ごとに見えるデータが変わるわけではない)。"""
    if not api_key:
        return None
    row = con.execute("SELECT * FROM tenants WHERE api_key=?", (api_key,)).fetchone()
    if row:
        return row
    staff = con.execute("SELECT tenant_id FROM staff WHERE api_key=?", (api_key,)).fetchone()
    if not staff:
        return None
    return con.execute("SELECT * FROM tenants WHERE id=?", (staff["tenant_id"],)).fetchone()


def add_staff(con, tenant_id, name, email=None):
    """担当者を追加し、その担当者専用のapi_keyを発行する。
    api_keyは生成時にしか分からない(以後DBには平文で残るが、呼び出し側の
    画面には1回しか出さない運用を想定)。"""
    now = datetime.now().isoformat(timespec="seconds")
    api_key = generate_api_key()
    cur = con.execute("""INSERT INTO staff (tenant_id, name, email, api_key, created_at)
        VALUES (?,?,?,?,?)""", (tenant_id, name, email, api_key, now))
    con.commit()
    return cur.lastrowid, api_key


def list_staff(con, tenant_id):
    rows = con.execute("""SELECT id, name, email, created_at FROM staff
        WHERE tenant_id=? ORDER BY created_at DESC""", (tenant_id,)).fetchall()
    return [dict(r) for r in rows]


def revoke_staff(con, tenant_id, staff_id):
    """担当者のapi_keyを失効させる(行ごと削除。以後そのキーでは認証できない)。"""
    cur = con.execute("DELETE FROM staff WHERE id=? AND tenant_id=?", (staff_id, tenant_id))
    con.commit()
    return cur.rowcount > 0


def listing(con):
    rows = con.execute("""SELECT o.*, t.name tname, t.kind FROM offers o
        JOIN tenants t ON t.id=o.tenant_id ORDER BY t.id, o.price_yen""").fetchall()
    cur_t = None
    for r in rows:
        if r["tname"] != cur_t:
            cur_t = r["tname"]
            print(f"\n■ {cur_t}（{r['kind']}）")
        price = "無料（入口）" if not r["price_yen"] else f"月額 {r['price_yen']:,}円"
        # 価格からCAC許容上限を逆算して示す（意思決定に直結する数字）
        cac_cap = r["price_yen"] * 24 * 0.33 if r["price_yen"] else None
        cap = f" / CAC許容上限 {cac_cap:,.0f}円" if cac_cap else ""
        print(f"  [{r['id']}] {r['name']:<22} {price}{cap}")
        print(f"       対象: {r['target_rule']}")


def resolve_targets(con, offer_id):
    """オファーの対象条件から実際の企業IDを引く（接触ガードも通す）"""
    o = con.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if not o:
        print("オファーが見つかりません"); return []
    rows = con.execute(f"SELECT id FROM companies WHERE dedup_of IS NULL AND ({o['target_rule']})").fetchall()
    import db
    ids = [r[0] for r in rows]
    ok, blocked = db.contactable_ids(con, ids)
    print(f"オファー「{o['name']}」の対象: 条件一致 {len(ids)}社 → 接触可 {len(ok)}社")
    if blocked:
        print("  除外: " + " / ".join(f"{k} {v}社" for k, v in blocked.items()))
    return ok


def check_ng(con, offer_id, text):
    """生成された文面がNGワードを含んでいないか。AIは指示しても稀に踏むので機械で止める。"""
    o = con.execute("SELECT ng_words, name FROM offers WHERE id=?", (offer_id,)).fetchone()
    if not o or not o["ng_words"]:
        return True, []
    hits = [w for w in json.loads(o["ng_words"]) if w in (text or "")]
    return (not hits), hits


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init"); sub.add_parser("list")
    t = sub.add_parser("targets"); t.add_argument("--offer", type=int, required=True)
    a = sub.add_parser("add")
    a.add_argument("--tenant", type=int, default=1); a.add_argument("--name", required=True)
    a.add_argument("--price", type=int, default=0); a.add_argument("--text", default="")
    a.add_argument("--rule", default="rank IN ('S','A')")
    at = sub.add_parser("add-tenant")
    at.add_argument("--name", required=True)
    at.add_argument("--sender-email", required=True)
    at.add_argument("--sender-address", default="")
    at.add_argument("--kind", default="client", choices=["own", "client", "acquirer"])
    args = ap.parse_args()

    import db
    con = db.connect(); db.migrate(con)
    if args.cmd == "init":
        init(con)
    elif args.cmd == "list":
        listing(con)
    elif args.cmd == "targets":
        resolve_targets(con, args.offer)
    elif args.cmd == "add":
        con.execute("""INSERT INTO offers (tenant_id,name,offer_text,landing_url,price_yen,
            target_rule,ng_words,created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (args.tenant, args.name, args.text or args.name, None, args.price,
             args.rule, "[]", datetime.now().isoformat(timespec="seconds")))
        con.commit(); print(f"オファー「{args.name}」を追加")
    elif args.cmd == "add-tenant":
        tid, api_key = add_tenant(con, args.name, args.sender_email, kind=args.kind,
                                   sender_address=args.sender_address)
        print(f"テナント「{args.name}」(id={tid}) を追加しました")
        print(f"api_key: {api_key}")
        print("  ↑ この値は今しか表示されません。顧客に安全な方法で渡してください"
              "(このAPIキーで target_lists の作成・閲覧ができます)")
