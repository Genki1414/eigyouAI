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
- `senders.py`の`FormSender`(問い合わせフォーム自動送信)は`playwright install --with-deps
  chromium`が必要（Dockerfileに追加済み）。この開発セッションの環境は外部サイトへの
  疎通が許可リスト方式のプロキシ経由に制限されており実サイトでの動作確認ができて
  いない。**本番デプロイ後、`dry_run=False`で少数の実企業サイトに対して動かし、
  成功率と誤入力の有無を確認してから本格運用に入ること**

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

### T10. FormSenderのPlaywright強化(β版・進行中)
8/31リリースに向け、`senders.py`のFormSenderが「1件も実サイト送信に成功していない」
状態を解消するための改修。責務分離: `senders.FormSender`=送信対象決定・接触ガード・
履歴管理、`form_navigator.py`=Playwrightによる実ブラウザ操作、という分担にした。

- `form_navigator.py`(新規): `navigate_and_submit(url, values)`が本体。
  問い合わせページ探索(トップページしか無い場合に1階層だけ辿る)、フィールド判定
  (name/id/placeholder/aria-label/label文言/周辺テキストの同義語マッチ。会社名/氏名/
  姓・名分割/メール/メール確認/電話/郵便番号/住所/件名/本文に対応)、確認画面対応、
  CAPTCHA検知(自動突破はしない)、営業禁止文言・採用専用・会員専用フォームの検知、
  `SUCCESS`/`SKIP_*`/`FAILED_RETRYABLE`/`FAILED_UNSUPPORTED`のステータス分類を担当。
  企業管理・テナント管理には一切触れない設計
- `db.py`: `form_send_log`テーブルを追加(1試行=1行。company_id/tenant_id/offer_id/
  target_url/contact_url/status/reason_code/detected_fields/filled_fields/
  submit_attempted/success_evidence/error_message/retryable/playwright_run_id。
  本文そのものは個人情報配慮のため保存しない)
- `senders.py`: `FormSender._deliver()`は`form_navigator.navigate_and_submit()`を
  呼ぶだけの薄い層に変更。`SKIP_*`/`FAILED_UNSUPPORTED`は`permanent=False`(会社では
  なくチャネルの問題なので配信停止には入れない)。`FAILED_RETRYABLE`は
  `R.Retryable`として投げ、既存の`R.retry()`(4回リトライ)に乗せる
- `offers.py init`が未実行だっただけで、テナント/オファーのスキーマ自体は完成済み
  だったと判明。実行したところ`test_pipeline.py`の失敗が4件→1件(is_target_business
  除外の想定内挙動のみ)に減った
- `batch_form_test.py`(新規、旧`manual_form_test.py`を置き換え): 複数社をまとめて
  検証しSUCCESS/SKIP/FAILED内訳を集計するツール。
  `python3 batch_form_test.py --n 10 --run-label step1`
- 現状: β版検証のStep1(10社)〜Step4(100社)は本番サーバで実施済み。実データから
  見つかった不具合(問い合わせページ誤判定、フリガナ未対応、`.fill()`後にJSの
  input/changeイベントが発火せず値が反映されない、Cloudflare等のbotチャレンジ
  未検知、確認ボタン押下がCookieバナー等に阻害される、`<select>`未対応)を
  順次修正。特に`<select>`(プルダウン)対応が最も効果が大きく、以降の成功率が
  底上げされた。実測: 累計約210件試行で成功約58件(約27.6%)。ユーザーの
  「送信成功率は100%を目指さなくて構いません」という方針どおり、フリガナが
  一部サイトで未反映、SPA的なサイトでの取得タイムアウト、iframe埋め込みの
  外部フォーム未対応、といった既知の残課題は許容範囲としてβ版のまま進める
- cronのペーシング上限・多重起動防止ロックを実装(β版チェックリスト7番)。
  `config.py`に`FORM_MAX_PER_RUN`(50)/`FORM_MAX_PER_HOUR`(20)/`FORM_MAX_PER_DAY`
  (100)/`FORM_MAX_PER_TENANT_PER_DAY`(100)を追加。`FormSender._check_quota()`が
  `form_send_log`の直近件数を集計し、超過時はPlaywrightを一切起動せず
  `SKIP_QUOTA_EXCEEDED`を返す(相手サイトへの負荷・bot判定回避・不具合時の
  被害拡大を防ぐための保守的な初期値。実績を見てから引き上げる想定)。
  `deploy/crontab`の送信行は`flock -n /tmp/eigyouai_send.lock`でラップし、
  前回実行が終わっていない場合は待たずにスキップする(実サイトへの送信は
  取り消せないため、二重実行より「今回はスキップ」の方が安全という判断)
- チェックリスト9番(テナント・オファー単位で送信できる)対応。実は`send_campaign()`
  が`LEFT JOIN offers o ON o.id = 1`とオファーIDを固定していたため、
  `compose.py --offer`で別オファーを指定して文面生成しても、送信時の送信者情報
  ([FormSender]の`tenant_id`/`offer_id`含む)は常にオファー1のテナントに固定される
  という不具合が判明。`campaigns`に`offer_id`列を追加し、`compose.py`が
  `--campaign`実行時に`campaigns.offer_id`を確定させ、`send_campaign()`は
  `COALESCE(cp.offer_id, 1)`(旧キャンペーンとの後方互換用)でオファー→テナントを
  解決し、`get_sender()`経由で`FormSender`に正しい`tenant_id`/`offer_id`が渡る
  ように修正。これにより`FORM_MAX_PER_TENANT_PER_DAY`のテナント別上限も
  本番経路で実際に機能するようになった。`run.py all --demo`・`api.py test`
  (`can_contact()`バイパス防止テスト含む)・`test_pipeline.py`・
  `test_concurrency.py`で回帰なしを確認済み
- チェックリスト3番(重複送信0件)・4番(配信停止企業への誤送信0件)は、
  `FormSender`が既存の`db.can_contact()`(接触ガード)・`Idempotency`(冪等性)の
  仕組みをそのまま利用しており、これらのコードパス自体は今回のPlaywright化で
  変更していないため、`test_pipeline.py`の抑制テスト・`test_concurrency.py`の
  既存カバレッジで担保されていると判断。専用の新規テストは追加していない

### T11. console.htmlを実データ生成方式へ置き換え
「販売していくサービスだからUIを仕上げたい」という要望を受け、`console.html`を
実データ接続とデザイン刷新の両方で対応。

- これまでの`console.html`はリポジトリ直下に置かれた静的ファイルで、2026年7月の
  架空キャンペーン結果がHTML内に直接埋め込まれていた(サーバのAPIには一切繋がって
  いなかった)。`enrich_review.py`と同じ「TEMPLATE文字列内の`__DATA__`を実データの
  JSONで置換する」方式に揃え、`console.py`(新規)が`out/companies.db`から都度
  `out/console.html`を生成するようにした。リポジトリ直下の`console.html`は
  役目を終えたため削除(README.mdは元々`out/console.html`と記載しており、
  実は最初からそちらが正しい設計だった)
- `console.py`は`metrics.compute()`(metrics.py・api.pyと同じ集計ロジックを再利用。
  二重化しない)・`out/model_v2.json`(存在すれば)・DBへの直接クエリ(実送信文面
  サンプル・次ロット候補・対象プールの状況・オファー一覧)からデータを組み立てる
- 本番キャンペーンがまだ1件も無い状態(このセッション時点の実状態)でも壊れない
  ことを最優先にした。ファネル・チャネル別・学習モデル等は「準備中」の空状態
  表示になり、シミュレーション値や存在しない配列への参照でクラッシュしない
  ことをjsdom(Node)でのランタイム検証で確認済み(このサンドボックスは以前から
  Playwrightのブラウザ起動に失敗する既知の制約があるため、jsdomで代替した)
- 新セクション「フォーム送信 β検証実績」を追加。`form_send_log`の集計
  (status別・reason_code別の件数、成功率)を表示する。本番キャンペーン開始前
  でも唯一の実測値がこれなので、ファネルが空でも独立して意味のある情報になる
- デザインは既存の建設業ブランド(安全色ストライプ・コンクリート/スチール配色・
  IBM Plex Monoでの数値表現)を維持しつつ、稼働状況バッジ(準備中/稼働中)・
  空状態の文言・β検証セクションのバー表示を追加する形で刷新
- README.md/INDEX.mdの`console.html`関連の記述を`console.py`→`out/console.html`
  の生成方式に合わせて更新

### T12. 送信先リスト作成(他社に売るSaaSとしての第一歩)
「これは販売するシステムだから、販売できる仕様にしてほしい。たとえば送信先リスト
作成とか」という要望を受け対応。想定は他社に使わせるSaaS(offers.pyのtenant/offer
構想の実装)で、顧客が自分の送信先リストを作れるようにする。最初から顧客別ログイン
(テナントごとのAPIキー)で実装。今回のスコープは「リスト作成」までで、作成した
リストをキャンペーン送信に接続する部分は次フェーズ。

- 現状把握: `companies`(38,322社)は全社共有の1プールで、テナント単位の分離が
  一切なかった。`tenants`/`offers`は存在したが、実際に顧客が使う経路(API・認証・
  UI)は無かった
- `companies.owner_tenant_id`列を追加(NULL=全テナント共有の国交省/mikomeru由来
  マスタ、値あり=そのテナント専用の非公開データ)。CSV取込で追加された企業は
  他テナントから一切見えない
- `tenants.api_key`列を追加。`offers.py add-tenant`でテナントを追加すると
  この場でのみ表示されるAPIキーが発行される(`offers.resolve_tenant_by_key()`で
  Authorization: Bearerヘッダから解決。クライアントが送るtenant_idは一切信用しない)
- `target_lists.py`(新規): `target_lists`/`target_list_members`テーブルを追加。
  - フィルタ型: 都道府県・業種・スコアランク・資本金上限等、許可リスト化した
    項目のみでパラメータ化SQLを組み立てる(顧客入力を直接SQLへ混ぜない)。
    件数は`preview_filter()`で保存前にプレビューできる
  - CSV型: 顧客持込のCSV(列名の日本語/英語ゆれを吸収)を取り込む。
    `db.normalize_name()`で既存の共有マスタ or 自テナントの既存データと照合し、
    一致すれば紐付け、無ければ`owner_tenant_id`付きの新規企業として追加
  - 両方式とも1リストあたり上限20,000件(FormSenderのペーシングと同じ、
    暴走・誤操作の被害を抑える保守的な初期値)
- `api.py`に`/api/tenant/lists*`系エンドポイントを追加(既存の運用専用
  `SALES_ENGINE_API_KEY`とは完全に別の認証)。`api.py test`に9件のテストを追加し、
  特に「他テナントのリストIDを指定しても404」「他テナントのCSV非公開企業が
  自分のフィルタ結果に出てこない」というテナント境界の検証を最重要項目として含めた
  (全て確認済み)
- `list_builder.html`(新規、静的ページ): APIキーを入力して接続し、フィルタでの
  プレビュー・保存、CSVアップロード、保存済みリスト一覧・詳細を操作できる画面。
  console.htmlと同じ配色・ブランドを踏襲
- 都道府県は当初プルダウン(単一選択)だったが、「プルダウンではなくチェックボックスに、
  エリア単位でも選択可能に」との要望で変更。`filters.pref`(単数)を`filters.prefs`
  (配列。他の項目と同じ許可リスト方式)に置き換え、フロントは8地方区分の
  エリアチップ(クリックで管内の都道府県チェックを一括ON/OFF)+47都道府県の
  個別チップを実装。この過程で、チップ(`<label>`がcheckboxを内包)を
  `"click"`イベントで扱うと、ブラウザのラベル→checkbox自動転送と自前のトグルが
  二重に効いて見た目上何も起きない不具合を発見(業種・ランク等の既存チップにも
  同じ不具合があった)。全チップの判定を`"click"`から`"change"`ベースに直して解消
- 資本金の上限は自由入力(数値欄)から選択式チップ(300万/500万/1,000万/3,000万/
  5,000万/1億円以下・指定しない、単一選択)へ変更
- フィルタ選択が変わるたびに(「件数をプレビュー」ボタン無しで)自動的に件数を
  再集計するようにした。連続変更はデバウンス(200ms)して1リクエストに合流させ、
  古いリクエストの応答が新しい選択結果を上書きしないようリクエストにseqを振っている
- 「システム自体にホーム画面を作成、UIをmikomeru.net(類似の業界ツール)の管理画面
  のような形式にしたい」という要望を受け、単一の縦並びページから、左サイドバー+
  ページ切替(ホーム/条件でリスト作成/CSVから作成/保存済みリスト/接続設定)の
  構成へ再編。ホームには保存済みリスト数・対象企業数合計を表示(新規エンドポイントは
  追加せず、`/api/tenant/lists`の応答をフロント側で集計するだけで済ませている)。
  ページ切替はリロード無しのSPA的な実装(`.page`のdisplay切替)で、既存のAPI呼び出し
  ロジック(プレビュー・保存・CSV取込・一覧・詳細)はそのまま各ページへ配置し直しただけ
- 保存済みリストから実際に送信できるようにした(「リリースするにあたって機能として
  弱い、送れるようにして」との要望)。`target_lists.send_list()`が
  `campaigns`/`touches`を組み立て、既存の`senders.send_campaign()`にそのまま
  委譲する(独自の送信経路は作らない。`can_contact()`・冪等性・FormSenderの
  ペーシング上限はすべて既存の仕組みが適用される)
  - テナント側で件名・本文をその場で入力する方式(AI生成はしない。コスト面と
    テナントごとに訴求内容が違うため)
  - `offers.add_tenant()`は自動で最低1件のデフォルトオファーも作成するように変更
    (`campaigns.offer_id`経由のテナント解決に必須のため。`target_rule`は
    `"1=0"`にして誤って他の経路から使われないようにしている)
  - 二重送信対策: `target_lists.campaign_id`列を追加し、同じリストへの送信は
    1つのcampaignを使い回す。2回目以降の送信は`INSERT OR IGNORE`と
    `sent_at IS NULL`条件により、未送信分だけが再試行される(リトライにはなるが
    重複送信にはならない)
  - `POST /api/tenant/lists/<id>/send`はユーザーの意思決定で
    **`dry_run`を既定`true`**にした(実サイトへの送信は取り消せないため)。
    `list_builder.html`側でも、本番送信(dry_run解除)時は赤字の警告表示と
    ブラウザの確認ダイアログを挟む
  - api.py testに、dry_runでのキャンペーン作成・二重送信されないこと(同じ
    campaign_idを使い回す)・他テナントのリストへは送信できないことを追加確認
- 「機能は隠さないで、メニューに表示して」との要望(mikomeru.net管理画面の
  サイドバー全項目のスクリーンショットを参照)を受け、サイドバーをmikomeru
  相当の全項目構成に拡張した: フォーム送信(自動送信/自動送信ログ/送信文章
  テンプレート/送信元テンプレート/送信除外設定)・会社情報(リスト取得/CSV検索/
  保存済みリスト、既存の「条件でリスト作成」「CSVから作成」をmikomeru呼称に
  改名)・その他(担当者管理/お知らせ一覧/その他ログ/マニュアルDL/接続設定/
  ログアウト)。実装済みの機能だけに絞らず、まだ無い機能もメニュー項目として
  見せて「準備中です」と明示する方針にした(隠して無かったことにしない)
  - 実装したもの: 自動送信ログ(`GET /api/tenant/send-log`。`form_send_log`を
    tenant_idで絞り込むだけの新規エンドポイント)、自動送信(保存済みリストへ
    誘導する説明ページ、実処理は既存の送信フォームを流用)、ログアウト(APIキーを
    ローカルストレージから消して未接続状態に戻す)
  - 未実装のまま画面だけ用意したもの: 送信文章/送信元テンプレート・送信除外設定・
    担当者管理・お知らせ・その他ログ・マニュアルDL。いずれも「準備中です」の
    説明文のみのプレースホルダーで、機能があるように見せかけない
- 「まだない機能を作りこんで」→「どこまで作り込むか」を確認したところ
  「7つ全部」との回答。以下、実装した分から追記していく(1/7)
  - **送信除外設定**: `tenant_exclusions`テーブル(tenant_id, company_id複合PK)を
    新規追加。全テナント共通の法令対応`suppression`とは別物で、
    「この会社は競合他社だから自社だけは送りたくない」という経営判断の除外。
    他テナントの送信には一切影響しない。`db.can_contact()`に`tenant_id`引数を
    追加し(既定None=従来どおり)、`suppression`チェックの直後に
    `tenant_exclusions`もチェックするようにした。`senders.send_campaign()`の
    最終ガード呼び出しにも`tenant_id=r["tenant_id"]`を渡すよう変更済み
    (=`can_contact()`をバイパスする新しい経路を作っていない)
    - 新規API: `GET /api/tenant/companies/search?q=`(2文字未満は400。
      共有マスタ+自テナント非公開データのみ検索対象、他テナントの非公開企業は
      検索にも出さない)、`GET /api/tenant/exclusions`、
      `POST /api/tenant/exclusions`、`POST /api/tenant/exclusions/remove`
    - `list_builder.html`の`exclude`ページを実装(検索→除外に追加→一覧→解除)。
      準備中プレースホルダーから置き換え
    - api.py testに追加: 検索の401/400、company_id不正/存在しない場合の
      400/404、追加後にcan_contact()がテナント除外理由でFalseを返すこと、
      他テナントの送信には影響しないこと(テナント分離)、解除後に再びTrueへ
      戻ることを確認。テスト対象企業は「素の状態でcan_contact()がTrueの会社」を
      事前に選ぶようにした(他テスト区画の副作用で既に反応済み扱いの会社を
      誤って選ぶと、除外の効果を検証できないため)
  - **送信文章テンプレート**(2/7): `message_templates`テーブル(id, tenant_id,
    name, subject, body, created_at)を新設。送信自体には手を加えず、
    `list_builder.html`の送信フォームに「テンプレートを使う」プルダウンを
    追加して件名・本文を自動入力するだけの機能(送信経路は既存のまま)
    - 新規API: `GET/POST /api/tenant/templates`、
      `POST /api/tenant/templates/delete`(他テナントのテンプレートは
      404で削除できない。テナント分離はDELETE文の`WHERE tenant_id=?`条件で担保)
    - `list_builder.html`の`tmpl-body`ページ(保存・一覧・削除)と、
      保存済みリストの送信フォームへの`<select id="sendTemplate">`追加
    - api.py testに保存・一覧・テナント分離・削除(自テナント/他テナント)の
      確認を追加
  - **送信元テンプレート**(3/7): 実装の前に既存バグを発見して修正した——
    `tenants.sender_name`列は`offers.add_tenant()`が保存していたが、
    `senders.send_campaign()`の送信者解決クエリは`tn.name`(テナントの
    内部管理名。例:「自社（AshiBase）」)を見ており、`sender_name`(例:
    「AshiBase（足場ベース）」)は一度も読まれていなかった。そのため
    テンプレートで送信者名を切り替える機能を作っても実際の送信には
    反映されないはずだった。`senders.py`のSELECT文を`tn.sender_name sname`
    に修正(1行)。senders.py test/api.py testとも green のまま
    - `sender_templates`テーブル(id, tenant_id, name, sender_name,
      sender_email, sender_address, optout_url, created_at)を新設。
      「有効にする」を押すと`db.activate_sender_template()`が
      `UPDATE tenants SET sender_name=...`する。送信側のロジックは
      1文字も変えていない(元々tenantsのその列を読む設計だったものを
      正しく読むようにしただけ)
    - 新規API: `GET/POST /api/tenant/sender-templates`,
      `POST /api/tenant/sender-templates/delete`,
      `POST /api/tenant/sender-templates/activate`(すべてテナント分離を
      `WHERE tenant_id=?`で担保。他テナントの操作は404)
    - `list_builder.html`の`tmpl-sender`ページ(保存・一覧・有効化・削除)を実装
    - api.py testに、保存・一覧・テナント分離・有効化後に実際に
      `tenants.sender_*`へ反映されること・削除の確認を追加
  - **担当者管理**(4/7): 1つのapi_keyをテナント全体で使い回すのではなく、
    担当者ごとに個別のapi_keyを発行できるようにした(退職・異動時にその
    担当者のキーだけ失効させられる)。`offers.py`に`staff`テーブル
    (id, tenant_id, name, email, api_key, created_at)を新設し、
    `offers.resolve_tenant_by_key()`が`tenants.api_key`だけでなく
    `staff.api_key`も見るように拡張。どちらのキーで認証しても解決される
    `tenant_id`は同じで、担当者ごとに見えるデータが変わるわけではない
    (テナント単位でデータ共有、というこのSaaSの設計方針どおり)
    - 新規API: `GET/POST /api/tenant/staff`, `POST /api/tenant/staff/revoke`。
      一覧応答にapi_keyは含めない(発行直後の応答でしか返さない)
    - `list_builder.html`の`staff`ページ(追加・一覧・失効)を実装。
      発行したAPIキーは「この画面でしか表示されない」ことを明記
    - api.py testに、担当者専用キーで実際にテナントのデータへアクセスできる
      こと・失効後は401になること・テナント分離の確認を追加
  - **お知らせ**(5/7): 全テナント共通の告知機能。他の機能と違いテナントごとの
      Web管理画面は作らず、`suppress_cli.py`・`offers.py`と同じ「CLIで運用側
      (HQ)が投稿する」方針にした(このプロジェクト全体の一貫した設計判断)
    - `announcements`テーブル(id, title, body, published, created_at)を
      新設。tenant_idを持たない(=全テナントに同じ内容が見える)
    - 新規CLI: `announcements_cli.py`(`add`/`list`/`publish`/`unpublish`)
    - 新規API: `GET /api/tenant/announcements`(公開中のみ返す。認証は必要だが
      テナントによる絞り込みはしない)
    - `list_builder.html`の`news`ページを実装(一覧表示のみ)
    - api.py testに、未認証401・公開中のものだけ返る・非公開は出ない・
      全テナントに同じ内容が見えることの確認を追加
  - **その他ログ**(6/7): 「自動送信ログ」(企業ごとのフォーム送信結果=
    form_send_log)には出ない、テナントの操作履歴(リスト作成・送信開始)を
    時系列でまとめた画面。設計方針どおり新規の記録用テーブルは作らず、
    既存の`target_lists`(作成イベント)と`campaigns`(送信開始イベント。
    `target_lists.campaign_id`経由で紐付け)を突き合わせて動的に作る
    - `target_lists.activity_log(con, tenant_id, limit)`を新設。同じリストへの
      再送信は同じcampaign_idを使い回す仕様(send_list()参照)なので、
      「送信開始」イベントはリストごとに初回送信時刻のみを表す
    - 新規API: `GET /api/tenant/activity-log`
    - `list_builder.html`の`otherlog`ページを実装(一覧表示)
    - api.py testに、未認証401・リスト作成/送信イベントが出ること・
      テナント分離の確認を追加
  - **マニュアルDL**(7/7・7つ全部完了): 接続〜リスト作成〜送信〜除外設定〜
    送信元設定〜担当者管理までを一通り説明する使い方ガイドを`manual`ページに
    直接埋め込んだ。「PDFとして保存」ボタンはブラウザ標準の`window.print()`を
    呼ぶだけで、外部のPDF生成ライブラリは使っていない(この環境はCDN/外部
    ライブラリが使えないため、かつ標準の印刷機能で十分に用が足りる)。
    `@media print`でサイドバー・トップバー・ボタン類を消し、選択中ページの
    内容だけを紙面いっぱいに出す
    - バックエンドの変更なし(静的なガイド文とCSSのみ)
- 未対応(次フェーズ): 顧客の新規登録・課金・自分でのAPIキー発行UI

**✅ HTTPS化 完了(2026-08-22朝、人間による実施)**:
ドメインは`app.ashibase.jp`(既存の`ashibase.jp`にAレコードを追加)。
`https://app.ashibase.jp/`でアクセスできる。

**当初`deploy/Caddyfile`でCaddyを使う設計にしていたが、実際にデプロイした
Hetznerサーバーは同じ80/443番ポートを既存のnginx(Stock Factory側の
`stockfactory-hq`/`stockfactory-runtime`と共用)が既に使っていたため、
Caddyはポート競合で起動できなかった。そのため最終的には以下の構成に
切り替えた:**

- Caddyコンテナは`docker compose stop caddy`で停止したまま(未使用)。
  `deploy/Caddyfile`・`docker-compose.yml`のcaddyサービス定義はコードとしては
  残しているが、**単独ホストで動かす場合の代替手段**という位置づけに変わった
- 実際にTLS終端をしているのは、サーバーに元々あった**nginx**。
  `/etc/nginx/sites-available/app-ashibase`に新規サーバーブロックを追加し、
  `proxy_pass http://127.0.0.1:8787`でapiコンテナへ転送している
  (`stockfactory-hq`と全く同じパターン)
- 証明書は`certbot --nginx -d app.ashibase.jp`で取得(Let's Encrypt。
  自動更新のcronはcertbotが標準で設定済み)
- apiコンテナは引き続き`127.0.0.1:8787`限定公開のまま
  (`deploy/docker-compose.yml`)。**8787を直接インターネットへ公開する
  構成には戻さないこと**
- list_builder.htmlはapi.py自身が同一オリジンで配信するので、
  フロント側のコード変更は無し(`location.origin`が
  `https://app.ashibase.jp`になるだけ)

**今後、別のサーバー(80/443が空いている単独ホスト)にデプロイする場合**は、
`deploy/Caddyfile`のCaddy構成がそのまま使える想定で残してある
(`.env`の`EIGYOUAI_DOMAIN`を設定し`docker compose up -d`するだけ)。
共用ホストに追加する場合は、今回と同様に既存nginxへの追加を先に検討すること。

### T13. β版リリース準備(2026-08-21夜)

第三者のβユーザーに安全に使わせられる状態へ近づける回。**今夜は実在企業への
本番フォーム送信を行っていない**(すべてdry_runまたはPlaywright/実チャネルに
到達しない状態でテスト)。

- **P0-2 テナント分離の監査**: 企業リスト・保存済みリスト・CSV・送信文章/
  送信元テンプレート・担当者・送信除外・オファー・送信履歴・その他ログ・
  お知らせ・FormSender関連データのすべてで、認証は`Authorization: Bearer`から
  サーバ側で解決した`tenant_id`のみを信用し、クライアントが指定した`tenant_id`
  を一切信用しない設計になっていることを確認(`grep`で全endpoint走査)。
  ギャップを1件発見・修正: `GET /api/tenant/companies/search`の他テナント
  非公開企業リークを確認するテストが無かったため追加(実装自体は元から安全)。
  オファーはテナント向けの直接読み取りエンドポイントが無く、内部処理は
  すべて`WHERE tenant_id=?`で絞り込まれているため、追加のリーク面は無い
- **P0-3 Kill Switch**: `kill_switch`(全体・id=1固定1行)と`tenant_kill_switch`
  (テナント別。行の存在=停止中)を新設。`senders.send_campaign()`が
  全送信経路(手動送信/list_builder.htmlからの送信/cron/Stock Factory運用API)
  の唯一の合流点であることを確認した上で、そこ1箇所(dry_run=Falseの行のみ)
  でチェックするようにした。**初期値は「全体停止中」**(`db.migrate()`が
  安全側で自動投入。本番送信には人間の明示的な解除が必須)
  - 新規CLI: `kill_switch_cli.py`(status/stop/resume、`--tenant`で個別指定可)
  - 新規API: `GET/POST /api/ops/kill-switch`(運用専用)、
    `GET /api/tenant/kill-switch`(自テナントの状態を見るだけの読み取り専用。
    他テナントの状態や制御権限は渡さない)
  - `list_builder.html`に停止中バナーを表示し、本番送信チェックボックスを
    強制的にドライラン固定・disabled化する(UIは補助。強制力はサーバ側)
- **P0-4 cron/二重送信安全性の監査**: 監査の過程で2件の実在するTOCTOU競合を
  発見・修正した(いずれも「重複送信0件」の最重要条件に直結するため)
  1. `senders.py`の`BaseSender.send()`: 冪等キーの重複チェックが
     「SELECTで確認→delivery後にINSERT」の2段階だったため、同じキーへの
     2つの同時リクエスト(ボタン連打・2人の担当者の同時送信)が両方とも
     「未送信」と判定し、実チャネルへの配信まで二重に進んでしまう恐れが
     あった。`idempotency.key`(PRIMARY KEY)への`INSERT OR IGNORE`を
     delivery**前**に行う原子的な「claim」方式に変更。失敗時はclaimを
     解放して再試行を許す(占有したまま失敗すると永久にスキップ扱いに
     なってしまうため)。5スレッド同時実行で実送信1回になることを
     `senders.py test`に追加して確認
  2. `target_lists.py`の`send_list()`: `if lst["campaign_id"]: ... else: 新規作成`
     も同型のTOCTOUで、同じリストへの2つの同時送信リクエストが別々の
     campaignを作ってしまう恐れがあった。`UPDATE target_lists SET
     campaign_id=? WHERE id=? AND campaign_id IS NULL`による原子的な
     「先着1件だけ採用」方式に変更(負けた側が作ったcampaign行は
     touchesが紐付かないまま残るだけで実害なし)。3スレッド同時実行で
     採用されるcampaign_idが1つだけになることを`api.py test`に追加して確認
  - cronの`0 9,14 * * 1-5 flock -n /tmp/eigyouai_send.lock python3 senders.py 1 1`
    は既に多重起動防止済み(確認のみ、変更なし)。ただし現状`senders.py`の
    CLIは`dry_run=True`固定のため、この cron 自体はまだ実送信していない
  - **既知の残課題(未対応・低リスクと判断)**: (a) `followup.py`が
    `db.connect()`(storage.py経由)ではなく`sqlite3.connect()`を直接使っており、
    将来Postgresへ移行した際にAPI/cronと別のデータベースを見てしまう
    可能性がある。現状はSQLite運用のため実害なしだが、Postgres移行時は
    要修正。(b) サーバーがidempotencyキーをclaimした直後(delivery前)に
    クラッシュすると、そのキーは「占有されたまま」残り、以後そのtouchは
    自動では再試行されない。危険な方向(二重送信)ではなく安全な方向
    (未送信のまま止まる)の失敗モードなので許容したが、運用上は
    `idempotency`テーブルの古い未確定行を定期的に監視するとよい
- **P0-5 企業1社単位の送信結果・履歴**: 新規の記録用テーブルは作らず、
  既存の`target_list_members`(1社×1リストの「現在の状態」)と
  `form_send_log`(1試行ごとの「履歴」。もともと1試行=1行で追記されるため、
  何もしなくても時系列の履歴になっている)を拡張して対応した
  - `target_list_members`に`send_status`(PENDING/PROCESSING/SUCCESS/SKIP/
    FAILED_RETRYABLE/FAILED_UNSUPPORTED/STOPPED)・`reason_code`・
    `retry_count`・`last_error`・`latest_result`・`started_at`〜`updated_at`・
    返信/商談化/受注の手動記録用列(`replied`/`deal`/`won`とその日時・`memo`)を追加
  - `db.sync_target_list_member_status()`: `send_list()`が`send_campaign()`を
    呼んだ直後(dry_run=falseのときだけ)に呼び、結果を`target_list_members`へ
    反映する。**重要な落とし穴を発見して回避した**: `touches.sent_at`は
    dry_run/実送信を問わず成功時に同じ形で立つため、「sent_atがある=実送信
    成功」と単純判定すると、過去にdry_runで「送信」した企業を後で本番送信した
    際にまとめて誤ってSUCCESS扱いにしてしまう。`SendResult.provider_id`が
    dry_run時は必ず`mock_`接頭辞になる既存の規約を使い、`touches.note`の
    `provider_id=mock_`有無で実送信かどうかを判別するようにした
  - PROCESSING状態は、その回に`send_campaign()`が実際に対象とする行
    (`sent_at IS NULL`の行)だけに絞って立てる(全件に立てると、対象外の
    既送信分がPROCESSINGのまま更新されず止まって見えてしまうため)
  - `GET /api/tenant/lists/<id>`は`?status=success|failed|skip|pending|
    replied|deal|won`で絞り込めるようにした(許可リスト方式。フリーテキストで
    SQLを組み立てない)
  - `POST /api/tenant/lists/<id>/outcome`: 返信・商談化・受注を担当者が
    手動記録する(β版はメール自動取得等をしない)。list_id経由でテナント境界を
    確認するため、他テナントのリストへは記録できない(404)
  - **原価計測**: `form_send_log`に`list_id`・`retry_count`・
    `execution_seconds`・AI/外部API/サーバー原価の列を追加。`config.py`に
    `SERVER_MONTHLY_COST_YEN`(概算値。実績に合わせて更新する)と、
    実行時間から月額費用を按分する`estimate_server_cost_yen()`、モデル別
    APIの単価テーブル`AI_PRICING_YEN_PER_TOKEN`(現状フォーム送信はAIを
    使わないため空。将来compose.py等を接続する前提の器)を追加。
    `R.retry()`が同じ`_deliver()`を複数回呼ぶ既存の仕組みにより、retryのたびに
    `form_send_log`へ1行ずつ記録される(=失敗が多いフォームほど原価が
    積み上がって見える設計に、追加のコードなしで既になっている)
- **P1 企業単位の送信結果UI**: `list_builder.html`のリスト詳細画面を拡張。
  企業ごとに状態(バッジ表示)・reason・返信/商談/受注チェックボックスを一覧表示し、
  フィルタ(すべて/未送信/成功/失敗/SKIP/返信あり/商談あり/受注あり)で
  絞り込める。会社名クリックで`GET /api/tenant/send-log?company_id=`から
  その会社の送信履歴(時系列)をその場に表示する
- **P1 β版ダッシュボード**: `GET /api/tenant/dashboard`を新設し、ホーム画面に
  「今月、AI営業社員が○社へ営業しました」の見出しと、今月の対象企業数/
  試行数/成功/SKIP/FAILED数、累計送信成功数、返信/商談化/受注の累計件数、
  最近の営業履歴(送信ログの直近10件の再利用)を表示。既存の
  `form_send_log`/`target_list_members`から集計するだけで、新しい集計用の
  巨大なデータ構造は作っていない
- **P2 メール開封・クリック計測(データ構造のみ。メール送信機能自体は
  未実装のため、追跡エンドポイントは今夜は実装していない)**:
  - `touches`に`email_sent_at`〜`email_unsubscribed_at`の11列を追加
    (送信/配信/開封(初回・最終・回数)/クリック(日時・回数)/バウンス/配信停止)
  - `email_tracking_tokens`テーブルを新設(token主キー、`touch_id`、
    `kind`('open'|'click')、`target_url`)。tokenはtenant/campaign/company/
    受信者を直接推測できない、十分に推測困難なランダム値にする設計
    (`secrets.token_urlsafe()`想定。実装時にDBへ保存する値そのものを
    ランダムにする、という方針だけ決めており、生成関数はまだ書いていない)
  - 将来メール送信機能を実装する際の想定エンドポイント(未実装):
    `GET /track/open/{token}` → `touches.email_opened_at`等を更新して
    1x1透明画像を返す。`GET /track/click/{token}` → `email_clicked_at`等を
    更新後、`email_tracking_tokens.target_url`へ302リダイレクト
  - **開封検知は「確実に読んだ」ことの証明にはならない**(Apple Mail
    Privacy Protection・画像自動読込・セキュリティソフト等の影響)。
    実装時はUI表現を「開封検知」「推定開封率」等にとどめ、成果指標としては
    返信 > クリック > 開封検知 の順で信頼性が高いものとして扱うこと
- **原価・粗利レポート(管理者専用CLI)**: `cost_report_cli.py`を新設。
  `form_send_log`の`total_estimated_cost_yen`等を集計するだけで、新しい
  集計用テーブルは作らない。`overall`(全体・今月/累計)、`by-tenant`
  (テナント別・今月)、`profit --tenant --monthly-fee`(1テナントの
  月額売上に対する粗利試算)の3コマンド。原価情報は顧客向け画面
  (list_builder.html)には一切露出していない(このCLIのみで見る)

### T14. 初回実送信で発覚した重大バグの修正 + MIKOMERU相当の目視確認機能(2026-08-22)

**背景**: β版リリース後、初めて実在企業7社(秋田県)へ本番フォーム送信を実行した際、
画面上は「送信7 失敗0」と出ていたが、実際にはPlaywrightが一度もサイトへ触れて
いなかった(冪等キーの汚染により「送信済み」として即スキップされていた)。原因は
以下3つの重なりで、いずれも「ドライランと本番送信が同じ状態を共有していた」ことに
起因する:

1. `send_campaign()`のSELECTが`sent_at IS NULL`のみを対象にしており、ドライランで
   立った`sent_at`を除外していた(対象0件の場合`None`を返し、list_builder.html側で
   `Cannot read properties of null`のエラーになっていた)
2. 冪等キーが`dry_run`の有無を問わず同一形式(`send:{campaign_id}:{company_id}:{step}`)
   だったため、ドライランが冪等キーを占有し、後続の本番送信が「送信済み(冪等キー
   一致)」として`_deliver()`まで到達せずスキップされていた
3. `can_contact()`の生涯接触上限・最短間隔(`MIN_TOUCH_INTERVAL_DAYS`)判定が、
   ドライラン分の`sent_at`も本当の接触としてカウントしており、ドライラン直後の
   本番送信が「最短間隔未満」でガードに阻まれる状態だった

いずれも`touches.note`の`"provider_id=mock_"`接頭辞(既存のドライラン判別規約)で
本番/ドライランを区別するよう修正。あわせて`send_list()`のtouches作成を
`INSERT OR IGNORE`から`ON CONFLICT DO UPDATE`(未送信の行のみ)に変え、ドライラン後に
件名・本文を直して再送信した場合に最新の内容が反映されるようにした。
汚染されてしまった実データ(冪等キー・touches・target_list_members)は
本番サーバー上で手動クリーンアップして復旧させ、その後の再送信で実際に
Playwrightが動いたことを確認している(結果は7社中0件成功・5件「送信ボタンは
押したが完了確認できず」・2件CAPTCHAでSKIP — 実測値であり、成功率の低さ自体が
今後の`form_navigator.py`改善課題)。回帰テストを`senders.py test`に追加済み
(「ドライラン後の本番送信(冪等キー分離)」)。

**MIKOMERU相当のフォーム送信機能整備(同日)**: 実マニュアルを見た上で、
自社の「フォーム送信」領域(自動送信/自動送信ログ)がMIKOMERUとどれだけ違うかを
洗い出し、最も価値の高い差分から着手した:

- **送信前後スクリーンショット**(MIKOMERUの「送信前画像」「送信後画像」相当):
  `form_navigator.navigate_and_submit()`に`screenshot_dir`引数を追加し、
  問い合わせページ到達直後(入力前)と送信ボタン押下後(送信を試みた場合のみ)に
  `page.screenshot()`を撮って`out/form_screenshots/`配下へ保存(Dockerの
  `engine-data`ボリューム上なので永続化される)。パスは`form_send_log`の
  新規列`screenshot_before_path`/`screenshot_after_path`に記録。撮影・保存の
  失敗は送信処理自体を止めない(あくまで補助情報)。
- 配信は`GET /api/tenant/send-log/{id}/screenshot?kind=before|after`
  (テナント認証必須。`form_send_log.tenant_id`が一致する記録のみ返す=
  テナント分離)。list_builder.htmlの自動送信ログ画面に「確認」ボタンを追加し、
  クリックで画像をモーダル表示する(Bearer認証のため`<a href>`では開けず、
  `fetch()`でBlobとして取得し`URL.createObjectURL()`で表示)。
- `h_tenant_send_log`に`?q=`(会社名部分一致)・`?status=`(カンマ区切りで
  複数ステータス指定)フィルタと、`counts`(ステータス別内訳。フィルタ前の
  全体件数)を追加。画面上部にMIKOMERU同様の集計バッジ(クリックでON/OFF
  切替可能なフィルタ)を表示するようにした。
- `form_send_log.status`の日本語ラベル対応表(`LOG_STATUS_LABELS`)を新設し、
  MIKOMERUの「営業拒否」に相当する`SKIP_NO_SOLICIT`をそのまま「営業拒否」と
  表示するようにした(検出ロジック自体は既存の`_detect_no_solicit()`が
  以前から実装済みだった。UI表現のみの追随)。
- **自動入力機能(手動送信サポート)**: MIKOMERUはChrome拡張(専用マニフェスト・
  Web Store配布)で実現しているが、本番未検証のブラウザ拡張をこの場で作って
  すぐ動く保証ができない(拡張のパッケージング・固定ID割当・実ブラウザでの
  読み込みテストはこの環境から確認できない)ため、同じ利用体験を
  **ブックマークレット**で実現した:
  - `autofill_queue`テーブル(テナントにつき最新1件)を新設。「自動送信ログ」画面の
    失敗行(`FAILED_UNSUPPORTED`/`FAILED_RETRYABLE`のみ。`SKIP_NO_SOLICIT`等の
    意図的スキップは対象外=MIKOMERUの「営業拒否」「フォームなし」除外と同じ考え方)
    に「自動入力」ボタンを追加。押すと`POST /api/tenant/send-log/{id}/autofill-queue`
    が、`list_id`→`target_lists.campaign_id`→`touches`の逆引きで元の件名・本文を
    復元し(保存済みリスト経由の送信のみ復元可能。それ以外は400でその旨を返す)、
    送信元テンプレートの情報と合わせて`autofill_queue`へ保存。対象企業のフォームURLを
    新しいタブで開く。
  - 「自動送信ログ」画面上部の「自動入力」ボタン(ブックマークバーへドラッグして
    登録する、APIキー埋め込み済みのjavascript:リンク)を、開いた新しいタブ上で
    クリックすると、`GET /api/tenant/autofill/pending`(10分でTTL失効。CORS対応
    のため`do_OPTIONS`の`Access-Control-Allow-Headers`に`Authorization`を追加)
    から取得した値で、フォーム項目をform_navigator.pyの`_FIELD_HINTS`相当の
    簡易ヒューリスティック(JS移植)で自動入力する。**送信ボタンは押さない**
    (人が最後に内容を確認して押す。取り消せない操作までは自動化しない)。
  - jsdomで実際のフォームHTMLに対してブックマークレット本体を実行し、
    正しく入力できることを確認済み(`/tmp/jsdom_test/check_autofill.js`。
    ただしjsdomにはレイアウトエンジンが無く`offsetParent`が常にnullになるため、
    可視判定のみテスト用にスタブしている。実ブラウザでの動作は未検証)。
  - **今夜やらなかったこと**: 「会社情報」「その他」領域(リスト取得・CSV検索等)の
    MIKOMERU比較・改修は未着手。ブックマークレットは実ブラウザで一度も
    動作確認していない(jsdomでのロジック検証のみ)ため、実際に使う前に
    人の手で一度、本物の問い合わせフォームで試すこと。
- **送信元の姓・名・フリガナ・郵便番号(MIKOMERU相当の項目)**: `tenants`/
  `sender_templates`に`sender_last_name`/`sender_first_name`/
  `sender_last_name_kana`/`sender_first_name_kana`/`sender_postal_code`を
  追加(すべて任意項目)。「送信元テンプレート」画面に入力欄を追加した。
  あわせて2つの実バグを修正した:
  - 以前は姓欄・名欄の両方に会社名(`sender.name`)をそのまま複製していた
    (`form_navigator.py`の`fill_value = values.get(kind) or (values.get("name")
    if kind in ("last_name","first_name") else None)`という暗黙のフォールバック)。
    姓・名が別欄の問い合わせフォームで、名欄にも会社名が入ってしまう不自然な
    内容になっていた。フォールバックを削除し、呼び出し側(`senders.py`)が
    姓欄=会社名(未設定時)/名欄=空、と明示的に決めるようにした。
  - フリガナ欄には常に固定文字列`"アシベース"`が入っていた。今夜の実送信で
    テナントが送信者名を「東北三上機材株式会社」にカスタマイズしていたのに
    フリガナだけ「アシベース」のまま送っていた可能性がある(初回実送信時の
    バグ)。姓カナ・名カナが未設定ならフリガナ欄は空にするよう修正。
  - 郵便番号も新たに`values["postal_code"]`として渡すようにした
    (`_FIELD_HINTS["postal_code"]`自体は以前から検出対応していたが、
    値を渡していなかったため常に空欄で送信されていた。多くのフォームで
    郵便番号は必須項目のため、これが未確認成功(`success_not_confirmed`)の
    一因だった可能性がある)。
  - `senders.py test`に検証を追加(未設定/設定済みの両パターンで
    `FN.navigate_and_submit`へ渡る`values`の中身を直接確認)。

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
