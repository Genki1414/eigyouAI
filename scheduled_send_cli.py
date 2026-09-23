"""
scheduled_send_cli.py — 送信キューの実行係(予約送信+即時送信の両方。T91で常駐化)

list_builder.htmlの「送信する」(T91以降は即時実行せずキューに入る)と
「送信開始日時を指定する」(予約)はどちらもscheduled_sends(status='PENDING')に
入り、ここが拾ってtarget_lists.send_list()へそのまま委譲する。新しい送信経路は
作らない——can_contact()・Kill Switch・冪等性等の既存ガードはsend_list()経由で
そのまま効く。

複数ワーカー対応(T91): 各予約はdb.claim_scheduled_send()でアトミックに取り込む
(PENDING→RUNNING)ため、ワーカーを何プロセス/何台動かしても同じ予約を二重に
実行しない。ワーカーが途中で落ちてRUNNINGのまま残った予約は、STALE_RUNNING_HOURS
経過後にPENDINGへ戻して別のワーカーがやり直す(send_list()は送信済みの会社を
冪等に飛ばすので二重送信にならない)。

使い方:
  python3 scheduled_send_cli.py loop [--workers N] [--interval SEC]
      # 常駐。N個の子プロセスがそれぞれinterval秒おきにキューを見て実行する
      # (deploy/docker-compose.ymlのsenderサービスがこれ。Nは環境変数SENDER_WORKERS)
  python3 scheduled_send_cli.py run-due     # 期限到来分を1回だけ実行して終了(手動・保険用)
  python3 scheduled_send_cli.py list        # PENDING/RUNNINGの予約一覧を表示
  python3 scheduled_send_cli.py stop ID     # 予約を停止する(実行中でも可。あとで再開できる)
  python3 scheduled_send_cli.py cancel ID   # 予約を取り消す(実行中でも可)
  python3 scheduled_send_cli.py resume ID   # 停止した予約を順番待ちへ戻す
  python3 scheduled_send_cli.py clone ID [--cancel-recent-days N] [--at 2026-09-24T09:00:00]
      # 過去の予約を複製して新しい予約を作る(前回と同じ文面で送り直す。T131)。
      # 何社に送るかを表示してから作る。本番送信なので ops-write では承認が要る

停止・取り消し(T127): 実行中の予約は stop_requested に要求を書き、ワーカーが会社ごとに
見て抜ける(進行中の数社は送り終える)。それまでは実行中の予約を止める正規の手段が無く、
2026-09-23はSSHでコンテナを止めてDBを直接書き換えるしかなかった。
"""
import argparse
import json
import multiprocessing
import os
import signal
import socket
import time
from datetime import datetime, timedelta

import db
import target_lists as TL

STALE_RUNNING_HOURS = 3
# T98: 例外で落ちた予約の自動再試行回数と、処理数が増えないまま何分経ったら「固まった」と
# みなして子ワーカーを起動し直すか(.envで変更可)
MAX_ATTEMPTS = int(os.environ.get("SENDER_MAX_ATTEMPTS", "3"))
STALL_MINUTES = int(os.environ.get("SENDER_STALL_MINUTES", "20"))


def _execute(con, s, worker):
    try:
        override = json.loads(s["sender_override_json"]) if s.get("sender_override_json") else None
        res = TL.send_list(con, s["tenant_id"], s["list_id"], s["subject"], s["body"],
                           dry_run=bool(s["dry_run"]), track_clicks=bool(s["track_clicks"]),
                           sender_template_id=s["sender_template_id"],
                           allow_no_solicit=bool(s.get("allow_no_solicit")),
                           cancel_recent_days=s.get("cancel_recent_days"),
                           sender_override=override,
                           skip_already_sent=bool(s.get("resumed")),
                           # ワーカースレッドが会社ごとに呼ぶ。渡される接続はそのスレッド専用
                           stop_check=lambda con_t: db.stop_requested_for(con_t, s["id"]))
        if res is None:
            db.finish_scheduled_send(con, s["id"], "FAILED",
                                     {"error": "リストが見つかりません(削除された可能性)"})
            print(f"  [{worker}] 予約{s['id']}: 失敗(リストが見つかりません)")
        elif "error" in res:
            _fail_or_retry(con, s, worker, res["error"])
        elif res.get("stopped_by"):
            # 停止要求で途中で抜けた(T127)。PAUSE→PAUSED(再開できる)、CANCEL→CANCELLED
            final = "PAUSED" if res["stopped_by"] == "PAUSE" else "CANCELLED"
            db.finish_scheduled_send(con, s["id"], final, res)
            stats = res.get("stats") or {}
            print(f"  [{worker}] 予約{s['id']}: {final}(停止要求。送信{stats.get('sent', 0)} "
                  f"未送信{stats.get('unsent_by_stop', 0)} 対象{res.get('target_count', 0)})")
        else:
            db.finish_scheduled_send(con, s["id"], "DONE", res)
            stats = res.get("stats") or {}
            print(f"  [{worker}] 予約{s['id']}: 完了(送信{stats.get('sent', 0)} "
                  f"失敗{stats.get('failed', 0)} 対象{res.get('target_count', 0)})")
    except Exception as e:  # noqa: BLE001
        # 1件の例外で他の予約の実行まで止めない
        _fail_or_retry(con, s, worker, str(e)[:200])


def _fail_or_retry(con, s, worker, error):
    """例外・エラーで終わった予約を、MAX_ATTEMPTS回までは少し待って自動でやり直す(T98)。
    送信済みの会社はsend_list()が飛ばすので、途中から再開する形になる。"""
    attempts = int(s.get("attempts") or 0)
    if attempts < MAX_ATTEMPTS and "リストが見つかりません" not in (error or ""):
        db.requeue_for_retry(con, s["id"], attempts + 1, error)
        print(f"  [{worker}] 予約{s['id']}: 失敗({error}) → {attempts + 1}回目の再試行を60秒後に予約")
    else:
        db.finish_scheduled_send(con, s["id"], "FAILED", {"error": error, "attempts": attempts})
        print(f"  [{worker}] 予約{s['id']}: 失敗({error})")


def run_due(con, worker="cron", quiet=False):
    """期限到来分を取り込めた分だけ実行する。戻り値は実行した件数。"""
    stale_before = (datetime.now() - timedelta(hours=STALE_RUNNING_HOURS)).isoformat(timespec="seconds")
    requeued = db.requeue_stale_running(con, stale_before)
    if requeued:
        print(f"  [{worker}] RUNNINGのまま{STALE_RUNNING_HOURS}時間以上経過した予約{requeued}件をPENDINGへ戻しました")
    now_iso = datetime.now().isoformat(timespec="seconds")
    due = db.due_scheduled_sends(con, now_iso)
    if not due:
        if not quiet:
            print("期限到来分の予約はありません")
        return 0
    done = 0
    for s in due:
        if not db.claim_scheduled_send(con, s["id"], worker):
            continue  # 別のワーカーが先に取り込んだ
        _execute(con, s, worker)
        done += 1
    return done


def _worker_main(idx, interval):
    worker = f"{socket.gethostname()}-{os.getpid()}-{idx}"
    stop = {"flag": False}

    def _stop(signum, frame):  # noqa: ARG001
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    con = db.connect()
    db.migrate(con)
    print(f"[{worker}] 送信ワーカー起動(interval={interval}s)")
    while not stop["flag"]:
        try:
            n = run_due(con, worker=worker, quiet=True)
        except Exception as e:  # noqa: BLE001
            # DB接続断など。プロセスは落とさず次の周回で再試行する
            print(f"[{worker}] 周回中の例外: {e}")
            n = 0
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval)
            con = db.connect()
            continue
        if n == 0:
            time.sleep(interval)
    print(f"[{worker}] 停止")


def loop(workers, interval):
    """N個の子プロセスで常駐する。子が落ちたら起動し直す(supervisor不要)。"""
    procs = {}

    def _spawn(i):
        p = multiprocessing.Process(target=_worker_main, args=(i, interval), daemon=False)
        p.start()
        procs[i] = p

    # 起動時: 前回の実行中(RUNNING)に取り残された予約を即PENDINGへ戻す(T96)。
    # デプロイで再起動されると実行中の大量送信が3時間止まっていたため
    con = db.connect()
    db.migrate(con)
    requeued = db.requeue_all_running(con)
    if requeued:
        print(f"[loop] 前回実行中のまま残っていた予約{requeued}件をPENDINGへ戻しました(送信済みは飛ばして再開)")
    con.close()

    for i in range(workers):
        _spawn(i)
    stopping = {"flag": False}

    def _stop(signum, frame):  # noqa: ARG001
        stopping["flag"] = True
        for p in procs.values():
            if p.is_alive():
                p.terminate()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # 固まり検知(T98): RUNNINGの予約ごとに「処理した会社数」を覚えておき、STALL_MINUTES分
    # 増えなければその子ワーカーを止めて起動し直す(止まった子の予約は下の再起動処理で
    # PENDINGへ戻る)。ページ読み込みが戻らない等で子がハングすると、コンテナ再起動でも
    # 子の死亡でもないため他の仕組みでは拾えない
    progress = {}
    last_watch = time.time()

    def _watch_stalls():
        con = db.connect()
        try:
            running = db.running_sends_with_progress(con)
            alive_ids = set()
            for r in running:
                alive_ids.add(r["id"])
                prev = progress.get(r["id"])
                if prev is None or prev[0] != r["processed"]:
                    progress[r["id"]] = (r["processed"], time.time())
                    continue
                if time.time() - prev[1] < STALL_MINUTES * 60:
                    continue
                for i, p in list(procs.items()):
                    if r["worker"] == f"{socket.gethostname()}-{p.pid}-{i}" and p.is_alive():
                        print(f"[loop] 予約{r['id']}の処理数が{STALL_MINUTES}分増えていない"
                              f"(処理済み{r['processed']}社)。ワーカー{i}を止めて起動し直します")
                        p.terminate()
                        p.join(15)
                        if p.is_alive():
                            p.kill()
                            p.join(5)
                        progress.pop(r["id"], None)
            for k in list(progress):
                if k not in alive_ids:
                    progress.pop(k, None)
        finally:
            con.close()

    while not stopping["flag"]:
        time.sleep(5)
        if time.time() - last_watch >= 60 and not stopping["flag"]:
            last_watch = time.time()
            try:
                _watch_stalls()
            except Exception as e:  # noqa: BLE001
                print(f"[loop] 固まり検知でエラー: {e}")
        for i, p in list(procs.items()):
            if not p.is_alive() and not stopping["flag"]:
                print(f"[loop] ワーカー{i}が終了(code={p.exitcode})。起動し直します")
                # 死んだ子が実行中だった予約を即PENDINGへ戻す(T96)。OOM等で子だけ落ちると
                # コンテナは再起動されないため、requeue_all_running(起動時)では拾えない
                try:
                    con = db.connect()
                    n = db.requeue_running_by_worker(con, f"{socket.gethostname()}-{p.pid}-{i}")
                    con.close()
                    if n:
                        print(f"[loop] ワーカー{i}が実行中だった予約{n}件をPENDINGへ戻しました(送信済みは飛ばして再開)")
                except Exception as e:  # noqa: BLE001
                    print(f"[loop] 予約の戻しに失敗: {e}")
                _spawn(i)
    for p in procs.values():
        p.join(timeout=30)


def list_pending(con):
    rows = con.execute("""SELECT s.id, s.tenant_id, tn.name tenant_name, s.list_id, tl.name list_name,
            s.scheduled_at, s.dry_run, s.status, s.worker, s.claimed_at, s.stop_requested
        FROM scheduled_sends s
        LEFT JOIN tenants tn ON tn.id = s.tenant_id
        LEFT JOIN target_lists tl ON tl.id = s.list_id
        WHERE s.status IN ('PENDING','RUNNING','PAUSED') ORDER BY s.scheduled_at""").fetchall()
    if not rows:
        print("PENDING/RUNNING/PAUSEDの予約はありません")
        return
    for r in rows:
        mode = "ドライラン" if r["dry_run"] else "本番送信"
        extra = f" 実行中: {r['worker']}({r['claimed_at']})" if r["status"] == "RUNNING" else ""
        if r["stop_requested"]:
            extra += f" ← 停止要求({r['stop_requested']})を受けて抜けるところ"
        print(f"  #{r['id']} {r['scheduled_at']} {mode} [{r['status']}] "
              f"テナント={r['tenant_name'] or r['tenant_id']} "
              f"リスト={r['list_name'] or r['list_id']}{extra}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run-due")
    sub.add_parser("list")
    for name in ("stop", "cancel", "resume"):
        sub.add_parser(name).add_argument("scheduled_id", type=int)
    cp = sub.add_parser("clone")
    cp.add_argument("scheduled_id", type=int)
    cp.add_argument("--cancel-recent-days", type=int, default=None)
    cp.add_argument("--at", default=None, help="開始日時(ISO)。未指定なら今すぐ")
    lp = sub.add_parser("loop")
    lp.add_argument("--workers", type=int, default=int(os.environ.get("SENDER_WORKERS", "1")))
    lp.add_argument("--interval", type=int, default=int(os.environ.get("SENDER_POLL_INTERVAL", "15")))
    args = ap.parse_args()

    if args.cmd == "loop":
        loop(max(1, args.workers), max(3, args.interval))
    else:
        con = db.connect(); db.migrate(con)
        if args.cmd == "run-due":
            run_due(con)
        elif args.cmd == "list":
            list_pending(con)
        elif args.cmd in ("stop", "cancel"):
            mode = "PAUSE" if args.cmd == "stop" else "CANCEL"
            ok = db.request_stop_scheduled_send(con, None, args.scheduled_id, mode)
            print(f"予約{args.scheduled_id}: " + ("要求を受け付けました" if ok else
                  "対象がありません(PENDING/PAUSED/RUNNINGの予約だけ止められます)"))
            list_pending(con)
        elif args.cmd == "clone":
            src = con.execute("SELECT id, tenant_id, list_id, subject, dry_run, cancel_recent_days "
                              "FROM scheduled_sends WHERE id=?", (args.scheduled_id,)).fetchone()
            if not src:
                print(f"予約{args.scheduled_id}はありません")
                raise SystemExit(1)
            days = args.cancel_recent_days if args.cancel_recent_days is not None else src["cancel_recent_days"]
            cnt = TL.count_send_targets(con, src["tenant_id"], src["list_id"], cancel_recent_days=days)
            if not cnt:
                print("元の予約のリストが見つかりません(削除された可能性)")
                raise SystemExit(1)
            print(f"元の予約 #{src['id']}: リスト{src['list_id']} 件名「{(src['subject'] or '')[:30]}」 "
                  f"{'ドライラン' if src['dry_run'] else '本番送信'}")
            print(f"過去送信対象キャンセル: {days if days else 'なし'}日 → 送る社数 {cnt['target_count']:,}社 "
                  f"(除外 {cnt['cancelled_recent']:,}社、うち完了未確認 {cnt.get('cancelled_unconfirmed', 0):,}社)")
            new_id = db.clone_scheduled_send(con, args.scheduled_id, cancel_recent_days=days,
                                             scheduled_at=args.at)
            print(f"予約 #{new_id} を作りました(開始: {args.at or '今すぐ'})")
            list_pending(con)
        elif args.cmd == "resume":
            ok = db.resume_scheduled_send(con, None, args.scheduled_id)
            print(f"予約{args.scheduled_id}: " + ("順番待ちへ戻しました" if ok else
                  "対象がありません(PAUSEDの予約だけ再開できます)"))
            list_pending(con)
