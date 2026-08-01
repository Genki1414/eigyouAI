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
"""
import argparse
import json
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
