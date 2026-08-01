"""
campaign.py — Phase 2 の中核: 接触ログと反応をDBに戻すループ
「誰に・どのチャネルで・どの文面を送り・どう反応したか」を全部残す。
ここが貯まると CAC / 反応率 / 転換率 が実測値になり、M&Aのデューデリで語れる資産になる。

使い方:
  python3 campaign.py init                      # テーブル作成
  python3 campaign.py create "2026年9月 S+A 初回接触" --target SA
  python3 campaign.py simulate 1                # デモ用: 反応を業界実勢で擬似生成
"""
import argparse, json, random, sqlite3, sys
from datetime import datetime, timedelta
from pathlib import Path

import db as DBM   # 接触ガードはここを必ず通す

DB = Path(__file__).parent / "out" / "companies.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  started_at TEXT,
  target_rule TEXT,          -- SA / S / ALL など
  cost_yen INTEGER DEFAULT 0 -- 実費(FAX送信料・郵送料・印刷代)
);

CREATE TABLE IF NOT EXISTS touches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL,
  company_id INTEGER NOT NULL,
  channel TEXT NOT NULL,        -- FAX / 郵送DM / メール / SMS / 架電
  variant TEXT,                 -- 文面バリアント(A/B/C) ← A/Bテストの単位
  subject TEXT,
  body TEXT,
  sent_at TEXT,
  unit_cost_yen INTEGER DEFAULT 0,
  -- ▼ 反応(ファネル各段階)
  delivered INTEGER DEFAULT 1,  -- 到達(FAX送信成功/メール非バウンス)
  opened INTEGER DEFAULT 0,     -- 開封(メールのみ計測可)
  responded INTEGER DEFAULT 0,  -- 反応(返信・折返し・LP訪問)
  signed_up INTEGER DEFAULT 0,  -- 無料ツール登録
  activated INTEGER DEFAULT 0,  -- 積算を1回以上実行(=価値到達)
  paid INTEGER DEFAULT 0,       -- 有料転換
  responded_at TEXT,
  paid_at TEXT,
  mrr_yen INTEGER DEFAULT 0,
  note TEXT,
  UNIQUE(campaign_id, company_id, variant),
  FOREIGN KEY(campaign_id) REFERENCES campaigns(id),
  FOREIGN KEY(company_id) REFERENCES companies(id)
);
CREATE INDEX IF NOT EXISTS idx_touch_camp ON touches(campaign_id);
CREATE INDEX IF NOT EXISTS idx_touch_co ON touches(company_id);
"""

# 業界実勢に寄せた反応率の事前分布(Phase2の実測で置換される仮置き)
BASE_RESPONSE = {
    "FAX": 0.018, "郵送DM": 0.030, "メール": 0.045, "SMS": 0.035, "架電": 0.120,
}
VARIANT_LIFT = {"A": 1.0, "B": 1.35, "C": 0.85}  # B=課題提起型が強い想定


def init(con):
    con.executescript(SCHEMA)
    con.commit()
    print("campaigns / touches テーブルを作成")


def create(con, name, target):
    rule = {"SA": "rank IN ('S','A')", "S": "rank='S'", "ALL": "1=1"}[target]
    cur = con.execute(
        "INSERT INTO campaigns (name, started_at, target_rule) VALUES (?,?,?)",
        (name, datetime.now().isoformat(timespec="seconds"), target))
    cid = cur.lastrowid
    rows = con.execute(f"SELECT id, channel_hint FROM (SELECT id, rank, "
                       f"CASE WHEN has_website=0 THEN 'FAX' "
                       f"     WHEN hiring_now=1 THEN 'メール' "
                       f"     WHEN rank IN ('S','A') THEN '郵送DM' "
                       f"     ELSE 'メール' END AS channel_hint FROM companies) "
                       f"WHERE {rule}").fetchall()
    # ── 接触ガード: 配信停止・重複・上限・間隔をここで一括除外 ──
    ok_ids, blocked = DBM.contactable_ids(con, [r[0] for r in rows])
    ok_set = set(ok_ids)
    if blocked:
        print("  除外: " + " / ".join(f"{k} {v}社" for k, v in blocked.items()))

    n = 0
    for company_id, channel in rows:
        if company_id not in ok_set:
            continue
        variant = random.choice(["A", "B", "C"])
        con.execute("""INSERT OR IGNORE INTO touches
            (campaign_id, company_id, channel, variant, unit_cost_yen)
            VALUES (?,?,?,?,?)""",
            (cid, company_id, channel, variant,
             {"FAX": 12, "郵送DM": 95, "メール": 1, "SMS": 8, "架電": 180}[channel]))
        n += 1
    con.commit()
    print(f"キャンペーン#{cid}「{name}」を作成: 対象{n}社")
    return cid


def simulate(con, cid):
    """デモ用: 送信〜反応〜有料転換を業界実勢の確率で生成"""
    rows = con.execute("""SELECT t.id, t.channel, t.variant, t.unit_cost_yen,
                                 c.score, c.hiring_now
                          FROM touches t JOIN companies c ON c.id=t.company_id
                          WHERE t.campaign_id=?""", (cid,)).fetchall()
    t0 = datetime.now() - timedelta(days=30)
    cost = 0
    for tid, ch, va, uc, score, hiring in rows:
        cost += uc
        p = BASE_RESPONSE[ch] * VARIANT_LIFT[va]
        p *= 0.6 + (score / 100) * 0.9      # スコアが高いほど反応する
        if hiring:
            p *= 1.4
        sent = (t0 + timedelta(days=random.randint(0, 5))).isoformat(timespec="seconds")
        delivered = 1 if random.random() > 0.04 else 0
        responded = 1 if (delivered and random.random() < p) else 0
        signed = 1 if (responded and random.random() < 0.62) else 0   # 反応→無料登録
        activated = 1 if (signed and random.random() < 0.70) else 0   # 登録→積算実行
        paid = 1 if (activated and random.random() < 0.28) else 0     # 活性→有料
        con.execute("""UPDATE touches SET sent_at=?, delivered=?, responded=?,
                       signed_up=?, activated=?, paid=?, responded_at=?, paid_at=?, mrr_yen=?
                       WHERE id=?""",
                    (sent, delivered, responded, signed, activated, paid,
                     sent if responded else None,
                     sent if paid else None,
                     random.choice([9800, 14800, 19800]) if paid else 0, tid))
    con.execute("UPDATE campaigns SET cost_yen=? WHERE id=?", (cost, cid))
    con.commit()
    print(f"キャンペーン#{cid} のシミュレーション完了 (実費 {cost:,}円)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["init", "create", "simulate"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--target", default="SA")
    a = ap.parse_args()
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    if a.cmd == "init":
        init(con)
    elif a.cmd == "create":
        create(con, a.arg or "無題キャンペーン", a.target)
    elif a.cmd == "simulate":
        simulate(con, int(a.arg))
