"""
metrics.py — ファネル / CAC / A-Bテスト集計
ここで出る数字がそのままM&AのIM（企業概要書）に載る。買い手が見るのはこの4つ:
  1. CAC（有料1社あたり獲得コスト）
  2. チャネル別・文面別の反応率（=再現性の証明）
  3. ファネル各段の転換率（=どこを直せば伸びるかが分かる状態）
  4. MRRとその積み上がり速度

使い方: python3 metrics.py            → 全キャンペーン集計 + out/metrics.json
        python3 metrics.py --campaign 1
"""
import argparse, json, sqlite3
from pathlib import Path

DB = Path(__file__).parent / "out" / "companies.db"
OUT = Path(__file__).parent / "out" / "metrics.json"

def pct(a, b):
    return round(a / b * 100, 2) if b else 0.0

def funnel(con, where, params):
    r = con.execute(f"""SELECT COUNT(*) sent, SUM(delivered) delivered,
        SUM(responded) responded, SUM(signed_up) signed_up,
        SUM(activated) activated, SUM(paid) paid,
        SUM(unit_cost_yen) cost, SUM(mrr_yen) mrr
        FROM touches WHERE {where}""", params).fetchone()
    sent, delivered, responded, signed, activated, paid, cost, mrr = [x or 0 for x in r]
    return {
        "sent": sent, "delivered": delivered, "responded": responded,
        "signed_up": signed, "activated": activated, "paid": paid,
        "cost_yen": cost, "mrr_yen": mrr,
        "rate_delivered": pct(delivered, sent),
        "rate_responded": pct(responded, delivered),
        "rate_signup": pct(signed, responded),
        "rate_activated": pct(activated, signed),
        "rate_paid": pct(paid, activated),
        "rate_sent_to_paid": pct(paid, sent),
        "cac_yen": round(cost / paid) if paid else None,
        "ltv_cac": round((mrr * 24) / cost, 2) if cost and mrr else None,  # LTV=24ヶ月想定
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", type=int, default=None)
    a = ap.parse_args()
    con = sqlite3.connect(DB)

    where, params = ("campaign_id=?", [a.campaign]) if a.campaign else ("1=1", [])
    result = {"overall": funnel(con, where, params), "by_channel": {}, "by_variant": {},
              "by_rank": {}, "by_step": {}}

    for (st,) in con.execute(f"SELECT DISTINCT step FROM touches WHERE {where} ORDER BY step", params):
        result["by_step"][str(st)] = funnel(con, f"{where} AND step=?", params + [st])

    for (ch,) in con.execute(f"SELECT DISTINCT channel FROM touches WHERE {where}", params):
        result["by_channel"][ch] = funnel(con, f"{where} AND channel=?", params + [ch])
    for (va,) in con.execute(f"SELECT DISTINCT variant FROM touches WHERE {where}", params):
        result["by_variant"][va] = funnel(con, f"{where} AND variant=?", params + [va])
    for rk in ["S", "A", "B", "C"]:
        result["by_rank"][rk] = funnel(
            con, f"{where} AND company_id IN (SELECT id FROM companies WHERE rank=?)",
            params + [rk])

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    o = result["overall"]
    print(f"""
━━━ ファネル実績 ━━━
 送信      {o['sent']:>5}
 到達      {o['delivered']:>5}  ({o['rate_delivered']}%)
 反応      {o['responded']:>5}  ({o['rate_responded']}% of 到達)
 無料登録  {o['signed_up']:>5}  ({o['rate_signup']}% of 反応)
 積算実行  {o['activated']:>5}  ({o['rate_activated']}% of 登録)
 有料転換  {o['paid']:>5}  ({o['rate_paid']}% of 活性)
━━━ 経済性 ━━━
 実費      {o['cost_yen']:,}円
 MRR       {o['mrr_yen']:,}円
 CAC       {o['cac_yen'] and f"{o['cac_yen']:,}円" or "-"}
 LTV/CAC   {o['ltv_cac'] or "-"}  (LTV=MRR×24ヶ月)

チャネル別 反応率:""")
    for ch, m in sorted(result["by_channel"].items(), key=lambda x: -x[1]["rate_responded"]):
        cac_str = f"{m['cac_yen']:,}円" if m["cac_yen"] else "-"
        print(f"  {ch:<6} {m['rate_responded']:>5}%  送信{m['sent']:>4}  有料{m['paid']:>3}  "
              f"CAC {cac_str}")
    print("\nステップ別（多段フォローの累積効果）:")
    cum_r = cum_p = 0
    for st, m in sorted(result["by_step"].items()):
        cum_r += m["responded"]; cum_p += m["paid"]
        print(f"  Step{st}  送信{m['sent']:>5}  反応{m['responded']:>3}({m['rate_responded']:>4}%)  "
              f"有料{m['paid']:>2}  │ 累積 反応{cum_r:>3} 有料{cum_p:>2}")

    print("\n文面バリアント別 反応率:")
    for va, m in sorted(result["by_variant"].items(), key=lambda x: -x[1]["rate_responded"]):
        label = {"A": "実績称賛型", "B": "課題提起型", "C": "同業事例型"}.get(va, va)
        print(f"  {va} {label:<6} {m['rate_responded']:>5}%  送信{m['sent']:>4}  有料{m['paid']:>3}")
    print(f"\n→ {OUT}")

if __name__ == "__main__":
    main()
