# 足場AI営業エンジン — Phase 1 プロトタイプ

「全国の足場・塗装・解体会社をデータ化 → AIスコアリング → 最適チャネルで接触」
のパイプライン第1弾。M&A時にはこのマシン自体が資産(#41 建設AI営業)になる。

## 構成
1. ingest.py    — 都道府県別 建設業許可業者名簿Excel → SQLite (とび・土工/塗装/解体を抽出)
                 ※ 2026-08-01 追記: 国交省の企業情報検索システムに一括CSV DLは無いと判明し、
                   都道府県別Excelを読む設計に変更。県ごとの差は parsers/<pref>.py に分離。
2. enrich.py    — Claude API(web検索付き)で各社のHP/求人/レビューを構造化付与
3. scoring.py   — 4軸25点×4=100点でS/A/B/Cランク付け + 推奨チャネル判定
4. out/dashboard.html — 営業司令塔ダッシュボード(単一HTML、そのまま開ける)

## 本番投入手順
1. 都道府県の建設業許可業者名簿Excelを取得（例: 東京都都市整備局が月1回公開） → data/ に置く
2. python3 ingest.py 東京都 data/xxx.xlsx
3. export ANTHROPIC_API_KEY=... && python3 enrich.py --limit 500 (資本金上位から)
4. python3 scoring.py
5. out/scored.json をダッシュボード/CRMに接続

## スコアV1の思想
「AI積算ツール無料オファーに反応する確率」を代理変数で近似。
- 規模適合: 5-50人がスイートスポット
- デジタル度: HP品質 = メール/Web導線の通りやすさ
- 成長シグナル: 求人出稿中 = 人手不足 = 積算が回っていない
- 商流適合: とび・土工主業 × 元請比率
反応実測が貯まったらロジスティック回帰に置換(V2)。

## 次フェーズ(Phase 2)
- enrich_note を使ったAIパーソナライズ文面生成(FAX/DM/メール)
- 送信ログ→反応率をDBに戻すループ = CACの実測開始

---

# Phase 2 — 接触ループ（実装済み）

5. campaign.py — キャンペーン/接触ログ/反応のDB。誰に何を送りどう反応したかを全記録
6. compose.py  — AIパーソナライズ文面生成。A/B/Cは「訴求軸」を変える(トーン違いではない)
                 A:実績称賛型 B:課題提起型 C:同業事例型
7. metrics.py  — ファネル/CAC/LTV・チャネル別・文面別・ランク別の集計
8. console.py  — out/console.html（キャンペーンコンソール）を実データから生成

## 実行順
python3 generate_sample.py            # (本番は ingest.py + enrich.py)
python3 scoring.py
python3 campaign.py init
python3 campaign.py create "2026年9月 S+A 初回接触" --target SA
python3 compose.py --campaign 1       # 本番はAI生成 / --offline でテンプレ
python3 campaign.py simulate 1        # 本番はここが実送信+反応入力に置き換わる
python3 metrics.py
python3 console.py                    # out/console.html を再生成

## 本番化で差し替える3点
1. campaign.py simulate → 実送信API(FAX: メッセージプラス/秒速FAX, メール: SendGrid)
2. 反応の取り込み → LPのフォーム/UTMパラメータからtouches.respondedを更新
3. 有料転換 → AshiBase側の課金イベントをtouches.paidにwebhookで書き戻す

## Phase 3(次)
- 反応実測をロジスティック回帰に学習させスコアV2へ(scoring.pyの重みを置換)
- 勝ち文面の自動増殖(勝ちバリアントからAIが派生案を生成し次ロットに投入)
- フォローアップ自動化(反応なし→14日後に別チャネルで再接触)

---

# Phase 3 — 学習ループ（実装済み）

9.  learn.py    — 実測反応をロジスティック回帰に学習させスコアV2を生成
                  会社属性 + 送信条件(チャネル/文面型)を同時に学習し、
                  「この会社にはどのチャネルで何型を送るべきか」まで出す
10. followup.py — 多段フォローアップ(D+0 / D+14 / D+35)と勝ち文面の自動増殖
                  無反応先のみが次段へ。チャネルと訴求を必ず変える

## 追加の実行順
python3 followup.py --campaign 1 --step 2 --simulate
python3 followup.py --campaign 1 --evolve --offline
python3 followup.py --campaign 1 --step 3 --simulate
python3 metrics.py
python3 learn.py

## 試走結果(デモ)
Step1 1,002送信 → 反応58 / Step2 897 → 25 / Step3 832 → 11
累積 反応94(初回の1.6倍) 有料6社 MRR 83,800円 CAC 13,446円
スコア 上位20%リフト V1 1.44倍 → V2 2.18倍 (AUC 0.589 → 0.670)

## この事業の堀
接触するほどV2が賢くなる。後発が同じ精度に追いつくには同じ量の接触実績が要る。
= リストでもツールでもなく「学習済みの当て勘」が資産になる。

---

# Phase 4 — 受け皿・再投入・売却資料（実装済み）

11. lp.html    — 無料AI積算ツールのLP（受け皿）
                 URLの ?t=touch_id&c=campaign_id&ch=&v= を拾い、
                 POST /api/signup で touches.responded/signed_up に書き戻す設計。
                 これでファネルが閉じる（送信→反応の紐付けが自動化される）
12. dormant.py — 休眠プール(180日)と復活シグナル検出、巡目ごとのオファー切替
                 1巡目:無料ツール → 2巡目:積算代行 → 3巡目:地域事例訪問
13. im.py      — 実測値からM&A用インフォメーションメモランダムを自動生成
                 python3 im.py > out/IM.md

## 現時点の全体像
[許可業者DB] → [AIエンリッチ] → [スコアV1] → [キャンペーン作成]
   → [AI文面生成] → [多段接触 D+0/14/35] → [LP] → [無料登録]
   → [有料転換] → [実測] → [スコアV2学習] ↺ 精度が上がって先頭に戻る
   → 無反応は [休眠プール] → 180日 or 復活シグナルで再投入
   → 全数値は [コンソール] と [IM.md] に自動反映

## 本番移行時に差し替える箇所（それ以外は完成）
- campaign.py simulate / followup.py simulate → 実送信API
- lp.html の POST /api/signup → 実エンドポイント
- SQLite → Postgres、cron で learn.py を週次実行

---

# Phase 5 — システムの堅牢化（実装済み）

これまでは機能の追加。ここは「壊れないこと」の担保。

14. config.py         — 全設定の単一情報源（閾値・単価・法令設定）
15. db.py             — スキーマ一本化 / マイグレーション / **接触ガード**
16. suppress_cli.py   — 配信停止の受付・監査
17. run.py            — 実行順序を強制するオーケストレーター
18. test_pipeline.py  — 33項目の不変条件テスト

## 接触ガード（can_contact）
全ての送信経路が db.can_contact() を通る。除外条件:
  - 配信停止リストに登録済み
  - 重複レコード（代表社へ集約済）
  - 生涯接触上限6回に到達
  - 前回接触から10日未満
campaign.py / followup.py / dormant.py の3経路すべてに組込済み。
※ dormant.py の未適用はテストが検出した。ガードは「作る」ではなく「通す」ことが要件。

## テストが保証していること
名寄せ / スコアの範囲と閾値整合 / ファネルの単調性(反応⊆到達 等) /
シーケンス規則(前段無反応のみ・別チャネル・上限) / 配信停止の遵守 /
コスト単価の一致 / metrics.jsonとDBの一致 / CAC・LTV計算式 / モデル精度の下限

## 使い方
python3 run.py status          # 進捗確認
python3 run.py all --demo      # 全ステップを順に実行
python3 test_pipeline.py       # 数字の健全性を検証
python3 suppress_cli.py check  # 法令遵守の監査

## 現状の全項目パス
成功 33 / 失敗 0

---

# Phase 6 — 本番規模への耐性 + マルチオファー化（実装済み）

19. resilience.py      — レート制御 / 指数バックオフ / チェックポイント / 冪等キー
20. offers.py          — テナントとオファーの分離（マルチプロダクト対応）
21. test_concurrency.py — 並列書き込み・レート制御・再開・二重送信防止のテスト

## 潰した3つの弱点
1. **enrich.py に再開機能がない** → Checkpoint導入。3万社の途中で落ちても続きから走る。
   429/5xxのみ指数バックオフで再試行し、4xx(Fatal)は即諦める。
2. **SQLiteが同時実行に耐えない** → WAL + busy_timeout 30秒 + BEGIN IMMEDIATE。
   8スレッド×30件の並列書き込みで欠落ゼロを確認。
   db.resolve_backend() にPostgres移行時の差分をコメントで閉じ込め済み。
3. **送信APIのレート制御がない** → RateLimiter(トークンバケット)をサービス別に定義。
   Idempotencyで二重送信を構造的に防止（信用を一発で失う事故）。

## オファーとテナントの分離（構造的な追加）
このエンジンは3つの立場で使われるため、オファー固定では成立しない:
  (1) 自社の他事業の契約を取る装置
  (2) 他社に売る商品
  (3) 事業譲渡の対象（買い手が自社商材を流す）

  tenant（誰の営業か） 1─n offer（何を売るか） 1─n campaign（いつ誰に打つか）

オファーが持つもの: 訴求文 / 対象条件 / 価格(→CAC許容上限が自動算出) / NGワード。
NGワード検査は生成後に機械で止める（AIは指示しても稀に踏むため）。
送信者情報はテナント単位で保持（特定電子メール法の表示義務はテナントごとに異なる）。

投入済みオファー例:
  無料AI積算（入口） / 資材管理 14,800円 / 与信スコア 29,800円 / AI入札部 49,800円
  → 価格が上がるほどCAC許容上限が上がる。同じ機構で高単価商材ほど経済性が良くなる。

## モデル昇格ゲート（テストが検出した問題への対応）
学習したモデルが必ずV1より良いとは限らない。反応80件程度では係数が不安定で、
交差検証でV1を下回るケースが実際に発生した。
→ 「学習したから使う」ではなく「V1を上回った時だけ採用」に変更。
   条件: AUC・リフトともにV1超 かつ 反応30件以上。
   満たさない場合は V1 を維持し、model_v2.json には参考値として記録。
   検証: 反応81件→却下(V1維持) / 反応140件→昇格(V2採用) の両方を確認。

## テスト
python3 test_pipeline.py     # 47項目（不変条件・法令遵守・数字の整合）
python3 test_concurrency.py  # 並列・レート・再開・冪等

---

# Phase 7 — 本番接続の準備（実装済み）

22. senders.py    — 送信アダプタ層（FAX/メール/SMS/郵送を共通インターフェース化）
23. api.py        — 反応・課金・配信停止を受けるHTTPサーバ（19項目の自己テスト付き）
24. storage.py    — SQLite/Postgresの方言差分を吸収。DATABASE_URLで切替
25. deploy/       — Dockerfile / docker-compose / crontab
26. HANDOFF.md    — Claude Code向け引き継ぎ仕様

## 送信アダプタ（senders.py）
- send()は例外を投げず必ずSendResultを返す（一括送信が止まらない）
- 恒久エラー(宛先不明)はpermanent=Trueで返し、自動で配信停止に入る
- 送信者情報(特定電子メール法)はアダプタ層で強制付与。文面側の漏れが起きない
- 送信直前にもcan_contact()を再確認（作成後に停止された可能性があるため）
- 宛先検証: メール形式/FAX番号桁数/固定電話へのSMS不達/住所の不完全

## APIサーバ（api.py）
POST /api/signup   LPフォーム → responded=1, signed_up=1
POST /api/activate 積算実行   → activated=1
POST /api/paid     課金webhook（HMAC署名検証必須）→ paid=1, mrr_yen
POST /api/optout   配信停止 → suppression + 未送信分の取消
GET  /t/<touch_id> クリック計測 → LPへリダイレクト
GET  /health       死活監視

アトリビューション3段: touch_id直接 → 直近45日の接触 → メールドメイン照合。
どれも当たらなければオーガニックとして別枠記録。

## テストが検出した実務的な問題（2〜3件目）
「反応が返ってきた会社に、予定済みのフォローが送られ続ける」状態を検出。
→ 反応時に未送信フォローを自動取消（api.py）＋ followup.py 側でも反応済みを除外。
また不変条件そのものの誤りも判明：送信済み履歴は書き換えられないため、
「Step2は前段が無反応のみ」ではなく「反応済みに未送信が残っていない」が正しい条件。

## テスト全体
python3 test_pipeline.py     # 48項目
python3 api.py test          # 19項目
python3 test_concurrency.py  # 並列・レート・再開・冪等
python3 senders.py test      # 宛先検証・送信者表示・二重送信防止
python3 storage.py test      # SQL方言変換

## 本番で残っている作業（HANDOFF.md 参照）
T1 国交省データ取込 / T2 メール送信実装 / T3 LP接続 / T4 課金webhook
T5 FAX送信実装 / T6 Postgres移行 / T7 デプロイ
いずれも「既存の関数1つを実装する」形に落としてあり、設計判断は不要。

## 接触ガードの最終形（全送信経路が通る関門）
| 除外条件 | 理由 |
|---|---|
| 配信停止リスト | 法令・信用 |
| 重複レコード | 同一社への二重送信 |
| **反応済み(商談対象)** | 反応した相手は営業対象でなく商談対象。売り込みを重ねない |
| **既存顧客** | 課金済みに新規売り込みは失礼 |
| 生涯接触6回到達 | しつこさの上限 |
| 前回から10日未満 | 最短間隔 |

太字の2つは、3件目としてテストが検出したもの
（campaign.py が反応済みの会社にも新規キャンペーンを組んでいた）。
意図的な再接触は can_contact(allow_warm=True) で明示的に許可する。
