"""
send_log_report_cli.py — 送信ログ(form_send_log)の集計を標準出力に出すだけのCLI

「なぜ送れなかったのか」を、管理画面やCSVを開けない場所からでも把握するために
用意した参照専用のツール。`.github/workflows/ops-readonly.yml` の send-stats から
呼ばれ、結果はGitHub Actionsのログに出る。

**個人情報・営業情報は出さない**。出すのは理由別の件数と、検証用に
問い合わせ先URL(公開されている企業サイトのURL)だけ。会社名・送信本文・
メールアドレス・電話番号は一切出力しない。読み取り専用で、DBは一切変更しない。

使い方:
  python3 send_log_report_cli.py reasons                 # 理由別の件数(直近30日)
  python3 send_log_report_cli.py reasons --days 3
  python3 send_log_report_cli.py urls --reason form_not_found --limit 20
                                                          # その理由のURLを検証用に出す
  python3 send_log_report_cli.py runs                    # 実行(リスト)単位の成績
"""
import argparse
from datetime import datetime, timedelta

import db
from target_lists import REASON_LABELS_JA

# 成功側の理由。REASON_LABELS_JA(失敗理由の辞書)の意味を変えたくないのでここに持つ。
_SUCCESS_LABELS_JA = {
    "success_text_matched": "完了文言を確認できた(確度high)",
    "url_changed_after_submit": "送信後にURLが変わった(確度low。押した対象を要確認)",
    "form_disappeared_after_submit": "送信後にフォームが消えた(確度mid)",
}


def _label(code):
    return _SUCCESS_LABELS_JA.get(code) or REASON_LABELS_JA.get(code, "")


def _since(days):
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def cmd_reasons(con, args):
    rows = con.execute("""SELECT status, reason_code, COUNT(*) n
        FROM form_send_log WHERE started_at >= ?
        GROUP BY status, reason_code ORDER BY n DESC""", (_since(args.days),)).fetchall()
    total = sum(r["n"] for r in rows)
    if not total:
        print(f"直近{args.days}日の送信ログはありません")
        return
    print(f"直近{args.days}日の試行 {total:,}件(1試行=1行。再試行のたびに増えます)")
    print(f"{'結果':22s} {'理由':28s} {'件数':>7s} {'割合':>7s}  日本語")
    print("-" * 92)
    for r in rows:
        code = r["reason_code"] or "-"
        ja = _label(code)
        print(f"{r['status']:22s} {code:28s} {r['n']:7,d} {r['n']*100.0/total:6.1f}%  {ja}")
    ok = sum(r["n"] for r in rows if r["status"] == "SUCCESS")
    print("-" * 92)
    print(f"成功 {ok:,}件 / 試行 {total:,}件 = {ok*100.0/total:.1f}%")
    # T117: URL変化だけを根拠にした成功は、押した対象が送信ボタンでなくても成立しうる。
    # 2026-09-21以降は success_evidence に「押した要素」を併記しているので内訳を出す。
    changed = [r for r in rows if r["reason_code"] == "url_changed_after_submit"]
    if changed:
        n = changed[0]["n"]
        print(f"\n※ このうち {n:,}件 は『URLが変わった』だけを根拠にした成功です。")
        print("   本当に送信できたかは urls --reason url_changed_after_submit で"
              "押した要素を確認してください。")


def cmd_urls(con, args):
    """指定した理由の問い合わせ先URLを出す(検証用)。会社名は出さない。"""
    q = """SELECT contact_url, target_url, success_evidence, error_message, started_at
        FROM form_send_log WHERE started_at >= ?"""
    params = [_since(args.days)]
    if args.reason:
        q += " AND reason_code = ?"
        params.append(args.reason)
    if args.status:
        q += " AND status = ?"
        params.append(args.status)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)
    rows = con.execute(q, params).fetchall()
    if not rows:
        print("該当する行がありません")
        return
    print(f"{len(rows)}件(会社名は出しません。URLは公開されている企業サイトのものです)")
    for r in rows:
        url = r["contact_url"] or r["target_url"] or "-"
        print(f"\n  {url}")
        if r["success_evidence"]:
            print(f"      根拠: {str(r['success_evidence'])[:150]}")
        if r["error_message"]:
            print(f"      エラー: {str(r['error_message'])[:150]}")


def cmd_error_hints(con, args):
    """error_message_detected で「どの文言を検知したか」の内訳(T110で残った宿題)。

    form_navigator._ERROR_HINTS には「再度お試しください」「確認して再度」のように
    **完了ページにも出うる文言**が入っている。そこに引っかかっていると、実際には
    送信できているのに失敗として記録していることになる。件数の多い文言から順に、
    それが本当にエラーなのかを人が判断するための出力。"""
    rows = con.execute("""SELECT error_message, COUNT(*) n FROM form_send_log
        WHERE reason_code = 'error_message_detected' AND started_at >= ?
        GROUP BY error_message ORDER BY n DESC LIMIT ?""",
        (_since(args.days), args.limit)).fetchall()
    if not rows:
        print("error_message_detected の行はありません")
        return
    total = sum(r["n"] for r in rows)
    print(f"検知した文言の内訳(上位{len(rows)}種 / 合計{total:,}件)")
    print("※「再度お試しください」等が上位にある場合、完了ページを失敗と誤判定している"
          "可能性があります(form_navigator._ERROR_HINTS を見直す)")
    print("-" * 92)
    for r in rows:
        msg = (r["error_message"] or "").replace("送信後ページにエラー文言を検知: ", "")
        print(f"  {r['n']:6,d}件  {msg[:70]}")


def cmd_runs(con, args):
    """実行(リスト)単位の成績。どの送信がいつ、どれだけ通ったか。"""
    rows = con.execute("""SELECT list_id,
            MIN(started_at) t0, MAX(started_at) t1, COUNT(*) n,
            SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) ok,
            COUNT(DISTINCT company_id) companies
        FROM form_send_log WHERE started_at >= ?
        GROUP BY list_id ORDER BY t1 DESC LIMIT ?""",
        (_since(args.days), args.limit)).fetchall()
    if not rows:
        print(f"直近{args.days}日の送信ログはありません")
        return
    print(f"{'リストID':>8s} {'開始':17s} {'終了':17s} {'試行':>7s} {'会社数':>7s} {'成功':>7s} {'成功率':>7s}")
    print("-" * 80)
    for r in rows:
        rate = r["ok"] * 100.0 / r["n"] if r["n"] else 0
        print(f"{str(r['list_id'] or '-'):>8s} {str(r['t0'])[:16]:17s} {str(r['t1'])[:16]:17s} "
              f"{r['n']:7,d} {r['companies']:7,d} {r['ok']:7,d} {rate:6.1f}%")


def main():
    ap = argparse.ArgumentParser(description="送信ログの集計(参照専用)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("reasons", help="理由別の件数")
    p1.add_argument("--days", type=int, default=30)

    p2 = sub.add_parser("urls", help="指定した理由のURLを検証用に出す")
    p2.add_argument("--reason", default=None)
    p2.add_argument("--status", default=None)
    p2.add_argument("--days", type=int, default=30)
    p2.add_argument("--limit", type=int, default=20)

    p4 = sub.add_parser("error-hints", help="エラー文言の内訳(誤検出の確認)")
    p4.add_argument("--days", type=int, default=30)
    p4.add_argument("--limit", type=int, default=30)

    p3 = sub.add_parser("runs", help="実行(リスト)単位の成績")
    p3.add_argument("--days", type=int, default=30)
    p3.add_argument("--limit", type=int, default=20)

    args = ap.parse_args()
    con = db.connect()
    try:
        {"reasons": cmd_reasons, "urls": cmd_urls, "runs": cmd_runs,
         "error-hints": cmd_error_hints}[args.cmd](con, args)
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
