"""
kill_switch_cli.py — 異常検知時に実送信を即時停止する

senders.send_campaign()が全送信経路(手動送信/list_builder.htmlからの送信/
cron/Stock Factory運用API)の唯一の合流点であり、そこがKill Switchの状態を
毎回チェックする。dry_runの送信は対象外(実サイトへ触れないため)。

初期状態は「全体停止中」(db.pyのmigrate()が安全側で自動投入する)。
本番送信を許可するには、必ずここで明示的にresumeすること。

使い方:
  python3 kill_switch_cli.py status                       # 現在の状態を表示
  python3 kill_switch_cli.py stop --reason "異常検知のため"   # 全体停止
  python3 kill_switch_cli.py resume                        # 全体停止を解除
  python3 kill_switch_cli.py stop --tenant 3 --reason "..."  # テナント3だけ停止
  python3 kill_switch_cli.py resume --tenant 3              # テナント3を解除
"""
import argparse

import db


def status(con):
    g = con.execute("SELECT stopped, reason, updated_at, updated_by FROM kill_switch WHERE id=1").fetchone()
    if g and g["stopped"]:
        print(f"🔴 全体停止中 — 理由: {g['reason'] or '(未記入)'} "
              f"/ 更新: {g['updated_at']} by {g['updated_by'] or '?'}")
    else:
        print("🟢 全体は稼働中")

    tenants = db.list_tenant_kill_switches(con)
    if not tenants:
        print("テナント別の停止設定はありません")
        return
    print(f"\nテナント別停止: {len(tenants)}件")
    for t in tenants:
        print(f"  🔴 tenant_id={t['tenant_id']} ({t['tenant_name'] or '?'}) "
              f"— 理由: {t['reason'] or '(未記入)'} / 更新: {t['updated_at']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("stop", "resume"):
        p = sub.add_parser(name)
        p.add_argument("--tenant", type=int, help="指定するとそのテナントだけ停止/解除する(省略時は全体)")
        p.add_argument("--reason")
        p.add_argument("--by", default="cli", help="誰が操作したか(監査用)")
    args = ap.parse_args()

    con = db.connect(); db.migrate(con)
    if args.cmd == "status":
        status(con)
    elif args.cmd == "stop":
        if args.tenant:
            db.set_tenant_kill_switch(con, args.tenant, True, reason=args.reason, updated_by=args.by)
            print(f"テナント{args.tenant}を停止しました")
        else:
            db.set_global_kill_switch(con, True, reason=args.reason, updated_by=args.by)
            print("全体を停止しました")
    elif args.cmd == "resume":
        if args.tenant:
            db.set_tenant_kill_switch(con, args.tenant, False, updated_by=args.by)
            print(f"テナント{args.tenant}を解除しました")
        else:
            db.set_global_kill_switch(con, False, updated_by=args.by)
            print("全体を解除しました")
