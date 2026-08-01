"""
enrich.py — AIエンリッチメント層
各社について Web検索→HP読取 をAIに行わせ、営業に効く属性を構造化して付与する。

必要環境変数: ANTHROPIC_API_KEY
コスト実測(2026-08、資本金300万〜1億円のサンプル73社平均。旧見積もりの「1社2〜4円」は
実態と大きく乖離していたため実測値に置き換え): 1社あたり入力6.7万トークン/出力1,400トークン/
web検索4.9回 ≈ $0.20(導入価格)〜$0.27(標準価格) ≈ 30〜40円。都道府県14,688社フルなら
概算$2,900〜4,000(実行時のAPI残高を要確認)。
まずは rank対象のS/A候補(資本金・許可種別で事前絞込)から回すのが定石。

使い方:
  python3 enrich.py --limit 100                    # 未エンリッチの100社を処理(既定は無作為抽出)
  python3 enrich.py --pref 東京都 --sample --limit 500  # 資本金300万〜1億円から無作為抽出
"""
import argparse, json, os, sqlite3, time
from pathlib import Path

import db
import resilience as R

DB = Path(__file__).parent / "out" / "companies.db"
MODEL = "claude-sonnet-5"

# $/1Mトークン。(入力, 出力)。Sonnet 5は2026-08-31まで導入価格が別にある。
PRICING = {
    "claude-sonnet-5":              {"intro": (2.0, 10.0), "standard": (3.0, 15.0)},
    "claude-haiku-4-5-20251001":    {"intro": (1.0, 5.0),  "standard": (1.0, 5.0)},
    "claude-haiku-4-5":             {"intro": (1.0, 5.0),  "standard": (1.0, 5.0)},
}
JPY_PER_USD = 150  # 概算。正確な為替は実行時に確認すること

PROMPT = """あなたは建設業界の営業リサーチャーです。次の会社を調査してください。

会社名: {name}
所在地: {pref}{city}
業種: {trades}

Web検索は2回まで(1回目: 会社名+所在地で公式HP・基本情報を特定。2回目: 求人媒体で
hiring_nowを確認)。3回目以降は使わず、その時点の情報で保守的に推定してください。

Web検索とHPの内容から、以下をJSONのみで返してください(前置き・コードブロック禁止):
{{
  "has_website": 0か1,
  "website_url": "URLまたはnull",
  "website_quality": 0-3の整数,  // 0:なし 1:古い名刺サイト 2:普通 3:採用・実績が充実
  "hiring_now": 0か1,            // Indeed/ハローワーク/求人ボックス等の求人媒体に「現在」掲載中の
                                  // 求人が確認できた場合のみ1。自社HPに採用ページがあるだけ、
                                  // または過去の掲載履歴しか見つからない場合は0。確認できなければ0。
  "hiring_source": "hiring_nowを1と判定した根拠(媒体名とURL等)。0ならnull",
  "est_employees": 従業員数の推定整数,
  "is_target_business": 0か1,    // 足場・とび・塗装・解体の施工を実際に行っている会社なら1。
                                  // 商社・不動産デベロッパー・メーカー・卸売等で、許可は持つが
                                  // 自社では施工しない(グループ会社が施工する等)場合は0。
  "prime_ratio": 0.0-1.0,        // 元請工事の比率推定
  "enrich_note": "営業初回接触に使える具体的な所見を日本語80字以内"
}}
確信が持てない項目は保守的に推定してください。"""

def enrich_one(client, row, model=MODEL):
    """1社分。呼び出し側で retry / rate limit / checkpoint に包まれる。"""
    msg = client.messages.create(
        # web検索を複数回はさむとJSON本体を書く前にmax_tokensを使い切ることがある
        # (実測: 1000だと出力の頭数十字で打ち切られるケースを確認)ため余裕を持たせる
        model=model, max_tokens=2000,
        # max_uses=2: 実測でweb検索平均4.9回/社が入力トークン(平均6.7万)の主因だった
        # ため、プロンプトの検索方針(2回まで)と合わせてハード上限も設ける
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
        messages=[{"role": "user", "content": PROMPT.format(
            name=row["name"], pref=row["pref"] or "", city=row["city"] or "",
            trades=row["trades"])}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        d = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception as e:
        # 検品(enrich_review.py)で原因を追えるよう、生レスポンスの先頭を残す。
        # 文字列に"500"を含めると resilience.is_retryable() の再試行対象ヒント
        # ("500"=サーバエラーの意)に誤って一致し、無駄な再試行(＝無駄なAPI課金)を
        # 生んでしまうため、"raw_snippet"という表記にしている(実際にこの事故で
        # 不要な再試行が発生していたことをログで確認済み)。
        raise ValueError(f"JSON抽出失敗({type(e).__name__}: {e}) raw_snippet={text[:500]!r}") from e
    stu = getattr(msg.usage, "server_tool_use", None)
    d["_usage"] = {
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
        "web_search_requests": (getattr(stu, "web_search_requests", 0) or 0) if stu else 0,
    }
    return d

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--pref", default=None)
    ap.add_argument("--sample", action="store_true",
                     help="資本金300万〜1億円のレンジからランダム抽出する"
                          "(capital DESCだと巨大企業ばかりになるため)")
    ap.add_argument("--prescored-only", action="store_true",
                     help="prescore.pyで選出された会社(prescore_selected=1)のみを対象にする")
    ap.add_argument("--model", default=MODEL,
                     help=f"使用モデル(既定: {MODEL})。例: claude-haiku-4-5-20251001")
    args = ap.parse_args()

    import anthropic  # pip install anthropic （本番のみ必要）
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    con = db.connect()
    db.migrate(con)

    q = "SELECT * FROM companies WHERE has_website IS NULL AND dedup_of IS NULL"
    p = []
    if args.pref:
        q += " AND pref=?"; p.append(args.pref)
    if args.sample:
        q += " AND capital BETWEEN 3000 AND 100000"
    if args.prescored_only:
        q += " AND prescore_selected=1"
    q += " ORDER BY RANDOM() LIMIT ?"; p.append(args.limit)
    rows = {r["id"]: r for r in con.execute(q, p).fetchall()}

    # ── 耐障害化: レート制御 + 指数バックオフ + 中断からの再開 ──
    rl = R.limiter_for("anthropic")
    # モデルを変えると同じ会社でも別実行として扱いたい(比較検証で両方の結果を残すため)
    job_suffix = (':' + args.pref if args.pref else '') + (f':{args.model}' if args.model != MODEL else '')
    ck = R.Checkpoint(con, job=f"enrich{job_suffix}")
    targets = ck.remaining(list(rows.keys()))

    done = failed = 0
    usage_totals = {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0}
    for cid in targets:
        row = rows[cid]
        try:
            rl.acquire()
            d = R.retry(lambda: enrich_one(client, row, model=args.model), job=f"enrich:{row['name']}")
            u = d.pop("_usage", {})
            usage_totals["input_tokens"] += u.get("input_tokens", 0)
            usage_totals["output_tokens"] += u.get("output_tokens", 0)
            usage_totals["web_search_requests"] += u.get("web_search_requests", 0)
            con.execute(
                """UPDATE companies SET has_website=?, website_url=?, website_quality=?,
                   hiring_now=?, hiring_source=?, est_employees=?, is_target_business=?,
                   prime_ratio=?, enrich_note=?, enriched_at=datetime('now')
                   WHERE id=?""",
                (d["has_website"], d.get("website_url"), d["website_quality"],
                 d["hiring_now"], d.get("hiring_source"), d["est_employees"],
                 d["is_target_business"], d["prime_ratio"], d["enrich_note"], cid))
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

    if done:
        avg_in = usage_totals["input_tokens"] / done
        avg_out = usage_totals["output_tokens"] / done
        avg_search = usage_totals["web_search_requests"] / done
        pricing = PRICING.get(args.model, PRICING[MODEL])
        def cost(inp_price, out_price):
            return avg_in / 1e6 * inp_price + avg_out / 1e6 * out_price + avg_search / 1000 * 10.0
        cost_intro = cost(*pricing["intro"])
        cost_std = cost(*pricing["standard"])
        print(f"\n実測コスト(モデル={args.model} / 成功{done}件平均・失敗分は含まず):")
        print(f"  入力 {avg_in:.0f}トークン / 出力 {avg_out:.0f}トークン / web検索 {avg_search:.2f}回")
        print(f"  1社あたり ${cost_intro:.5f}〜${cost_std:.5f}"
              f"(概算{cost_intro*JPY_PER_USD:.1f}〜{cost_std*JPY_PER_USD:.1f}円、"
              f"{JPY_PER_USD}円/$として概算。正確な為替は別途確認)")
        print(f"  対象母数14,688社なら ${cost_intro*14688:,.0f}〜${cost_std*14688:,.0f}"
              f" / 事前絞込後3,000社なら ${cost_intro*3000:,.0f}〜${cost_std*3000:,.0f}")

if __name__ == "__main__":
    main()
