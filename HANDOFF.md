# Claude Code 引き継ぎ仕様書 — 足場AI営業エンジン

**⚠ 先に INDEX.md の「最重要の注意」を読むこと（数値はシミュレーション値です）**

**この文書の目的**: 本番投入で残っている作業を、設計判断なしで実行できる形にする。
既存の設計を変更しないこと。テスト（`test_pipeline.py` 48項目 / `api.py test` 27項目 /
`test_concurrency.py` / `senders.py test` / `storage.py test`）が全て通る状態を維持すること。

---

## 0. 最初に実行して現状を確認する

```bash
pip install -r requirements.txt
python3 run.py all --demo      # デモデータで全工程が通ることを確認
python3 test_pipeline.py       # 48項目
python3 api.py test            # 27項目
python3 test_concurrency.py
python3 senders.py test
python3 storage.py test
```

全て通らない状態で先に進まないこと。通らない場合は原因を報告し、勝手に設計を変えない。

---

## 1. アーキテクチャ（変更しないこと）

```
[都道府県別許可業者名簿Excel] → parsers/<pref>.py → ingest.py → [companies]
                              ↓ enrich.py（AI: HP/求人/レビュー）
                              ↓ scoring.py（V1: 4軸100点）
[offers/tenants] → campaign.py（接触ガード） → [touches]
                              ↓ compose.py（AI文面 / NGワード検査）
                              ↓ senders.py（FAX/メール/SMS/郵送）
                              ↓
[LP] → api.py（signup/activate/paid/optout） → [touches更新]
                              ↓ metrics.py → learn.py（V2昇格ゲート）
                              ↓ followup.py（D+14/D+35）→ dormant.py（180日）
                              ↓ im.py → IM.md
```

**絶対に守る不変条件**
- 全ての送信は `db.can_contact()` を通る。バイパスする経路を作らない
- 配信停止に入った会社へは二度と送らない
- 同じ `idem_key` で二度送らない
- モデルは `learn.py` の昇格ゲートを通った時だけ採用する
- `INSERT OR REPLACE` を使わない（Postgres非対応。`ON CONFLICT ... DO UPDATE` を使う）

---

## 2. 実装するタスク（優先順）

### T1. 建設業許可業者名簿の取込【半日】※2026-08-01 設計変更
- ~~取得元: https://etsuran2.mlit.go.jp/TAKKEN/~~ → **このシステムに一括CSVダウンロードは無い。**
  実データは都道府県ごとに公開されている名簿Excel（例: 東京都は都市整備局が
  建設業情報管理センター登録情報から月1回公開）を使う。
- 対象業種: とび・土工工事業 / 塗装工事業 / 解体工事業
- 大臣許可業者（本店・支店が複数都道府県）は当面スコープ外。知事許可が9割以上のため
- 設計: `ingest.py` は都道府県別Excelを読むオーケストレータ。県ごとのヘッダ位置・
  業種表記（コード/業種名/1・2フラグの横持ち）の差は `parsers/<pref>.py` に分離し、
  業種の表現ゆれの変換表・和暦日付や金額の正規化は `parsers/common.py` に共通化した。
  **companiesテーブルのスキーマは変更していない**
- 現状: `parsers/tokyo.py` で東京都のみ実装済み。合成Excel（縦持ち/横持ち両形式、
  和暦・カンマ区切り金額・大臣許可混在）で ingest→dedup の通しを確認済みだが、
  **実ファイルは未検証**（このネットワーク環境からは対象サイトに到達できず、
  実データでのヘッダ確認ができていない）。ヘッダは固定位置ではなく候補語マッチで
  検出する作りなので、実ファイルを初めて通す際は「対象業種が1件も取れない」警告と
  ログの `n_in/n_target` 件数を必ず確認すること。ヘッダが想定と違えば
  `parsers/tokyo.py` の `_HEADER_CANDIDATES` に実際の表記を追加すればよい
- 東京都で通ってから他県を追加する。追加時は `parsers/<pref>.py` に
  `parse(path) -> Iterator[dict]` を実装し、`parsers/__init__.py` の `REGISTRY` に登録するだけ
- 使い方: `python3 ingest.py 東京都 data/tokyo_kensetsu_meibo.xlsx`
- 投入後に必ず `python3 run.py step dedup` を実行（名寄せ）
- 検証: `python3 test_pipeline.py` が通ること（新規投入した会社はscoring未実施のため
  rank NULLになる。demoデータと混在させたまま検証しないこと。クリーンな状態で
  ingest→dedup→scoringの順に通してから検証する）

### T2. メール送信の実装【半日】
- `senders.py` の `MailSender._deliver()` のみを実装する
- SendGrid想定。他社でも良いが `SendResult` の形は変えない
- **恒久エラー（無効アドレス・ブロック）は `permanent=True` で返す**
  → 呼び出し側が自動で配信停止に入れる
- 401/403は `R.Fatal` を投げる（再試行しても無駄なため）
- 検証: `dry_run=False` で自分宛に1通送り、`touches.sent_at` が入ること

### T3. LPの接続【2時間】
- `lp.html` を公開し、`POST /api/signup` を実エンドポイントに向ける
- 送信URLに必ず `?t=<touch_id>&c=<campaign_id>` を付ける
  （これが無いとアトリビューションが取れず、学習データにならない）
- メール本文のリンクは `https://<host>/t/<touch_id>` を使う（クリック計測とリダイレクトを兼ねる）

### T4. 課金webhook【2時間】
- 課金システムから `POST /api/paid` を叩く
- ヘッダ `X-Signature` に `hmac_sha256(WEBHOOK_SECRET, body)` を入れる
  （生成関数は `api.sign()` にある）
- `event_id` を必ず含める（二重計上防止のキーになる）

### T5. FAX送信の実装【半日】
- `senders.py` の `FaxSender._deliver()` のみを実装
- 事業者: 秒速FAX / メッセージプラス等
- **送信は平日9-18時に限定する**（`deploy/crontab` で制御済み。深夜FAXは苦情に直結）

### T6. Postgres移行【半日 / 数万社を超えてから】
- `DATABASE_URL` を設定するだけで `storage.py` が切り替える
- `psycopg` をインストール
- DDL生成: `DATABASE_URL=... python3 storage.py ddl`
- 方言変換は `storage.to_pg_sql()` が吸収する。新しいSQLを書く場合は
  `python3 storage.py test` で変換されることを確認する

### T7. デプロイ【2時間】
```bash
cp .env.example .env      # 全項目を埋める。SENDER_ADDRESSは省略不可（法令）
openssl rand -hex 32      # → WEBHOOK_SECRET
docker compose -f deploy/docker-compose.yml up -d --build
curl http://127.0.0.1:8787/health
```
- APIの前段にTLS終端（nginx / Cloudflare）を置く。`api.py` は127.0.0.1のみ待受
- cronは `deploy/crontab` をそのまま使う

### T8. Stock Factory連携【完了・2026-08-01】
`stockfactory-office`（`src/execution/adapters/sales-engine.ts`）から叩けるよう、
`api.py` に運用API 3本を追加済み。新規テーブル・スキーマ変更なし。

- `GET /api/ops/status` — `run.status_dict()`。企業数・採点済み数・ランク分布・
  キャンペーン数・各パイプラインステップの完了状況
- `GET /api/ops/metrics` — `metrics.compute()`（CLIの`metrics.py`と同じ集計ロジックを
  関数として切り出して共有）
- `POST /api/ops/run-step` — `run.run_op(con, step, campaign_id, dry_run)`。
  body: `{"step": "score"|"compose"|"dedup"|"learn"|"send"|"followup", "campaignId", "dryRun"}`
  - `send`/`followup` は必ず `senders.send_campaign()` 経由（＝`db.can_contact()` を
    必ず通る）。この経路が「接触ガードのバイパス」（3節参照）にならないことを
    `api.py test` に専用のテストとして追加してある
  - `send`/`followup` の実送信は `senders.py` の `_deliver()` が未実装（T2/T5未着手）の
    チャネルでは `NotImplementedError` になる。T2/T5を実装すればそのまま実送信に切り替わる
- 認証: 3本共通で `Authorization: Bearer <SALES_ENGINE_API_KEY>`。未設定時は常に401
  （`WEBHOOK_SECRET`と違い開発用デフォルト値は持たせていない。実送信まで叩ける
  強い権限のため）
- `.env` に `SALES_ENGINE_API_KEY` を生成して設定するだけで社長側のRuntimeと繋がる

### T9. mikomeruデータ統合【完了・2026-08-04】
社長が別サービス(mikomeru、業種横断の企業ディレクトリ)から取得したCSVを
`companies` テーブルへ統合。狙いはAI検索なしで`has_website`/連絡先を確定させ、
`enrich.py`のコストを下げること。

- 取込元: mikomeru保存済みリスト「東京建設業」7,708件(CSVはブラウザコンソールで
  ページネーションを巡回して取得。ログイン情報は本セッションのチャットにのみ存在し
  リポジトリには一切含めていない。パスワードは使い終わったらローテーション推奨と
  社長に伝達済み)
- 実行: `python3 ingest_mikomeru.py <CSVパス>`
- 名寄せ: `db.normalize_name()`(pref単位)で既存レコードと照合。
  一致した2,239社は**新規行を作らず**既存レコードに`website_url`/`contact_url`/
  `has_contact_form`/`corporate_no`を書き足すのみ（既存の空欄だけ埋める。
  AIエンリッチ済みの値は上書きしない）。不一致の5,469社は新規追加
  (`data_source='mikomeru'`、業種は問わず全件追加する方針で社長合意済み)
- 新規列: `contact_url`(問い合わせフォームURL) / `has_contact_form` / `corporate_no`
  (法人番号13桁) / `data_source`(NULL=国交省名簿 / `'mikomeru'`=mikomeru由来の新規行)
- **`db.normalize_name()`のバグを本作業中に発見・修正**: `_STRIP`が半角`(株)`のみ対応で
  全角`（株）`を除外できていなかった(実データは全角カッコ)。`dedup()`/このスクリプトの
  両方にあった「name_normはNULLの行だけ埋める」というキャッシュ設計も、関数修正が
  既存行に反映されない同型の事故を起こしたため「毎回フル再計算」に変更した。
  この修正で新たに358件の未検出重複(同一社が知事許可の別表記で2レコードに
  分かれていたもの)が見つかり`dedup_of`で統合済み。データ破損はなし
  (`test_pipeline.py`/`test_concurrency.py`で確認済み)
- 業種スコープ: mikomeruは`とび・土工/塗装/解体`に絞られていない一般的な建設業
  ディレクトリ。新規追加5,469社のうち上記3業種に該当するのは101社のみで、
  残りは対象業種外（電気設備工事・住宅リフォーム等）。`scoring.py`の商流適合軸で
  自然に評価が下がる設計のため除外はしていない

**第2弾(同日): 全国版の取込**
mikomeruの「リスト取得」機能で業種(とび・土工工事/解体工事/リフォーム/
住宅リフォーム・改修工事 ※「塗装」という単体カテゴリはmikomeru側に存在せず、
一番近い「リフォーム」系2カテゴリで代替)×全47都道府県を条件検索し、19,970件を
同じ手順で取込(リストID 1997)。「リフォーム」「住宅リフォーム・改修工事」は
とび・土工/解体より対象業種としては緩いが、`trades`列には「塗装」の文字列一致が
無い限りタグを付けないため、スコアリング上は自然に評価が下がるだけで実害はない。

- 既存(14,688社＋第1弾mikomeru5,469社)との名寄せで1,805社を更新、18,165社を新規追加
- `ingest_mikomeru.py`は都道府県をCSVの列からそのまま読む設計のため、コード変更なしで
  全国データに対応できた
- 現状: `out/companies.db` は14,688 → **38,308社**(mikomeru由来 累計23,634社)。
  `scoring.py`実行済み。`prescore.py`はまだこの規模で再実行していない
  (対象プールが2.6倍になったため、次に実行する際は`--pref`指定なしで全国を
  対象にするか要相談)。`enrich.py`も未実行

---

## 3. やってはいけないこと

- **スキーマの再設計**: `db.py` の `SCHEMA` を作り変えない。列追加は `migrate()` の
  後付けリストに足す
- **接触ガードのバイパス**: 「今回だけ」で `can_contact()` を飛ばさない。
  過去に `dormant.py` で1箇所抜けており、テストが検出した実績がある
- **テストを緩める**: 落ちたら実装を直す。テストの閾値を下げて通さない
- **モデルを無条件採用**: 学習結果が常に良いとは限らない（反応81件でV1劣化を実測）
- **送信のリトライを無制限にする**: 4回で打ち切る。それ以上は相手に迷惑
- **LPやコンソールのデザイン変更**: 依頼されていない変更をしない

---

## 4. 運用開始後に見る数字

| 指標 | 見る場所 | 危険水準 |
|---|---|---|
| 配信停止率 | `suppress_cli.py check` | 3%超 → オファーか文面を見直す |
| 到達率 | `metrics.py` | メール95%未満 → 送信ドメイン評価を確認 |
| CAC | `console.html` | オファー価格×24×0.33 を超えたら停止 |
| モデル昇格 | `out/model_v2.json` の `active_model` | v1のままなら接触数が足りない |
| 停止後送信 | `out/audit.log` | 1件でもあれば即調査 |

---

## 5. 連絡すべき判断

以下は実装者が決めず、必ず確認を取ること。

- オファーの価格・訴求内容の変更
- 送信チャネルの追加（架電の自動化など）
- IM.md / console.html の数値を外部（買い手・顧客）に提示すること
  → 実データでの再生成が完了するまで禁止
- 接触上限（現在: 生涯6回 / 最短間隔10日）の緩和
- 個人情報の新たな取得項目の追加
- 他社への販売・譲渡に伴うテナント分離の要件
