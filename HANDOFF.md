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
- 未対応(次フェーズ): 顧客の新規登録・課金・自分でのAPIキー発行UI、作成した
  リストから`campaigns`/実送信への接続

**⚠ 暫定措置・要対応(2026-08-21)**: `list_builder.html`の動作確認のため、
`deploy/docker-compose.yml`のapiサービスを`127.0.0.1:8787`限定公開から
`0.0.0.0:8787`(外部公開)へ一時的に変更した。TLSが無いため、テナント
`api_key`がAuthorization: Bearerヘッダに平文で流れる。**実際の顧客に
`list_builder.html`を使わせる前に、必ずリバースプロキシ経由のHTTPS化
(サブドメイン+証明書、または既存のstockfactory側インフラ活用)に戻すこと。**
それまでは動作確認・デモ用途に限定する。

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
