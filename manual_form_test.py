"""
manual_form_test.py — FormSender実サイト検証用の使い捨てスクリプト
本番投入前の少数テストのためだけの一時ファイル。検証が終わったら削除してよい。

使い方(コンテナ内で):
  python3 manual_form_test.py v5
  python3 manual_form_test.py v6 --company-id 12
"""
import argparse

import db as D
import senders as S

SUBJECT = "積算の拾い出し、無料のAIツールで試してみませんか"
BODY = """{name} ご担当者様

急な連絡失礼いたします。足場の積算ツールを作っているAshiBaseと申します。

東京都でも職人不足で現場が詰まっているという話をよく伺います。そんな中で積算の拾い出しに時間を取られるのはもったいないと思い、寸法を入力するだけで部材と数量が出るツールを無料で開放しました。

▼メールアドレスだけで使えます
https://sekisan.ashibase.jp/

AshiBase（足場ベース）"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("key_suffix", help="冪等キーの末尾(再試行のたびに変える。例: v5)")
    ap.add_argument("--company-id", type=int, default=5)
    args = ap.parse_args()

    con = D.connect()
    row = con.execute(
        "SELECT id, name, contact_url FROM companies WHERE id=?", (args.company_id,)
    ).fetchone()
    if not row:
        print(f"company_id={args.company_id} が見つかりません")
        return

    adapter = S.FormSender(con, dry_run=False)
    sender = S.Sender(
        name="AshiBase（足場ベース）",
        email="info@tohoku-mikamikizai.co.jp",
        address="",
        optout_url="本フォームまたは上記メールアドレスへご返信ください")
    to = S.Recipient(company_id=row["id"], name=row["name"], contact_url=row["contact_url"])

    res = adapter.send(to, sender, SUBJECT, BODY.format(name=row["name"]),
                        f"manual_test:company{row['id']}:{args.key_suffix}")
    print(f"対象: {row['name']} ({row['contact_url']})")
    print(res)


if __name__ == "__main__":
    main()
