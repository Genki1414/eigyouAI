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


def _window(args):
    """集計の開始時刻を決める。--since があればそれを優先する。

    --days は**現在時刻からのスライド窓**なので、時間をおいて2回実行すると
    母集団が変わり、比べても意味のない数字になる(2026-09-23に実際にやった:
    改修の前後を --days 1 で比べたが、前半が窓から外れて件数が減っただけだった)。
    改修の効果を測るときは --since で固定の開始時刻を渡すこと。"""
    if getattr(args, "since", None):
        return args.since
    return _since(args.days)


def _window_label(args):
    if getattr(args, "since", None):
        return f"{args.since} 以降"
    return f"直近{args.days}日"


def cmd_reasons(con, args):
    rows = con.execute("""SELECT status, reason_code, COUNT(*) n
        FROM form_send_log WHERE started_at >= ?
        GROUP BY status, reason_code ORDER BY n DESC""", (_window(args),)).fetchall()
    total = sum(r["n"] for r in rows)
    if not total:
        print(f"{_window_label(args)}の送信ログはありません")
        return
    print(f"{_window_label(args)}の試行 {total:,}件(1試行=1行。再試行のたびに増えます)")
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
    params = [_window(args)]
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


def cmd_delivery(con, args):
    """「どこまで進めたか」を会社数で出す(2026-09-21)。

    成功の数え方はサービスによって違う。ヒラケルは完了文言・URL変化・フォーム消失で
    判定しているが、他社が「送信処理が通った率」を成功と呼んでいる場合、それは
    **送信ボタンを押せた会社の割合**に相当する。同じものさしで比べられるよう、
    1試行=1行のログを**会社単位**に畳んで段階別に出す(再試行や重複送信で
    水増しされないよう COUNT(DISTINCT company_id) で数える)。"""
    since = _window(args)
    row = con.execute("""SELECT
            COUNT(DISTINCT company_id) companies,
            COUNT(DISTINCT CASE WHEN submit_attempted=1 THEN company_id END) submitted,
            COUNT(DISTINCT CASE WHEN status='SUCCESS' THEN company_id END) success_any,
            COUNT(DISTINCT CASE WHEN reason_code='success_text_matched'
                  THEN company_id END) success_text,
            COUNT(DISTINCT CASE WHEN status='SUCCESS'
                  AND reason_code='url_changed_after_submit' THEN company_id END) success_url,
            COUNT(DISTINCT CASE WHEN reason_code='success_not_confirmed'
                  THEN company_id END) unconfirmed,
            COUNT(DISTINCT CASE WHEN reason_code IN
                  ('error_message_detected','required_field_empty','submit_click_failed')
                  THEN company_id END) blocked
        FROM form_send_log WHERE started_at >= ?""", (since,)).fetchone()
    n = row["companies"]
    if not n:
        print(f"{_window_label(args)}の送信ログはありません")
        return

    def pct(x):
        return f"{x*100.0/n:5.1f}%"

    print(f"集計範囲: {_window_label(args)}")
    print(f"対象 {n:,}社(会社単位。再試行・重複は畳んで数えています)")
    print("-" * 74)
    print(f"  送信ボタンを押せた            {row['submitted']:6,d}社  {pct(row['submitted'])}"
          "   ←『送信処理が通った率』はこれに相当")
    print("  （内訳。同じ会社が複数回試行されると重複しうるため合計は一致しません）")
    print(f"    完了文言を確認(確度high)    {row['success_text']:6,d}社  {pct(row['success_text'])}")
    print(f"    URL変化のみ(確度low)        {row['success_url']:6,d}社  {pct(row['success_url'])}")
    print(f"    完了を確認できない          {row['unconfirmed']:6,d}社  {pct(row['unconfirmed'])}")
    print(f"    押したが相手に弾かれた      {row['blocked']:6,d}社  {pct(row['blocked'])}"
          "   ←届いていない")
    print("-" * 74)
    print(f"  ヒラケルが『成功』と記録      {row['success_any']:6,d}社  {pct(row['success_any'])}")
    reached = row["success_any"] + row["unconfirmed"]
    print(f"  届いた可能性がある上限        {reached:6,d}社  {pct(reached)}"
          "   (成功 + 未確認)")
    print()
    print("※『送信ボタンを押せた』は届いた数ではありません。押した後に相手のフォームが")
    print("  『入力内容に問題があります』等で弾いた分(上の『弾かれた』)が含まれます。")
    print("  他社が『送信処理が通った率』を成功と呼んでいる場合、その数字にはこの分が")
    print("  入っている可能性があるため、比べるときはものさしを確認してください。")


def cmd_error_hints(con, args):
    """error_message_detected で「どの文言を検知したか」の内訳(T110で残った宿題)。

    form_navigator._ERROR_HINTS には「再度お試しください」「確認して再度」のように
    **完了ページにも出うる文言**が入っている。そこに引っかかっていると、実際には
    送信できているのに失敗として記録していることになる。件数の多い文言から順に、
    それが本当にエラーなのかを人が判断するための出力。

    2026-09-23から error_message の末尾に「[欄: name=文言 / ...]」(どの欄がどう弾かれたか)
    が付く。総括文言はその前で切って集計し、欄ごとの文言は別に数える。"""
    reason = getattr(args, "reason", None) or "error_message_detected"
    rows = con.execute("""SELECT error_message, COUNT(*) n FROM form_send_log
        WHERE reason_code = ? AND started_at >= ?
        GROUP BY error_message ORDER BY n DESC""",
        (reason, _window(args))).fetchall()
    if not rows:
        print(f"{reason} の行はありません")
        return
    summary, fields = {}, {}
    for r in rows:
        msg = (r["error_message"] or "").replace("送信後ページにエラー文言を検知: ", "")
        head, _, tail = msg.partition(" [欄: ")
        summary[head] = summary.get(head, 0) + r["n"]
        if tail:
            for item in tail.rstrip("]").split(" / "):
                fields[item] = fields.get(item, 0) + r["n"]
    total = sum(summary.values())
    top = sorted(summary.items(), key=lambda kv: -kv[1])[:args.limit]
    print(f"検知した文言の内訳(上位{len(top)}種 / 合計{total:,}件)")
    if reason == "error_message_detected":
        print("※「再度お試しください」等が上位にある場合、完了ページを失敗と誤判定している"
              "可能性があります(form_navigator._ERROR_HINTS を見直す)")
    print("-" * 92)
    for head, n in top:
        print(f"  {n:6,d}件  {head[:70]}")
    if fields:
        top_f = sorted(fields.items(), key=lambda kv: -kv[1])[:args.limit]
        print()
        print(f"弾かれた欄と文言(上位{len(top_f)}種。欄の名前=相手サイトの文言。入力値は含まない)")
        print("-" * 92)
        for item, n in top_f:
            print(f"  {n:6,d}件  {item[:80]}")
    elif reason == "error_message_detected":
        print()
        print("※ 欄ごとの文言はまだありません(2026-09-23のデプロイ以降の送信から残ります)")


def cmd_sender_fields(con, args):
    """送信元テンプレートのどの欄が埋まっているか(あり/なし だけ。値は出さない)。

    弾かれる原因の切り分け用(2026-09-23)。相手フォームの必須欄(電話・フリガナ・
    郵便番号など)は分類できているのに「入力内容に問題があります」になる場合、
    送信元テンプレート側が空で、空のまま送っていることがある。"""
    cols = [("sender_name", "会社名/氏名"), ("sender_email", "メール"), ("sender_phone", "電話"),
            ("sender_last_name", "姓"), ("sender_first_name", "名"),
            ("sender_last_name_kana", "姓カナ"), ("sender_first_name_kana", "名カナ"),
            ("sender_postal_code", "郵便番号"), ("sender_prefecture", "都道府県"),
            ("sender_city", "市区町村"), ("sender_block", "丁目番地"), ("sender_building", "建物"),
            ("sender_address", "住所(単一)"), ("sender_department", "部署"), ("sender_position", "役職")]
    sel = ", ".join(c for c, _ in cols)

    def _report(label, r):
        missing = [lab for c, lab in cols if not (r[c] or "").strip()]
        print(f"  {label} 空の欄: {'・'.join(missing) if missing else 'なし(全部埋まっている)'}")
        # 相手フォームで必須になりやすい欄が空なら、はっきり言う
        risky = [lab for c, lab in cols if c in ("sender_phone", "sender_last_name_kana",
                                                   "sender_postal_code") and not (r[c] or "").strip()]
        if risky:
            print(f"      ⚠ {'・'.join(risky)} が空。これらを必須にしているフォームでは空のまま送って弾かれます")

    # 実際に送信で使われるのは tenants.sender_*(「有効化」でテンプレートから写した値)。
    # 送信時にテンプレートを指定した場合だけ sender_templates の値が使われる
    print("■ テナントの送信元(有効化済み。通常の送信で使われる値。値は出しません)")
    for r in con.execute(f"SELECT id, {sel} FROM tenants ORDER BY id").fetchall():
        _report(f"テナント{r['id']}", r)
    print()
    print("■ 送信元テンプレート(送信時に指定したときだけ使われる)")
    rows = con.execute(f"SELECT id, tenant_id, {sel} FROM sender_templates ORDER BY tenant_id, id").fetchall()
    if not rows:
        print("  なし")
    for r in rows:
        _report(f"#{r['id']} (テナント{r['tenant_id']})", r)

def cmd_runs(con, args):
    """実行(リスト)単位の成績。どの送信がいつ、どれだけ通ったか。"""
    rows = con.execute("""SELECT list_id,
            MIN(started_at) t0, MAX(started_at) t1, COUNT(*) n,
            SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) ok,
            COUNT(DISTINCT company_id) companies
        FROM form_send_log WHERE started_at >= ?
        GROUP BY list_id ORDER BY t1 DESC LIMIT ?""",
        (_window(args), args.limit)).fetchall()
    if not rows:
        print(f"{_window_label(args)}の送信ログはありません")
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
    p1.add_argument("--since", help="開始時刻(例 2026-09-23T15:05:00)。--daysより優先")

    p2 = sub.add_parser("urls", help="指定した理由のURLを検証用に出す")
    p2.add_argument("--reason", default=None)
    p2.add_argument("--status", default=None)
    p2.add_argument("--days", type=int, default=30)
    p2.add_argument("--since", help="開始時刻(例 2026-09-23T15:05:00)。--daysより優先")
    p2.add_argument("--limit", type=int, default=20)

    p5 = sub.add_parser("delivery", help="どこまで進めたかを会社数で(他社比較用)")
    p5.add_argument("--days", type=int, default=30)
    p5.add_argument("--since", help="開始時刻(例 2026-09-23T15:05:00)。--daysより優先")

    p4 = sub.add_parser("error-hints", help="エラー文言の内訳(誤検出の確認)")
    p4.add_argument("--reason", default="error_message_detected",
                    help="集計する reason_code。success_not_confirmed にすると「押しても何も"
                         "起きない」の手がかり(reCAPTCHA v3の有無など)の内訳が出る(2026-09-23)")
    p4.add_argument("--days", type=int, default=30)
    p4.add_argument("--since", help="開始時刻(例 2026-09-23T15:05:00)。--daysより優先")
    p4.add_argument("--limit", type=int, default=30)

    p6 = sub.add_parser("sender-fields", help="送信元テンプレートのどの欄が埋まっているか(値は出さない)")

    p3 = sub.add_parser("runs", help="実行(リスト)単位の成績")
    p3.add_argument("--days", type=int, default=30)
    p3.add_argument("--since", help="開始時刻(例 2026-09-23T15:05:00)。--daysより優先")
    p3.add_argument("--limit", type=int, default=20)

    args = ap.parse_args()
    con = db.connect()
    try:
        {"reasons": cmd_reasons, "urls": cmd_urls, "runs": cmd_runs,
         "error-hints": cmd_error_hints,
         "sender-fields": cmd_sender_fields, "delivery": cmd_delivery}[args.cmd](con, args)
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
