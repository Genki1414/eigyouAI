"""
dormant.py — 休眠プールの管理と再投入
3段まで打って無反応の会社を捨てるのは早い。足場屋が動くのは「案件が増えて積算が回らなくなった時」
なので、反応しなかったのは商品が合わないのではなく、単にタイミングでないことが多い。

休眠ルール:
  - Step3まで無反応 → 休眠プールへ（cooldown 180日）
  - ただし「復活シグナル」が立った会社は待たずに即再投入
      * 求人を新たに出し始めた（＝案件が増えて人が足りない）
      * HPが更新された / 施工実績が追加された
      * 同地域の同業が導入した（社会的証明が使えるようになった）
  - 再投入時は必ず「前回とは別の入口」にする。同じオファーの再送はブランドを削る。

再投入オファーの切り替え:
  1巡目: 無料ツールを配る          → 反応しなかった
  2巡目: 積算を代行して結果を渡す   → 手を動かさせない
  3巡目: 同地域の導入事例を持っていく → 人の紹介に寄せる

使い方:
  python3 dormant.py --campaign 1 --pool          # 休眠プールを作る
  python3 dormant.py --campaign 1 --revive --dry  # 復活対象の確認
  python3 dormant.py --campaign 1 --revive        # 再投入キャンペーンを生成
"""
import argparse, sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import db as DBM   # 接触ガード

DB = Path(__file__).parent / "out" / "companies.db"
COOLDOWN_DAYS = 180

SCHEMA = """
CREATE TABLE IF NOT EXISTS dormant (
  company_id INTEGER PRIMARY KEY,
  from_campaign INTEGER,
  entered_at TEXT,
  cycles INTEGER DEFAULT 1,      -- 何巡目まで打ったか
  next_eligible_at TEXT,
  revive_signal TEXT,            -- 立ったシグナル
  FOREIGN KEY(company_id) REFERENCES companies(id)
);
"""

# 巡目ごとのオファー切り替え（同じ入口を二度使わない）
CYCLE_OFFER = {
    1: ("無料ツール配布", "図面を送るだけで部材と数量が出るツールを無料開放"),
    2: ("積算代行", "図面を1枚お預かりして、こちらで積算した結果をお返しする"),
    3: ("地域事例訪問", "同じ地域の足場会社の導入結果を持って、10分だけ説明に伺う"),
}


def build_pool(con, cid):
    """Step3まで無反応の会社を休眠プールへ"""
    rows = con.execute("""
        SELECT company_id FROM touches WHERE campaign_id=?
        GROUP BY company_id
        HAVING MAX(step) >= 3 AND SUM(responded) = 0
    """, (cid,)).fetchall()
    now = datetime.now()
    nxt = (now + timedelta(days=COOLDOWN_DAYS)).isoformat(timespec="seconds")
    for (company_id,) in rows:
        con.execute("""INSERT INTO dormant (company_id, from_campaign, entered_at, cycles, next_eligible_at)
            VALUES (?,?,?,1,?)
            ON CONFLICT(company_id) DO UPDATE SET
              cycles = cycles + 1,
              next_eligible_at = excluded.next_eligible_at""",
            (company_id, cid, now.isoformat(timespec="seconds"), nxt))
    con.commit()
    print(f"休眠プールへ {len(rows)}社（cooldown {COOLDOWN_DAYS}日 / 次回 {nxt[:10]}）")
    return len(rows)


def detect_signals(con):
    """復活シグナルを検出。本番では enrich.py の再クロール差分でここが埋まる。
    ここでは現在値から判定できるものだけを実装している。"""
    # 1) 求人を出している = 案件過多のサイン
    con.execute("""UPDATE dormant SET revive_signal='求人出稿中'
        WHERE revive_signal IS NULL AND company_id IN (
          SELECT id FROM companies WHERE hiring_now=1)""")
    # 2) 同地域で既に有料転換が出ている = 事例が使える
    con.execute("""UPDATE dormant SET revive_signal=COALESCE(revive_signal,'')||' 同地域に導入事例あり'
        WHERE company_id IN (
          SELECT c.id FROM companies c WHERE c.pref IN (
            SELECT c2.pref FROM touches t JOIN companies c2 ON c2.id=t.company_id
            WHERE t.paid=1))""")
    con.commit()


def revive(con, cid, dry):
    """cooldown経過 or 復活シグナルありの会社を再投入対象として抽出"""
    detect_signals(con)
    now = datetime.now().isoformat(timespec="seconds")
    rows = con.execute("""
        SELECT d.company_id, d.cycles, d.next_eligible_at, d.revive_signal,
               c.name, c.pref, c.rank, c.score_v2
        FROM dormant d JOIN companies c ON c.id = d.company_id
        WHERE d.next_eligible_at <= ? OR d.revive_signal IS NOT NULL
        ORDER BY (d.revive_signal IS NOT NULL) DESC, c.score_v2 DESC
    """, (now,)).fetchall()

    # ── 接触ガード: 休眠からの復活もここを必ず通す ──
    ok_ids, blocked = DBM.contactable_ids(con, [r[0] for r in rows])
    ok_set = set(ok_ids)
    if blocked:
        print("  除外: " + " / ".join(f"{k} {v}社" for k, v in blocked.items()))
    rows = [r for r in rows if r[0] in ok_set]

    print(f"再投入候補 {len(rows)}社")
    by_cycle = {}
    for r in rows:
        by_cycle.setdefault(r[1] + 1, []).append(r)
    for cyc, items in sorted(by_cycle.items()):
        offer, desc = CYCLE_OFFER.get(min(cyc, 3), CYCLE_OFFER[3])
        print(f"\n  ── {cyc}巡目 / オファー「{offer}」: {len(items)}社")
        print(f"     {desc}")
        for r in items[:4]:
            sig = f" ← {r[3].strip()}" if r[3] else ""
            print(f"     ・{r[4]}（{r[5]} / V2 {r[7] or 0:.1f}）{sig}")
        if len(items) > 4:
            print(f"     ...他 {len(items)-4}社")

    if dry:
        print("\n--dry のため作成しませんでした")
        return

    # 再投入キャンペーンを作成
    name = f"再投入 {datetime.now():%Y年%m月} (休眠復活)"
    cur = con.execute("INSERT INTO campaigns (name, started_at, target_rule) VALUES (?,?,?)",
                      (name, now, "DORMANT_REVIVE"))
    new_cid = cur.lastrowid
    for r in rows:
        cyc = min(r[1] + 1, 3)
        ch = "架電" if cyc == 3 else "メール"
        con.execute("""INSERT OR IGNORE INTO touches
            (campaign_id, company_id, channel, variant, step, unit_cost_yen)
            VALUES (?,?,?,?,1,?)""",
            (new_cid, r[0], ch, f"REVIVE{cyc}", 180 if ch == "架電" else 1))
    con.commit()
    print(f"\n再投入キャンペーン #{new_cid}「{name}」を作成: {len(rows)}社")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", type=int, required=True)
    ap.add_argument("--pool", action="store_true")
    ap.add_argument("--revive", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    if a.pool:
        build_pool(con, a.campaign)
    if a.revive:
        revive(con, a.campaign, a.dry)
