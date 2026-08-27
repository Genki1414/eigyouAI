"""
followup.py — 多段フォローアップ + 勝ち文面の自動増殖
アウトバウンドの収穫の大半は初回ではなく2〜3回目に出る。ただし同じ内容を送り直すと嫌われる
ので、「チャネルを変える × 訴求を変える」を必ずセットにする。

シーケンス設計（無反応先のみが次段に進む）:
  Step1  D+0    初回（scoring推奨チャネル）
  Step2  D+14   別チャネル + 別訴求（勝ち文面の派生）
  Step3  D+35   最終接触。オファーを「無料で使える」から「使い方を10分で説明する」に変える
  → Step3も無反応なら 休眠プールへ（6ヶ月後に再投入）

勝ち文面の増殖:
  metrics で最も反応率が高かったバリアントをAIに渡し、派生案A'/A''を生成させて次段に投入。
  これで文面は世代を追うごとに強くなる（--evolve）。

使い方:
  python3 followup.py --campaign 1 --step 2
  python3 followup.py --campaign 1 --step 2 --simulate
  python3 followup.py --campaign 1 --evolve --offline
"""
import argparse, json, random
from datetime import datetime, timedelta

import db as DBM   # 接触ガード

# ステップごとの「次に使うチャネル」。初回と必ず変える。
NEXT_CHANNEL = {
    "メール": ["SMS", "FAX"],
    "FAX": ["メール", "郵送DM"],
    "郵送DM": ["メール", "FAX"],
    "SMS": ["メール", "FAX"],
}
UNIT_COST = {"FAX": 12, "郵送DM": 95, "メール": 1, "SMS": 8, "架電": 180}

# 段が進むほど反応率は落ちるが、累積では確実に増える（実務の経験則）
STEP_DECAY = {2: 0.62, 3: 0.38}

STEP3_BODY = """{name} ご担当者様

何度かご案内をお送りしておりました、足場の積算ツールのAshiBaseです。
これで最後のご連絡といたします。

「ツールを入れても使いこなせるか分からない」というお声をよくいただきます。
そのため、実際の図面を1枚お預かりして、こちらで積算した結果をお返しする形も承っております。
お手間は図面を送っていただくだけで、費用はかかりません。

もし今はタイミングでなければ、このままご放念ください。

AshiBase（足場ベース）"""


def build_step(con, cid, step):
    """前段で無反応だった会社に、別チャネル・別訴求で次の接触を作る"""
    prev = step - 1
    rows = con.execute("""
        SELECT t.company_id, t.channel, t.variant, c.name, c.pref, c.enrich_note
        FROM touches t JOIN companies c ON c.id = t.company_id
        WHERE t.campaign_id = ? AND t.step = ? AND t.delivered = 1 AND t.responded = 0
          -- 他キャンペーン・他ステップも含め、一度でも反応した会社は追いかけない
          AND NOT EXISTS (SELECT 1 FROM touches x
                          WHERE x.company_id = t.company_id AND x.responded = 1)
    """, (cid, prev)).fetchall()

    # tenant_id=1(自社/houseエンジン)を明示する。T43参照(campaign.pyと同じ理由)。
    ok_ids, blocked = DBM.contactable_ids(con, [r[0] for r in rows], tenant_id=1)
    ok_set = set(ok_ids)
    if blocked:
        print("  除外: " + " / ".join(f"{k} {v}社" for k, v in blocked.items()))

    n = 0
    for company_id, ch, va, name, pref, note in rows:
        if company_id not in ok_set:
            continue
        nch = random.choice(NEXT_CHANNEL.get(ch, ["メール"]))
        nva = f"{va}'" if step == 2 else "FINAL"
        body = STEP3_BODY.format(name=name) if step == 3 else None
        con.execute("""INSERT OR IGNORE INTO touches
            (campaign_id, company_id, channel, variant, step, unit_cost_yen, body)
            VALUES (?,?,?,?,?,?,?)""",
            (cid, company_id, nch, nva, step, UNIT_COST[nch], body))
        n += 1
    con.commit()
    print(f"Step{step} を生成: {n}件（Step{prev}の無反応先に別チャネルで再接触）")
    return n


def simulate_step(con, cid, step):
    from campaign import BASE_RESPONSE, VARIANT_LIFT
    rows = con.execute("""SELECT t.id, t.channel, t.variant, c.score, c.hiring_now
        FROM touches t JOIN companies c ON c.id=t.company_id
        WHERE t.campaign_id=? AND t.step=? AND t.sent_at IS NULL""", (cid, step)).fetchall()
    t0 = datetime.now() - timedelta(days=30 - 14 * (step - 1))
    for tid, ch, va, score, hiring in rows:
        base_va = va[0] if va and va[0] in "ABC" else "A"
        p = BASE_RESPONSE[ch] * VARIANT_LIFT[base_va] * STEP_DECAY.get(step, 0.4)
        p *= 0.6 + (score / 100) * 0.9
        if hiring:
            p *= 1.4
        if va.endswith("'"):
            p *= 1.15  # 勝ち文面の派生は少し強い
        sent = (t0 + timedelta(days=random.randint(0, 3))).isoformat(timespec="seconds")
        delivered = 1 if random.random() > 0.04 else 0
        responded = 1 if (delivered and random.random() < p) else 0
        signed = 1 if (responded and random.random() < 0.62) else 0
        activated = 1 if (signed and random.random() < 0.70) else 0
        paid = 1 if (activated and random.random() < 0.28) else 0
        con.execute("""UPDATE touches SET sent_at=?, delivered=?, responded=?, signed_up=?,
                       activated=?, paid=?, responded_at=?, paid_at=?, mrr_yen=? WHERE id=?""",
                    (sent, delivered, responded, signed, activated, paid,
                     sent if responded else None, sent if paid else None,
                     random.choice([9800, 14800, 19800]) if paid else 0, tid))
    con.commit()
    print(f"Step{step} をシミュレーション: {len(rows)}件")


EVOLVE_PROMPT = """次の営業文面は、足場会社向けキャンペーンで最も反応率が高かったものです（反応率{rate}%）。

--- 元文面 ---
{body}
---

この文面の「効いている要素」を保ったまま、切り口だけを変えた派生案を1つ書いてください。
2回目の接触に使うため、1回目と同じことを繰り返している印象を与えてはいけません。
足場屋の社長が読む文章です。横文字・IT用語は使わず、本文のみを出力してください。"""


def evolve(con, cid, offline):
    """最も反応率の高かったバリアントから派生文面を生成し、Step2に適用"""
    row = con.execute("""SELECT variant, COUNT(*) n, SUM(responded) r
        FROM touches WHERE campaign_id=? AND step=1 GROUP BY variant
        ORDER BY CAST(SUM(responded) AS REAL)/COUNT(*) DESC LIMIT 1""", (cid,)).fetchone()
    if not row:
        print("Step1のデータがありません"); return
    win_va, n, r = row
    rate = round(r / n * 100, 2)
    base = con.execute("""SELECT body FROM touches WHERE campaign_id=? AND step=1
        AND variant=? AND body IS NOT NULL AND body!='' LIMIT 1""", (cid, win_va)).fetchone()
    print(f"勝ちバリアント: {win_va}（反応率 {rate}%）から派生案を生成")

    if offline:
        derived = ("{name} ご担当者様\n\n先日ご案内をお送りしておりました、足場の積算ツールの"
                   "AshiBaseです。\n\n図面を1枚お預かりして、必要な部材と数量をこちらで出して"
                   "お返しする形もご用意しました。まずは結果だけ見ていただければと思います。\n\n"
                   "費用はかかりません。ご希望でしたらこのままご返信ください。\n\nAshiBase（足場ベース）")
    else:
        import os, anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=1000,
            messages=[{"role": "user", "content": EVOLVE_PROMPT.format(rate=rate, body=base[0])}])
        derived = "".join(b.text for b in msg.content if b.type == "text").strip()

    rows = con.execute("""SELECT t.id, c.name FROM touches t JOIN companies c ON c.id=t.company_id
        WHERE t.campaign_id=? AND t.step=2 AND (t.body IS NULL OR t.body='')""", (cid,)).fetchall()
    for tid, name in rows:
        con.execute("UPDATE touches SET body=? WHERE id=?", (derived.replace("{name}", name), tid))
    con.commit()
    print(f"派生文面をStep2の{len(rows)}件に適用")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", type=int, required=True)
    ap.add_argument("--step", type=int)
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--evolve", action="store_true")
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()

    # DATABASE_URL未設定ならSQLite、設定済みならPostgresへ自動で振り分ける
    # (db.connect()経由。素のsqlite3.connect()だとPostgres運用時にも常に
    # ローカルSQLiteへ書いてしまい、本番のtouchesと乖離する)。
    # touches.stepはdb.pyのSCHEMAに既に定義済み(db.migrate()で作成される)
    # ため、ここでの個別ALTER TABLEは不要。
    con = DBM.connect()

    if a.evolve:
        evolve(con, a.campaign, a.offline)
    elif a.step:
        build_step(con, a.campaign, a.step)
        if a.simulate:
            simulate_step(con, a.campaign, a.step)
