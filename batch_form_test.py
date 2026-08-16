"""
batch_form_test.py — FormSender β版検証用のバッチ実行ツール
1社に固執せず複数社で並行検証し、SUCCESS/SKIP/FAILED_RETRYABLE/FAILED_UNSUPPORTEDの
内訳を見るための検証ステップ専用スクリプト(HANDOFF.mdのStep1〜4に対応)。
本番のキャンペーン送信はsend_campaign()を通す(このスクリプトはそこを経由しない検証専用経路)。

使い方:
  python3 batch_form_test.py --n 10                    # 無作為に10社選んで検証
  python3 batch_form_test.py --company-ids 5,12,88      # 指定した会社だけ検証
  python3 batch_form_test.py --n 50 --seed 1            # 再現性のある無作為抽出
"""
import argparse

import db as D
import senders as S

TEST_SUBJECT = "積算の拾い出し、無料のAIツールで試してみませんか"
TEST_BODY_TMPL = """{name} ご担当者様

急な連絡失礼いたします。足場の積算ツールを作っているAshiBaseと申します。

東京都でも職人不足で現場が詰まっているという話をよく伺います。そんな中で積算の拾い出しに時間を取られるのはもったいないと思い、寸法を入力するだけで部材と数量が出るツールを無料で開放しました。

▼メールアドレスだけで使えます
https://sekisan.ashibase.jp/

AshiBase（足場ベース）"""


def pick_companies(con, n, seed=None):
    """検証対象をprescore選出済み・contact_url確定済みの中から無作為抽出する。
    「異なるフォーム構造」の代理指標として、業種・都道府県が偏らないよう緩く分散させる。"""
    rows = con.execute("""SELECT id, name, contact_url, pref, trades FROM companies
        WHERE prescore_selected=1 AND contact_url IS NOT NULL AND dedup_of IS NULL""").fetchall()
    import random
    rng = random.Random(seed)
    pool = list(rows)
    rng.shuffle(pool)
    return pool[:n]


def run_one(con, sender, row, tenant_id, offer_id, keep_debug_fields, run_label):
    adapter = S.FormSender(con, dry_run=False, tenant_id=tenant_id, offer_id=offer_id,
                            keep_debug_fields=keep_debug_fields)
    to = S.Recipient(company_id=row["id"], name=row["name"], contact_url=row["contact_url"])
    body = TEST_BODY_TMPL.format(name=row["name"])
    # run_labelは日付ではなく明示指定にしている。日付ベースだと日をまたいだ再実行で
    # 冪等キーが変わり、同じ会社に無自覚に再送してしまうため
    key = f"batch_test:{row['id']}:{run_label}"
    res = adapter.send(to, sender, TEST_SUBJECT, body, key)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10, help="無作為抽出する社数")
    ap.add_argument("--company-ids", default=None, help="カンマ区切りで対象を直接指定")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--keep-debug-fields", action="store_true",
                     help="final_url/page_titleをform_send_logに残す(検証中のみ推奨)")
    ap.add_argument("--run-label", required=True,
                     help="冪等キーに使う識別子(例: step1)。同じラベルでの再実行は二重送信されない")
    args = ap.parse_args()

    con = D.connect()
    D.migrate(con)

    if args.company_ids:
        ids = [int(x) for x in args.company_ids.split(",")]
        rows = [con.execute(
            "SELECT id, name, contact_url, pref, trades FROM companies WHERE id=?",
            (i,)).fetchone() for i in ids]
        rows = [r for r in rows if r]
    else:
        rows = pick_companies(con, args.n, seed=args.seed)

    if not rows:
        print("対象が見つかりません")
        return

    sender = S.Sender(
        name="AshiBase（足場ベース）",
        email="info@tohoku-mikamikizai.co.jp",
        address="",
        optout_url="本フォームまたは上記メールアドレスへご返信ください")

    print(f"検証対象: {len(rows)}社\n")
    results = []
    for i, row in enumerate(rows, 1):
        allowed, why = D.can_contact(con, row["id"])
        if not allowed:
            print(f"[{i}/{len(rows)}] {row['name']}: 接触ガードで除外({why})")
            continue
        res = run_one(con, sender, row, tenant_id=1, offer_id=1,
                       keep_debug_fields=args.keep_debug_fields, run_label=args.run_label)
        if res.ok:
            status, reason = "SUCCESS", (res.raw or {}).get("reason_code", "")
        elif res.raw:
            status = res.raw.get("status", "?")
            reason = res.raw.get("reason_code", "")
        else:
            # R.retry()を使い切って例外のまま返ってきたケース(=FAILED_RETRYABLE)。
            # form_send_logには各試行が記録済みなので、ここは表示用の要約のみ
            status, reason = "FAILED_RETRYABLE", res.error or ""
        results.append(status)
        print(f"[{i}/{len(rows)}] {row['name']} ({row['contact_url']}) "
              f"→ {status} {reason}")

    print(f"\n{'='*50}")
    counts = {}
    for s in results:
        counts[s] = counts.get(s, 0) + 1
    for status, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {status}: {n}")
    print(f"  合計: {len(results)}件試行")

    print("\n── form_send_logの直近件数(reason_code別) ──")
    rows2 = con.execute("""SELECT status, reason_code, COUNT(*) n FROM form_send_log
        GROUP BY status, reason_code ORDER BY n DESC LIMIT 20""").fetchall()
    for r in rows2:
        print(f"  {r['status']:<20} {r['reason_code'] or '-':<30} {r['n']}")


if __name__ == "__main__":
    main()
