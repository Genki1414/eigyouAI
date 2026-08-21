"""
cost_report_cli.py — 1送信あたりの原価・粗利の集計(管理者専用)

form_send_log(1試行=1行、retryのたびに増える)に記録された execution_seconds/
ai_cost_yen/external_api_cost_yen/estimated_server_cost_yen を集計するだけで、
新しい集計用テーブルは作らない。原価情報は顧客向け画面には出さない
(list_builder.htmlのテナントAPIには一切露出していない。このCLIのみで見る)。

使い方:
  python3 cost_report_cli.py overall              # 全体・今月
  python3 cost_report_cli.py overall --all-time    # 全体・累計
  python3 cost_report_cli.py by-tenant             # テナント別・今月
  python3 cost_report_cli.py profit --tenant 3 --monthly-fee 49800
                                                    # 1テナントの粗利試算
"""
import argparse
from datetime import datetime

import db


def _month_start():
    return datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0) \
        .isoformat(timespec="seconds")


def overall(con, all_time=False):
    where = "1=1" if all_time else "started_at>=?"
    params = [] if all_time else [_month_start()]
    row = con.execute(f"""SELECT
            COUNT(DISTINCT company_id) targeted,
            COUNT(*) attempts,
            SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) success,
            SUM(total_estimated_cost_yen) total_cost
        FROM form_send_log WHERE {where}""", params).fetchone()
    targeted = row["targeted"] or 0
    success = row["success"] or 0
    total_cost = row["total_cost"] or 0.0
    label = "累計" if all_time else "今月"
    print(f"── 全体({label}) ──")
    print(f"  送信対象企業数: {targeted:,}社")
    print(f"  送信試行数: {row['attempts'] or 0:,}件")
    print(f"  SUCCESS: {success:,}件")
    print(f"  総推定原価: ¥{total_cost:,.2f}")
    if targeted:
        print(f"  1送信対象あたり平均原価: ¥{total_cost / targeted:,.2f}")
    if success:
        print(f"  SUCCESS 1件あたり平均原価: ¥{total_cost / success:,.2f}")
    if not targeted:
        print("  (該当データがまだありません)")


def by_tenant(con):
    print("── テナント別(今月) ──")
    rows = con.execute("""SELECT l.tenant_id, t.name tenant_name,
            COUNT(*) attempts,
            SUM(CASE WHEN l.status='SUCCESS' THEN 1 ELSE 0 END) success,
            SUM(l.total_estimated_cost_yen) total_cost
        FROM form_send_log l LEFT JOIN tenants t ON t.id = l.tenant_id
        WHERE l.started_at>=? AND l.tenant_id IS NOT NULL
        GROUP BY l.tenant_id ORDER BY total_cost DESC""", (_month_start(),)).fetchall()
    if not rows:
        print("  (該当データがまだありません)")
        return
    for r in rows:
        cost = r["total_cost"] or 0.0
        per_send = cost / r["attempts"] if r["attempts"] else 0
        print(f"  tenant_id={r['tenant_id']} ({r['tenant_name'] or '?'}): "
              f"送信{r['attempts']:,}件 / SUCCESS{r['success'] or 0:,}件 / "
              f"月間推定原価¥{cost:,.2f} / 1送信あたり¥{per_send:,.2f}")


def profit(con, tenant_id, monthly_fee_yen):
    row = con.execute("""SELECT COUNT(*) attempts,
            SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) success,
            SUM(total_estimated_cost_yen) total_cost
        FROM form_send_log WHERE tenant_id=? AND started_at>=?""",
        (tenant_id, _month_start())).fetchone()
    cost = row["total_cost"] or 0.0
    gross_profit = monthly_fee_yen - cost
    margin = (gross_profit / monthly_fee_yen * 100) if monthly_fee_yen else 0
    tname = con.execute("SELECT name FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    print(f"── テナント{tenant_id}({tname['name'] if tname else '?'})の今月の粗利試算 ──")
    print(f"  今月送信対象企業数: {row['attempts'] or 0:,}件(SUCCESS {row['success'] or 0:,}件)")
    print(f"  月額売上: ¥{monthly_fee_yen:,}")
    print(f"  推定原価(サーバー按分・AI・外部API合計): ¥{cost:,.2f}")
    print(f"  推定粗利: ¥{gross_profit:,.2f}")
    print(f"  粗利率: 約{margin:.1f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("overall")
    o.add_argument("--all-time", action="store_true")
    sub.add_parser("by-tenant")
    p = sub.add_parser("profit")
    p.add_argument("--tenant", type=int, required=True)
    p.add_argument("--monthly-fee", type=float, required=True)
    args = ap.parse_args()

    con = db.connect(); db.migrate(con)
    if args.cmd == "overall":
        overall(con, all_time=args.all_time)
    elif args.cmd == "by-tenant":
        by_tenant(con)
    elif args.cmd == "profit":
        profit(con, args.tenant, args.monthly_fee)
