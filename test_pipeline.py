"""
test_pipeline.py — パイプラインの不変条件テスト
このシステムは「数字そのもの」が商品なので、数字が壊れていないことを機械的に保証する。
静かに間違った数字を出すのが最悪の失敗であり、それはIMに載ってデューデリで露見する。

  python3 test_pipeline.py
"""
import json
import sys

import config as C
import db
from db import normalize_name

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def main():
    con = db.connect()
    db.migrate(con)
    q = lambda s, *p: con.execute(s, p).fetchone()[0]

    print("\n── 名寄せ ──")
    check("株式会社/有限会社の表記ゆれを吸収",
          normalize_name("株式会社丸友組") == normalize_name("丸友組") == normalize_name("(株)丸友組"))
    check("全角英数を半角化", normalize_name("ＡＢＣ工業") == normalize_name("ABC工業"))
    check("空白を無視", normalize_name("丸友 組") == normalize_name("丸友組"))

    print("\n── スコアリング ──")
    check("全社にランクが付与されている",
          q("SELECT COUNT(*) FROM companies WHERE rank IS NULL") == 0,
          f"{q('SELECT COUNT(*) FROM companies WHERE rank IS NULL')}社が未採点")
    check("スコアが0-100の範囲内",
          q("SELECT COUNT(*) FROM companies WHERE score < 0 OR score > 100") == 0)
    check("ランクとスコアの対応が閾値と一致",
          q(f"SELECT COUNT(*) FROM companies WHERE rank='S' AND score < {C.SCORE_THRESHOLDS['S']}") == 0
          and q(f"SELECT COUNT(*) FROM companies WHERE rank='C' AND score >= {C.SCORE_THRESHOLDS['B']}") == 0)
    n_s_rank = q("SELECT COUNT(*) FROM companies WHERE rank='S'")
    check("Sランクが全体の20%以下（絞り込みとして機能している）",
          n_s_rank <= q("SELECT COUNT(*) FROM companies") * 0.2,
          f"S {n_s_rank}社")

    print("\n── ファネルの整合性 ──")
    check("到達 ≤ 送信", q("SELECT COUNT(*) FROM touches WHERE delivered > 1") == 0)
    check("反応した接触は必ず到達している",
          q("SELECT COUNT(*) FROM touches WHERE responded=1 AND delivered=0") == 0)
    check("登録した接触は必ず反応している",
          q("SELECT COUNT(*) FROM touches WHERE signed_up=1 AND responded=0") == 0)
    check("活性化した接触は必ず登録している",
          q("SELECT COUNT(*) FROM touches WHERE activated=1 AND signed_up=0") == 0)
    check("有料転換は必ず活性化している",
          q("SELECT COUNT(*) FROM touches WHERE paid=1 AND activated=0") == 0)
    check("MRRが立つのは有料転換のみ",
          q("SELECT COUNT(*) FROM touches WHERE mrr_yen > 0 AND paid=0") == 0)
    check("未送信の接触に反応が記録されていない",
          q("SELECT COUNT(*) FROM touches WHERE sent_at IS NULL AND responded=1") == 0)

    print("\n── シーケンス ──")
    # 送信済みの履歴は書き換えられない（後から反応が返ることがある）ので、
    # 検証すべきは「反応済みの会社に未送信のフォローが残っていないこと」。
    check("反応済みの会社に未送信のフォローが残っていない（追いかけを止めている）",
          q("""SELECT COUNT(*) FROM touches t WHERE t.sent_at IS NULL AND EXISTS(
               SELECT 1 FROM touches x WHERE x.company_id=t.company_id AND x.responded=1)""") == 0)
    check("Step2の生成時点で前段に反応がなかった（送信済み分のみ検証）",
          q("""SELECT COUNT(*) FROM touches t2 WHERE t2.step=2 AND t2.sent_at IS NOT NULL
               AND EXISTS(SELECT 1 FROM touches t1 WHERE t1.company_id=t2.company_id
               AND t1.campaign_id=t2.campaign_id AND t1.step=1 AND t1.responded=1
               AND t1.responded_at IS NOT NULL AND t1.responded_at < t2.sent_at)""") == 0)
    check(f"1社あたりの接触が上限{C.MAX_LIFETIME_TOUCHES}回以内",
          q(f"""SELECT COUNT(*) FROM (SELECT company_id FROM touches WHERE sent_at IS NOT NULL
               GROUP BY company_id HAVING COUNT(*) > {C.MAX_LIFETIME_TOUCHES})""") == 0)
    check("同一キャンペーン・同一社・同一ステップの重複がない",
          q("""SELECT COUNT(*) FROM (SELECT campaign_id, company_id, step FROM touches
               GROUP BY 1,2,3 HAVING COUNT(*) > 1)""") == 0)
    check("Step2はStep1と別チャネル",
          q("""SELECT COUNT(*) FROM touches t1 JOIN touches t2
               ON t1.company_id=t2.company_id AND t1.campaign_id=t2.campaign_id
               WHERE t1.step=1 AND t2.step=2 AND t1.channel=t2.channel""") == 0)

    print("\n── コンプライアンス ──")
    check("配信停止後に送信された記録がない",
          q("""SELECT COUNT(*) FROM suppression s JOIN touches t ON t.company_id=s.company_id
               WHERE t.sent_at IS NOT NULL AND t.sent_at > s.created_at""") == 0)
    check("配信停止対象に未送信の予定が残っていない",
          q("""SELECT COUNT(*) FROM suppression s JOIN touches t ON t.company_id=s.company_id
               WHERE t.sent_at IS NULL""") == 0)
    check("重複統合された会社に接触していない",
          q("""SELECT COUNT(*) FROM companies c JOIN touches t ON t.company_id=c.id
               WHERE c.dedup_of IS NOT NULL AND t.sent_at IS NOT NULL""") == 0)
    check("送信者情報が設定されている", bool(C.SENDER_INFO.get("email") and C.SENDER_INFO.get("optout_url")))

    print("\n── コスト計算 ──")
    check("全接触に単価が設定されている",
          q("SELECT COUNT(*) FROM touches WHERE unit_cost_yen IS NULL OR unit_cost_yen < 0") == 0)
    check("単価がconfigの定義と一致",
          all(q("SELECT COUNT(*) FROM touches WHERE channel=? AND unit_cost_yen<>?", ch, cost) == 0
              for ch, cost in C.UNIT_COST_YEN.items()))

    print("\n── 耐障害性 ──")
    check("WALモードが有効（読み書きが互いをブロックしない）",
          con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
    check("busy_timeoutが設定されている",
          con.execute("PRAGMA busy_timeout").fetchone()[0] >= 10000)
    check("チェックポイント表が存在（中断から再開できる）",
          q("SELECT COUNT(*) FROM sqlite_master WHERE name='checkpoints'") == 1)
    check("冪等キー表が存在（二重送信を防げる）",
          q("SELECT COUNT(*) FROM sqlite_master WHERE name='idempotency'") == 1)
    import resilience as R
    check("レートリミッターがサービス別に定義されている",
          all(k in R.LIMITS for k in ["anthropic", "sendgrid", "fax_api", "sms"]))
    def _http_err(status):
        e = RuntimeError(f"http {status}")
        e.status_code = status
        return e
    check("再試行すべきエラーと諦めるべきエラーを区別できる(ステータスコード/型で判定)",
          R.is_retryable(_http_err(429)) and not R.is_retryable(_http_err(400))
          and not R.is_retryable(R.Fatal("400"))
          # メッセージの文字列一致には頼らない(誤判定の実例があったため廃止した設計)
          and not R.is_retryable(RuntimeError("500文字まで表示")))

    print("\n── オファー / テナント ──")
    import offers as OF
    check("テナントが定義されている", q("SELECT COUNT(*) FROM tenants") > 0)
    check("オファーが複数定義されている（単一商材に固定されていない）",
          q("SELECT COUNT(*) FROM offers") >= 2,
          f"{q('SELECT COUNT(*) FROM offers')}件")
    check("全オファーが送信者情報を持つテナントに紐づく",
          q("""SELECT COUNT(*) FROM offers o LEFT JOIN tenants t ON t.id=o.tenant_id
               WHERE t.id IS NULL OR t.sender_email IS NULL OR t.optout_url IS NULL""") == 0)
    check("全オファーに対象条件が設定されている",
          q("SELECT COUNT(*) FROM offers WHERE target_rule IS NULL OR target_rule=''") == 0)
    ok_ng, hits = OF.check_ng(con, 3, "この会社は倒産する可能性があります")
    check("NGワード検査が禁止表現を検出する", (not ok_ng) and "倒産する" in hits)
    ok_ng2, _ = OF.check_ng(con, 3, "取引先の支払い実績を確認できます")
    check("NGワード検査が正常な文面を通す", ok_ng2)
    check("有料オファーはCAC許容上限が算出可能（価格が入っている）",
          q("SELECT COUNT(*) FROM offers WHERE price_yen IS NULL") == 0)

    mp = C.OUT_DIR / "metrics.json"
    if mp.exists():
        print("\n── メトリクスの再計算検証 ──")
        m = json.loads(mp.read_text())["overall"]
        check("送信数がDBと一致", m["sent"] == q("SELECT COUNT(*) FROM touches"),
              f"metrics {m['sent']} vs db {q('SELECT COUNT(*) FROM touches')}")
        check("有料転換数がDBと一致", m["paid"] == q("SELECT COALESCE(SUM(paid),0) FROM touches"))
        check("MRRがDBと一致", m["mrr_yen"] == q("SELECT COALESCE(SUM(mrr_yen),0) FROM touches"))
        if m["paid"]:
            check("CAC = 実費 ÷ 有料転換数", abs(m["cac_yen"] - m["cost_yen"] / m["paid"]) < 1)
            check(f"LTV/CAC が{C.LTV_MONTHS}ヶ月前提で整合",
                  abs(m["ltv_cac"] - (m["mrr_yen"] * C.LTV_MONTHS) / m["cost_yen"]) < 0.1)

    mv = C.OUT_DIR / "model_v2.json"
    if mv.exists():
        print("\n── 学習モデル ──")
        mo = json.loads(mv.read_text())
        check("V2のAUCが0.5を上回る（ランダムより良い）", mo["auc_v2"] > 0.5,
              f"AUC {mo['auc_v2']}")
        check("採用中のモデルがV1以上の性能（劣化したモデルを使っていない）",
              mo.get("promoted", False) or mo.get("active_model") == "v1",
              f"V1 {mo['lift_v1']} → V2 {mo['lift_v2']} / 採用 {mo.get('active_model')}")
        check("V2を採用した場合はV1を上回っている",
              (not mo.get("promoted")) or mo["lift_v2"] >= mo["lift_v1"])
        check("学習サンプルが100件以上", mo["n_samples"] >= 100)
        check("反応サンプルが20件以上（係数の安定性）", mo["n_positive"] >= 20,
              f"反応 {mo['n_positive']}件")

    print(f"\n{'='*50}\n  成功 {len(PASS)} / 失敗 {len(FAIL)}")
    if FAIL:
        print("\n  失敗した項目:")
        for n, d in FAIL:
            print(f"    ✗ {n}" + (f"  — {d}" if d else ""))
        sys.exit(1)
    print("  全項目パス。数字は信頼できる状態です。")


if __name__ == "__main__":
    main()
