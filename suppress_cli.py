"""
suppress_cli.py — 配信停止（サプレッション）の受付と確認
「送るな」と言われたら即座にここへ入れる。入った会社には二度と送らない。

なぜ最優先か:
  1. 特定電子メール法上、受信拒否後の送信は違法
  2. 足場業界は横のつながりが濃い。1件の「しつこい」が地域一帯に伝わる
  3. 買い手はデューデリでここを必ず見る。停止処理の運用実績がない獲得機構は
     法務リスクとして減額材料になる

使い方:
  python3 suppress_cli.py add --name "株式会社◯◯" --reason optout --source メール返信
  python3 suppress_cli.py add --id 123 --reason complaint --note "電話で強く要請"
  python3 suppress_cli.py list
  python3 suppress_cli.py check         # 送信済みの中に停止対象が混ざっていないか監査
"""
import argparse
from datetime import datetime

import db

REASONS = {
    "optout": "本人からの配信停止依頼",
    "complaint": "苦情・クレーム",
    "bounce_hard": "宛先不明（恒久エラー）",
    "manual": "手動除外",
    "competitor": "競合・取引不適",
}


def add(con, args):
    if args.id:
        row = con.execute("SELECT id, name FROM companies WHERE id=?", (args.id,)).fetchone()
    else:
        row = con.execute("SELECT id, name FROM companies WHERE name_norm=? OR name=?",
                          (db.normalize_name(args.name), args.name)).fetchone()
    if not row:
        print("該当する会社が見つかりません")
        return
    db.suppress(con, row["id"], args.reason, args.source, args.note)
    print(f"配信停止に登録: {row['name']} (id={row['id']}) / 理由: {REASONS.get(args.reason, args.reason)}")

    # 未送信の予定分も同時に取り消す（登録しただけで送られては意味がない）
    n = con.execute("""DELETE FROM touches WHERE company_id=? AND sent_at IS NULL""",
                    (row["id"],)).rowcount
    con.commit()
    if n:
        print(f"  未送信の予定 {n}件を取り消しました")


def listing(con):
    rows = con.execute("""SELECT s.*, c.name, c.pref FROM suppression s
        JOIN companies c ON c.id=s.company_id ORDER BY s.created_at DESC""").fetchall()
    print(f"配信停止リスト: {len(rows)}社\n")
    for r in rows[:50]:
        print(f"  {r['created_at'][:10]}  {r['name']}（{r['pref']}）"
              f"  {REASONS.get(r['reason'], r['reason'])}"
              f"{'  ／ ' + r['note'] if r['note'] else ''}")
    if len(rows) > 50:
        print(f"  ...他 {len(rows)-50}社")
    by = {}
    for r in rows:
        by[r["reason"]] = by.get(r["reason"], 0) + 1
    if by:
        print("\n  理由別:", "  ".join(f"{REASONS.get(k,k)} {v}件" for k, v in by.items()))


def check(con):
    """停止登録後に送信された形跡がないかを監査する"""
    bad = con.execute("""
        SELECT c.name, s.created_at AS suppressed_at, t.sent_at, t.channel
        FROM suppression s
        JOIN touches t ON t.company_id = s.company_id
        JOIN companies c ON c.id = s.company_id
        WHERE t.sent_at IS NOT NULL AND t.sent_at > s.created_at
        ORDER BY t.sent_at DESC""").fetchall()
    pending = con.execute("""
        SELECT COUNT(*) c FROM touches t JOIN suppression s ON s.company_id=t.company_id
        WHERE t.sent_at IS NULL""").fetchone()["c"]

    print("── 配信停止の遵守監査 ──")
    if bad:
        print(f"  ⚠ 停止後に送信された記録が {len(bad)}件あります。原因の特定が必要です。")
        for b in bad[:10]:
            print(f"    {b['name']} / 停止 {b['suppressed_at'][:10]} → 送信 {b['sent_at'][:10]} ({b['channel']})")
    else:
        print("  ✓ 停止後の送信記録なし")
    if pending:
        print(f"  ⚠ 停止対象に未送信の予定が {pending}件残っています。取り消してください。")
    else:
        print("  ✓ 停止対象への送信予定なし")
    total_sup = con.execute("SELECT COUNT(*) c FROM suppression").fetchone()["c"]
    total_co = con.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]
    if total_co:
        print(f"  停止率: {total_sup}/{total_co} ({total_sup/total_co*100:.2f}%)"
              f"  ※3%を超えたらオファーか文面を見直す水準")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--id", type=int); a.add_argument("--name")
    a.add_argument("--reason", choices=list(REASONS), default="optout")
    a.add_argument("--source"); a.add_argument("--note")
    sub.add_parser("list"); sub.add_parser("check")
    args = ap.parse_args()

    con = db.connect(); db.migrate(con)
    if args.cmd == "add":
        add(con, args)
    elif args.cmd == "list":
        listing(con)
    elif args.cmd == "check":
        check(con)
