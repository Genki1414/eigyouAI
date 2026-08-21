"""
announcements_cli.py — お知らせ(全テナント共通の告知)の投稿・管理
list_builder.htmlの「お知らせ一覧」ページに出す内容はここから入れる。
このプロジェクトの方針どおり、管理用のWeb画面は作らずCLIで運用する
(suppress_cli.py・offers.pyと同じ考え方)。

使い方:
  python3 announcements_cli.py add --title "メンテナンスのお知らせ" --body "8/25 2:00-3:00に..."
  python3 announcements_cli.py list              # 公開中のみ
  python3 announcements_cli.py list --all        # 非公開も含めて全件
  python3 announcements_cli.py unpublish --id 3  # 取り下げ(削除はしない)
  python3 announcements_cli.py publish --id 3    # 再公開
"""
import argparse

import db


def add(con, args):
    aid = db.add_announcement(con, args.title, args.body, published=not args.draft)
    state = "下書き" if args.draft else "公開"
    print(f"お知らせを追加しました (id={aid} / {state})")


def listing(con, show_all):
    rows = db.list_announcements(con, published_only=not show_all)
    print(f"お知らせ: {len(rows)}件\n")
    for r in rows:
        state = "" if r["published"] else " [非公開]"
        print(f"  #{r['id']}  {r['created_at'][:16]}{state}\n  {r['title']}\n  {r['body']}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--title", required=True)
    a.add_argument("--body", required=True)
    a.add_argument("--draft", action="store_true", help="公開せず下書きとして保存する")
    lst = sub.add_parser("list")
    lst.add_argument("--all", action="store_true", help="非公開のものも含めて表示")
    for name in ("publish", "unpublish"):
        p = sub.add_parser(name)
        p.add_argument("--id", type=int, required=True)
    args = ap.parse_args()

    con = db.connect(); db.migrate(con)
    if args.cmd == "add":
        add(con, args)
    elif args.cmd == "list":
        listing(con, args.all)
    elif args.cmd == "publish":
        ok = db.set_announcement_published(con, args.id, True)
        print("公開しました" if ok else "見つかりません")
    elif args.cmd == "unpublish":
        ok = db.set_announcement_published(con, args.id, False)
        print("非公開にしました" if ok else "見つかりません")
