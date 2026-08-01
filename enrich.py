"""
enrich.py — AIエンリッチメント層
各社について Web検索→HP読取 をAIに行わせ、営業に効く属性を構造化して付与する。

必要環境変数: ANTHROPIC_API_KEY
コスト目安: web検索込みで1社あたり2〜4円。3万社フルで約10万円前後。
まずは rank対象のS/A候補(資本金・許可種別で事前絞込)から回すのが定石。

使い方:
  python3 enrich.py --limit 100          # 未エンリッチの100社を処理
  python3 enrich.py --pref 東京都 --limit 500
"""
import argparse, json, os, sqlite3, time
from pathlib import Path

import db
import resilience as R

DB = Path(__file__).parent / "out" / "companies.db"
MODEL = "claude-sonnet-5"

PROMPT = """あなたは建設業界の営業リサーチャーです。次の会社を調査してください。

会社名: {name}
所在地: {pref}{city}
業種: {trades}

Web検索とHPの内容から、以下をJSONのみで返してください(前置き・コードブロック禁止):
{{
  "has_website": 0か1,
  "website_url": "URLまたはnull",
  "website_quality": 0-3の整数,  // 0:なし 1:古い名刺サイト 2:普通 3:採用・実績が充実
  "hiring_now": 0か1,            // 求人媒体・自社採用ページに現在出稿しているか
  "est_employees": 従業員数の推定整数,
  "google_reviews": Googleマップのレビュー件数(不明なら0),
  "prime_ratio": 0.0-1.0,        // 元請工事の比率推定
  "enrich_note": "営業初回接触に使える具体的な所見を日本語80字以内"
}}
確信が持てない項目は保守的に推定してください。"""

def enrich_one(client, row):
    """1社分。呼び出し側で retry / rate limit / checkpoint に包まれる。"""
    msg = client.messages.create(
        model=MODEL, max_tokens=1000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": PROMPT.format(
            name=row["name"], pref=row["pref"] or "", city=row["city"] or "",
            trades=row["trades"])}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text[text.index("{"): text.rindex("}") + 1])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--pref", default=None)
    args = ap.parse_args()

    import anthropic  # pip install anthropic （本番のみ必要）
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    con = db.connect()
    db.migrate(con)

    q = "SELECT * FROM companies WHERE has_website IS NULL AND dedup_of IS NULL"
    p = []
    if args.pref:
        q += " AND pref=?"; p.append(args.pref)
    q += " ORDER BY capital DESC LIMIT ?"; p.append(args.limit)
    rows = {r["id"]: r for r in con.execute(q, p).fetchall()}

    # ── 耐障害化: レート制御 + 指数バックオフ + 中断からの再開 ──
    rl = R.limiter_for("anthropic")
    ck = R.Checkpoint(con, job=f"enrich{':' + args.pref if args.pref else ''}")
    targets = ck.remaining(list(rows.keys()))

    done = failed = 0
    for cid in targets:
        row = rows[cid]
        try:
            rl.acquire()
            d = R.retry(lambda: enrich_one(client, row), job=f"enrich:{row['name']}")
            con.execute(
                """UPDATE companies SET has_website=?, website_url=?, website_quality=?,
                   hiring_now=?, est_employees=?, google_reviews=?, prime_ratio=?,
                   enrich_note=?, enriched_at=datetime('now')
                   WHERE id=?""",
                (d["has_website"], d.get("website_url"), d["website_quality"],
                 d["hiring_now"], d["est_employees"], d["google_reviews"],
                 d["prime_ratio"], d["enrich_note"], cid))
            con.commit()
            ck.done(cid)
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(targets)} 完了")
        except Exception as e:
            ck.failed(cid, e)
            failed += 1
            print(f"  ✗ {row['name']}: {str(e)[:100]}")

    print(f"\nエンリッチメント完了: 成功 {done} / 失敗 {failed}")
    if failed:
        print(f"  失敗分は同じコマンドを再実行すれば続きから走ります "
              f"(処理済み {done}件はスキップされます)")

if __name__ == "__main__":
    main()
