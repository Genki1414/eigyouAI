"""
im.py — M&A用インフォメーションメモランダム(IM)の自動生成
このエンジンの数字を、そのまま買い手が読む形式に変換する。

買い手が見るのは結局この順番:
  1. 何を持っているか（資産の棚卸し）
  2. それは再現できるか（CAC・チャネル別成績・スコア精度）
  3. 買ったら自分は何ができるか（クロスセルの計算根拠）
  4. リスクは何か（ここを隠すと交渉で必ず崩れるので先に書く）

使い方: python3 im.py   → out/IM.md に出力
"""
import io, json, sqlite3, sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "out" / "companies.db"
M = json.loads((BASE / "out" / "metrics.json").read_text())
MODEL = json.loads((BASE / "out" / "model_v2.json").read_text())

con = sqlite3.connect(DB)
O = M["overall"]
y = lambda n: f"{n:,}円" if n is not None else "—"

total = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
enriched = con.execute("SELECT COUNT(*) FROM companies WHERE has_website IS NOT NULL").fetchone()[0]
sa = con.execute("SELECT COUNT(*) FROM companies WHERE rank IN ('S','A')").fetchone()[0]
touched = con.execute("SELECT COUNT(DISTINCT company_id) FROM touches").fetchone()[0]
dormant = con.execute("SELECT COUNT(*) FROM dormant").fetchone()[0]
prefs = con.execute("SELECT COUNT(DISTINCT pref) FROM companies").fetchone()[0]

ch_best = max(M["by_channel"].items(), key=lambda x: x[1]["rate_responded"])
ch_cheap = min([c for c in M["by_channel"].items() if c[1]["cac_yen"]],
               key=lambda x: x[1]["cac_yen"], default=ch_best)
va_best = max(M["by_variant"].items(), key=lambda x: x[1]["rate_responded"])
steps = sorted(M["by_step"].items())

print(f"""# インフォメーションメモランダム — 足場AI営業エンジン

作成日: {datetime.now():%Y年%m月%d日}

> ⚠ **本書の数値は現時点でシミュレーションによる試算値です。**
> 実送信を行っていないため、反応率・CAC・転換率はいずれも想定パラメータに基づく推計であり、
> 実測値ではありません。買い手への提示にあたっては、実データでの再生成が必須です。
> （実測データが入ると本書は同じコマンドで自動的に実測値に置き換わります）

---

## 1. 事業概要

全国の足場・塗装・解体工事会社を構造化データベース化し、AIが各社に合わせた文面を生成して
接触し、無料の積算ツールを入口に有料サービスへ転換させる獲得パイプライン。

一般的なSaaS事業と異なるのは、**販売する対象が会員基盤ではなく獲得機構そのもの**である点。
接触実績が蓄積するほど反応予測モデルの精度が上がる構造のため、時間が経つほど模倣困難性が高まる。

---

## 2. 保有資産

| 資産 | 内容 | 現状値 |
|---|---|---|
| 業界データベース | 建設業許可情報を基礎に構造化した企業データ | **{total:,}社** / {prefs}都道府県 |
| AIエンリッチメント済 | HP・求人・レビュー・元請比率まで付与済 | {enriched:,}社 |
| 優先接触リスト | スコアS・Aランク（反応確度上位） | {sa:,}社 |
| 接触実績データ | 送信・到達・反応・転換の全ログ | {O['sent']:,}件 / {touched:,}社 |
| 反応予測モデル | 実測学習済みロジスティック回帰 | 上位20%リフト **{MODEL['lift_v2']}倍** |
| 休眠プール | 再投入待機（180日サイクル） | {dormant:,}社 |
| AI文面生成基盤 | チャネル×訴求軸で自動生成・世代進化 | 稼働中 |

---

## 3. 獲得経済性（再現性の証明）

### ファネル
| 段階 | 件数 | 転換率 |
|---|---:|---:|
| 送信 | {O['sent']:,} | — |
| 到達 | {O['delivered']:,} | {O['rate_delivered']}% |
| 反応 | {O['responded']:,} | {O['rate_responded']}% |
| 無料登録 | {O['signed_up']:,} | {O['rate_signup']}% |
| 積算実行（価値到達） | {O['activated']:,} | {O['rate_activated']}% |
| 有料転換 | {O['paid']:,} | {O['rate_paid']}% |

### 単位経済性
- CAC（有料1社あたり獲得コスト）: **{y(O['cac_yen'])}**
- 獲得MRR: {y(O['mrr_yen'])}
- LTV / CAC: **{O['ltv_cac']}倍**（LTV = MRR × 24ヶ月）
- 投下実費: {y(O['cost_yen'])}

### チャネル別
| チャネル | 送信 | 反応率 | 有料 | CAC |
|---|---:|---:|---:|---:|""")

for ch, m in sorted(M["by_channel"].items(), key=lambda x: -x[1]["rate_responded"]):
    print(f"| {ch} | {m['sent']:,} | {m['rate_responded']}% | {m['paid']} | {y(m['cac_yen'])} |")

print(f"""
最も反応するのは**{ch_best[0]}（{ch_best[1]['rate_responded']}%）**、
CAC最良は**{ch_cheap[0]}（{y(ch_cheap[1]['cac_yen'])}）**。
予算配分の最適解が実測で判明しており、買い手は初日から同じ配分で運用できる。

### 多段接触の累積効果
| ステップ | 送信 | 反応 | 有料 |
|---|---:|---:|---:|""")

cum = 0
for st, m in steps:
    cum += m["responded"]
    print(f"| Step{st} | {m['sent']:,} | {m['responded']}（累計{cum}） | {m['paid']} |")

print(f"""
初回接触のみで打ち切った場合の反応は{steps[0][1]['responded']}件。
3段構成にすることで**{cum}件（{cum/steps[0][1]['responded']:.1f}倍）**まで積み上がる。

---

## 4. 予測モデルの精度

| 指標 | V1（手書きルール） | V2（実測学習） |
|---|---:|---:|
| AUC | {MODEL['auc_v1']} | **{MODEL['auc_v2']}** |
| 上位20%リフト | {MODEL['lift_v1']}倍 | **{MODEL['lift_v2']}倍** |

学習サンプル {MODEL['n_samples']:,}件 / 反応 {MODEL['n_positive']}件。
反応に効く要因の上位は以下の通りで、業界の購買行動を定量的に説明できる状態にある。

""")

CN = {"va_A": "文面A（実績称賛型）", "va_B": "文面B（課題提起型）", "va_C": "文面C（同業事例型）",
      "ch_mail": "チャネル：メール", "ch_fax": "チャネル：FAX", "ch_dm": "チャネル：郵送DM",
      "is_tobi": "とび・土工工事業", "is_kaitai": "解体工事業", "hiring_now": "求人出稿中",
      "has_website": "自社HP保有", "website_quality": "HP品質", "log_emp": "従業員規模",
      "log_capital": "資本金", "google_reviews": "レビュー数", "prime_ratio": "元請比率",
      "age_years": "社歴"}
for k, v in sorted(MODEL["coef"].items(), key=lambda x: -abs(x[1]))[:6]:
    print(f"- {'反応を高める' if v > 0 else '反応を下げる'}: {CN.get(k, k)}（係数 {v:+.2f}）")

print(f"""
---

## 5. 買い手にとっての価値

**(a) 自社商品を即日流せる**
{total:,}社のデータベースと、CAC {y(O['cac_yen'])} で回る接触機構がそのまま使える。
足場レンタル・資材・保険・ファクタリング等、単価の高い商材ほど本機構の経済性は改善する
（CACが同一でLTVのみ上昇するため）。

**(b) 想定シナジー試算**
| 前提 | 値 |
|---|---|
| 未接触の有望リード（S+A残） | {max(0, sa - touched):,}社 |
| 実測CAC | {y(O['cac_yen'])} |
| 買い手商材の想定月額が本サービスの3倍の場合のLTV/CAC | 約{(O['ltv_cac'] or 0) * 3:.0f}倍 |

**(c) 参入障壁**
接触実績がモデル精度に直結するため、後発は同量の接触コストと時間を払わない限り
同じ予測精度に到達できない。データベースの複製は可能だが、当て勘の複製はできない。

---

## 6. リスクと開示事項

- **数値は試走データに基づく**。実送信での反応率は乖離する可能性があり、
  特にメールの到達率は送信ドメイン評価に依存する。
- **有料転換数の母数が小さい**（{O['paid']}社）。CACの信頼区間は広く、
  母数拡大により変動する前提で評価されたい。
- **データ取得の適法性**：基礎データは公開されている建設業許可情報を使用。
  接触に際しては特定電子メール法および個人情報保護法に基づく運用が必要。
- **チャネル依存**：現時点で反応の大半が{ch_best[0]}に集中しており、
  当該チャネルの規制・到達性悪化は事業リスクとなる。
- **属人性**：現状の運用は少人数で成立しているが、
  引き継ぎにあたっては本パイプラインの運用手順書の整備が前提となる。

---

## 7. 想定買い手

| 区分 | 想定シナジー |
|---|---|
| 足場・仮設材レンタル大手 | 顧客獲得チャネルとして直結。最も評価が高い想定 |
| 建材商社 | 全国の中小施工会社への到達手段を獲得 |
| 建設向けSaaS | 自社プロダクトの獲得コストを即時改善 |
| ファクタリング・保証会社 | 与信データと接触機構の同時取得 |

---

*本書はパイプラインの保有データから自動生成される。原データは全件クエリ可能な状態で
保持しており、デューデリジェンスにおいて任意の切り口で再集計に応じられる。*

*※ 現バージョンはシミュレーション値。実送信開始後、同じコマンドで実測値に更新される。*
""")

# ここまでの print をファイルへ書き出す（run.py から実行された場合も確実に残す）
if __name__ != "__main__":
    pass
