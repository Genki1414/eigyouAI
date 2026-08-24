"""
scheduled_send_cli.py — 予約送信(MIKOMERUの「送信開始日時を指定する」相当)の実行係

list_builder.htmlから登録された予約(scheduled_sends。status='PENDING')のうち、
指定日時が過ぎたものを拾ってtarget_lists.send_list()へそのまま委譲する。
新しい送信経路は作らない——can_contact()・Kill Switch・冪等性等の既存ガードは
send_list()経由でそのまま効く。

crontabから数分おきに`run-due`を実行する想定(deploy/crontab参照)。

使い方:
  python3 scheduled_send_cli.py run-due     # 期限到来分をすべて実行
  python3 scheduled_send_cli.py list        # PENDINGの予約一覧を表示
"""
import argparse
from datetime import datetime

import db
import target_lists as TL


def run_due(con):
    now_iso = datetime.now().isoformat(timespec="seconds")
    due = db.due_scheduled_sends(con, now_iso)
    if not due:
        print("期限到来分の予約はありません")
        return
    print(f"{len(due)}件の予約を実行します")
    for s in due:
        try:
            res = TL.send_list(con, s["tenant_id"], s["list_id"], s["subject"], s["body"],
                               dry_run=bool(s["dry_run"]), track_clicks=bool(s["track_clicks"]),
                               sender_template_id=s["sender_template_id"])
            if res is None:
                db.finish_scheduled_send(con, s["id"], "FAILED",
                                         {"error": "リストが見つかりません(削除された可能性)"})
                print(f"  予約{s['id']}: 失敗(リストが見つかりません)")
            elif "error" in res:
                db.finish_scheduled_send(con, s["id"], "FAILED", res)
                print(f"  予約{s['id']}: 失敗({res['error']})")
            else:
                db.finish_scheduled_send(con, s["id"], "DONE", res)
                stats = res.get("stats") or {}
                print(f"  予約{s['id']}: 完了(送信{stats.get('sent', 0)} "
                      f"失敗{stats.get('failed', 0)} 対象{res.get('target_count', 0)})")
        except Exception as e:  # noqa: BLE001
            # 1件の例外で他の予約の実行まで止めない(cronの次回実行にも影響させない)
            db.finish_scheduled_send(con, s["id"], "FAILED", {"error": str(e)[:200]})
            print(f"  予約{s['id']}: 例外で失敗({e})")


def list_pending(con):
    rows = con.execute("""SELECT s.id, s.tenant_id, tn.name tenant_name, s.list_id, tl.name list_name,
            s.scheduled_at, s.dry_run
        FROM scheduled_sends s
        LEFT JOIN tenants tn ON tn.id = s.tenant_id
        LEFT JOIN target_lists tl ON tl.id = s.list_id
        WHERE s.status='PENDING' ORDER BY s.scheduled_at""").fetchall()
    if not rows:
        print("PENDINGの予約はありません")
        return
    for r in rows:
        mode = "ドライラン" if r["dry_run"] else "本番送信"
        print(f"  #{r['id']} {r['scheduled_at']} {mode} "
              f"テナント={r['tenant_name'] or r['tenant_id']} "
              f"リスト={r['list_name'] or r['list_id']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run-due")
    sub.add_parser("list")
    args = ap.parse_args()

    con = db.connect(); db.migrate(con)
    if args.cmd == "run-due":
        run_due(con)
    elif args.cmd == "list":
        list_pending(con)
