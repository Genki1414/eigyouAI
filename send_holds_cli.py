"""
send_holds_cli.py — 送信保留(T136。2026-09-24)

相手サイト側の事情で構造的に送れない会社(ドメイン消滅・問い合わせフォームが無い・
採用専用の窓口・mailto: だけ・ボット検知ページ等)を通常の送信から外し、定期的に
送信を試して、届いた時点で保留から外す。**画像認証(CAPTCHA)は保留にしない**
(人が解けば送れるので、手動フォローの対象として残す。ユーザー指示 2026-09-24)。

保留は `send_holds` テーブル(会社単位。サイト側の事情なのでテナントを問わない)。
- 通常の送信(target_lists.send_list)は保留中の会社を対象から外す(画面の件数表示にも
  「送信保留中N件を除外」と出る)
- 送信が終わるたびに db.apply_send_holds() が結果から保留を更新する(構造的に送れなかった
  会社を保留に、届いた会社を保留から外す)
- 定期的な再試行は「保留中の会社だけ」に送る予約(scheduled_sends.retry_holds=1)を、
  そのリストの直近の本番予約を複製して作る(件名・本文・送信元は前回と同じ)

使い方:
  python3 send_holds_cli.py list [--list ID]            # 保留中の会社数を理由別に
  python3 send_holds_cli.py backfill --list ID [--days 30]
      # 過去の送信結果(直近N日)から保留を作る。新しい送信結果から更新するのは自動なので、
      # 保留の仕組みを入れる前に送ったリストに1回だけ使う
  python3 send_holds_cli.py release COMPANY_ID [--note ...]   # 手動で保留から外す
  python3 send_holds_cli.py retry --list ID [--older-than-days 30]
      # そのリストの保留中の会社だけに送る予約を作る(直近の本番予約の複製)。本番送信
  python3 send_holds_cli.py retry --all [--older-than-days 30]
      # 保留中の会社を持つ全リストについて上記(cronの定期実行用。deploy/crontab)

再試行の予約は通常の予約と同じくキュー(scheduled_sends)に入り、senderサービスが
実行する。完了通知メールも通常どおり届く(件名に「(保留の再試行)」と入る)。
"""
import argparse
from datetime import datetime, timedelta

import db
import target_lists as TL


def _label(code):
    return TL.REASON_LABELS_JA.get(code, code)


def cmd_list(con, list_id=None):
    rows = db.hold_counts(con, list_id)
    total = sum(n for _, n in rows)
    scope = f"リスト{list_id}" if list_id else "全リスト"
    print(f"送信保留中({scope}): {total:,}社")
    for code, n in rows:
        print(f"  {n:6,}  {_label(code)} ({code})")
    if not rows:
        print("  (なし)")


def cmd_backfill(con, list_id, days):
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    lst = con.execute("SELECT id, name FROM target_lists WHERE id=? AND deleted_at IS NULL",
                      (list_id,)).fetchone()
    if not lst:
        print(f"リスト{list_id}はありません")
        raise SystemExit(1)
    r = db.apply_send_holds(con, list_id, since)
    print(f"リスト{list_id}「{lst['name']}」の直近{days}日の送信結果から: "
          f"新たに保留 {r['held']:,}社 / 解除 {r['released']:,}社")
    cmd_list(con, list_id)


def cmd_release(con, company_id, note):
    ok = db.release_send_hold(con, company_id, note or "手動で解除")
    print(f"会社{company_id}: " + ("保留を外しました" if ok else "保留中ではありません"))


def _latest_prod_send(con, list_id):
    return con.execute("""SELECT id, tenant_id, subject FROM scheduled_sends
        WHERE list_id=? AND dry_run=0 AND COALESCE(retry_holds,0)=0
          AND status IN ('DONE','PAUSED','CANCELLED','FAILED')
        ORDER BY id DESC LIMIT 1""", (list_id,)).fetchone()


def _retry_one(con, list_id, older_than_days):
    """保留中の会社だけに送る予約を作る。作った予約IDか None。"""
    cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat(timespec="seconds")
    due = con.execute("""SELECT COUNT(*) FROM send_holds h
        JOIN target_list_members m ON m.company_id=h.company_id AND m.list_id=?
        WHERE h.released_at IS NULL AND COALESCE(h.last_checked_at, h.held_at) < ?""",
        (list_id, cutoff)).fetchone()[0]
    held = sum(n for _, n in db.hold_counts(con, list_id))
    if not held:
        print(f"リスト{list_id}: 保留中の会社はありません")
        return None
    if not due:
        print(f"リスト{list_id}: 保留中{held:,}社は最後に試してから{older_than_days}日経っていません(まだ再試行しない)")
        return None
    src = _latest_prod_send(con, list_id)
    if not src:
        print(f"リスト{list_id}: 複製できる本番の予約が無いので再試行の予約は作れません")
        return None
    pending = con.execute("""SELECT id FROM scheduled_sends WHERE list_id=? AND retry_holds=1
        AND status IN ('PENDING','RUNNING','PAUSED')""", (list_id,)).fetchone()
    if pending:
        print(f"リスト{list_id}: 再試行の予約 #{pending['id']} がまだ終わっていないので作りません")
        return None
    new_id = db.clone_scheduled_send(con, src["id"], cancel_recent_days=0, retry_holds=True)
    print(f"リスト{list_id}: 保留中{held:,}社(うち期限到来{due:,}社)に送る再試行の予約 #{new_id} を作りました"
          f"(予約 #{src['id']}「{(src['subject'] or '')[:30]}」の複製)")
    return new_id


def cmd_retry(con, list_id=None, all_lists=False, older_than_days=30):
    if all_lists:
        targets = db.lists_with_holds_due(con, older_than_days)
        if not targets:
            print(f"再試行の期限({older_than_days}日)が来た保留はありません")
            return
        for row in targets:
            _retry_one(con, row["list_id"], older_than_days)
    else:
        _retry_one(con, list_id, older_than_days)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--list", type=int, default=None)
    p = sub.add_parser("backfill"); p.add_argument("--list", type=int, required=True)
    p.add_argument("--days", type=int, default=30)
    p = sub.add_parser("release"); p.add_argument("company_id", type=int)
    p.add_argument("--note", default=None)
    p = sub.add_parser("retry"); p.add_argument("--list", type=int, default=None)
    p.add_argument("--all", action="store_true")
    p.add_argument("--older-than-days", type=int, default=30)
    args = ap.parse_args()

    con = db.connect(); db.migrate(con)
    if args.cmd == "list":
        cmd_list(con, args.list)
    elif args.cmd == "backfill":
        cmd_backfill(con, args.list, args.days)
    elif args.cmd == "release":
        cmd_release(con, args.company_id, args.note)
    elif args.cmd == "retry":
        if not args.all and args.list is None:
            ap.error("retry には --list ID か --all が必要です")
        cmd_retry(con, args.list, args.all, args.older_than_days)


if __name__ == "__main__":
    main()
