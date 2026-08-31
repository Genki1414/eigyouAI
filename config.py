"""
config.py — 全設定の単一情報源
各スクリプトに散っていた閾値・単価・文言をここに集約する。
本番で調整するのはこのファイルだけ、という状態を保つこと。
"""
import os
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "out" / "companies.db"
OUT_DIR = BASE / "out"

# クリック計測(MIKOMERUの「URLアクセスの記録」相当)のリダイレクトリンクに使う
# 公開URL。本番では実際の公開ドメインを環境変数で上書きすること
# (api.py LP_URLと同じ考え方)。
TRACK_BASE_URL = os.environ.get("TRACK_BASE_URL", "https://ashibase.jp")

# ── 対象業種 ──────────────────────────────
# 建設業許可29業種(parsers/common.py TRADE_CODE_NAMES)のうち、ここに書いた
# キーワードが業種名に含まれるものだけを対象にする。キーワードは他の業種名と
# 誤って重ならない粒度で書くこと(例: 「電気工事」は22番「電気通信工事」には
# 一致しない。「電気」だけだと一致してしまうため不可)。
#
# 【mikomeruの業種分類を全件登録(2026-08-29、ユーザー指示)】
# 最初はAI入札連携(AInyusatsu)向けに電気・造園・空調の3つだけを追加したが、
# 「スクショした業種を全て追加」との指示で、mikomeruの「業種で絞り込む」画面に
# 出ている分類(建設・工事/自動車・乗り物/機械関連サービス/電気製品/機械製造/製造/
# 食品/生活用品/外食/小売の10グループ・176項目)をここに登録した。
# mikomeruの分類名の大半は建設業許可29業種(parsers/common.py TRADE_CODE_NAMES)には
# 存在しない語彙のため、国交省名簿からは拾えず、mikomeruの取込
# (ingest_mikomeru.py)経由でのみ値が入る。ここに登録するのは、コードを
# 「対象」として_ALLOWED_TRADESと/api/tenant/tradesの語彙に載せるため。
# コード名はカタカナ・漢字部分をローマ字化して機械的に付けたもの(可読性より
# 一意性を優先。対応表を書くのは本部で、コードそのものを人が読む場面は少ない)。
# なお「電気設備工事」「産業用電気設備工事」「とび・土工工事」「解体工事」
# 「造園・庭園設計工事」はそれぞれ既存の denki/tobi/kaitai/zouen に部分一致
# するため、重複コードを新設していない(zouenのみキーワードを1つ追加)。
# まだ「運輸・物流/人材系/医療・福祉・バイオ/広告/商社関連」以降のグループは
# 未登録(スクリーンショットが届き次第、追加すること)。
#
# 清掃・警備・情報処理・廃棄物処理・給食等、AI入札部が必要とする業種のうち
# mikomeruにも存在しないものは引き続き対象外。別データソースの選定が必要
# (eigyouAI HANDOFF.md「5. 連絡すべき判断」)。
TARGET_TRADES = {
    "とび": "tobi", "土工": "tobi", "塗装": "tosou", "解体": "kaitai",
    "電気工事": "denki", "造園工事": "zouen", "空調設備工事": "kucho",

    # 建設・工事
    "土木・インフラ工事": "doboku",
    "衛生設備工事": "eiseisetsubikouj",
    "プラント設備工事": "purantosetsubiko",
    "住宅・オフィス向け設備工事": "juutaku",
    "ビル建設": "birukensetsu",
    "通信設備工事": "tsuushinsetsubik",
    "建築専門工事": "kenchikusenmonko",
    "交通関連工事": "koutsuukanrenkou",
    "商業施設・公共施設建設": "shougyoushisetsu",
    "建築設計・施工管理": "kenchikusekkei",
    "総合土木工事": "sougoudobokukouj",
    "窯業系建材製造": "yougyoukeikenzai",
    "注文住宅建築": "chuumonjuutakuke",
    "住宅リフォーム・改修工事": "juutakurifoomu",
    "木材・建材製造": "mokuzai",
    "建造物建築・設計": "kenzoubutsukench",
    "総合建設・ゼネコン": "sougoukensetsu",
    "金属系建材製造": "kinzokukeikenzai",
    "事業用リフォーム": "jigyouyourifoomu",
    "樹脂系建材製造": "jushikeikenzaise",
    "分譲型住宅建築": "bunjoukatajuutak",
    "インテリアデザイン・空間設計": "interiadezain",
    "リフォーム": "rifoomu",
    "太陽光パネル設置": "taiyoukoupanerus",
    "河川・港湾工事": "kasen",
    "マンション建築・施工": "manshonkenchiku",
    "燃料タンク工事": "nenryoutankukouj",
    # 自動車・乗り物
    "自動車部品・カーアクセサリー製造": "jidoushabuhin",
    "自動車製造": "jidoushaseizou",
    "ゴム製品・タイヤ製造": "gomuseihin",
    "自動車整備・修理": "jidoushaseibi",
    "二輪車・バイク製造": "nirinsha",
    "レンタカー・リースサービス": "rentakaa",
    "自動車関連サービス": "jidoushakanrensa",
    "宇宙開発・宇宙産業": "uchuukaihatsu",
    "その他乗り物": "sonohokanorimono",
    # 機械関連サービス
    "機械レンタル・リース": "kikairentaru",
    "プラントエンジニアリング": "purantoenjiniari",
    "機械修理": "kikaishuuri",
    "機械設計": "kikaisekkei",
    "その他機械関連サービス": "sonohokakikaikan",
    # 電気製品
    "家電製品製造": "kadenseihinseizo",
    "音響・映像機器製造": "onkyou",
    "照明器具製造": "shoumeikiguseizo",
    "その他電気製品製造": "sonohokadenkisei",
    # 機械製造
    "電子部品製造": "denshibuhinseizo",
    "試験機製造": "shikenkiseizou",
    "工具製造": "kouguseizou",
    "印刷機械製造": "insatsukikaiseiz",
    "産業用ロボット・オートメーション機器製造": "sangyouyourobott",
    "建設機械製造": "kensetsukikaisei",
    "半導体・半導体関連装置製造": "handoutai",
    "工作機械製造": "kousakukikaiseiz",
    "空調機": "kuuchouki",
    "金型製造": "kanagataseizou",
    "センサー・計測機器製造": "sensaa",
    "精密機器製造": "seimitsukikiseiz",
    "発電・電力設備製造": "hatsuden",
    "農業・漁業機械製造": "nougyou",
    "動力装置製造": "douryokusouchise",
    "アミューズメント機器製造": "amyuuzumentokiki",
    "水処理機械製造": "mizushorikikaise",
    "エレベーター・エスカレーター製造": "erebeetaa",
    "自動販売機・自動サービス機": "jidouhanbaiki",
    "食品加工機械製造": "shokuhinkakoukik",
    "厨房機器関連製造": "chuuboukikikanre",
    "交通機器製造": "koutsuukikiseizo",
    "ポンプ製造": "ponpuseizou",
    "化学機械製造": "kagakukikaiseizo",
    "溶接機械製造": "yousetsukikaisei",
    "プラスチック成形機械製造": "purasuchikkuseik",
    "光学機器・レンズ製造": "kougakukiki",
    "非金属加工機械製造": "hikinzokukakouki",
    "ボイラー製造": "boiraaseizou",
    "その他機械製造": "sonohokakikaisei",
    # 製造
    "金属製品製造": "kinzokuseihinsei",
    "鉄鋼製造": "tekkouseizou",
    "防災・防犯機器": "bousai",
    "金属部品製造": "kinzokubuhinseiz",
    "電線・ケーブル製造": "densen",
    "包装資材製造": "housoushizaiseiz",
    "ガラス製品製造": "garasuseihinseiz",
    "繊維製造": "seniseizou",
    "非鉄金属製造": "hitetsukinzokuse",
    "金属加工請負": "kinzokukakouukeo",
    "電池製品製造": "denchiseihinseiz",
    "製紙・パルプ製造": "seishi",
    "パイプ・バルブ製造": "paipu",
    "作業関連用品製造": "sagyoukanrenyouh",
    "プラスチック包装資材製造": "purasuchikkuhous",
    "ステンレス製品製造": "sutenresuseihins",
    "皮革製品製造": "hikakuseihinseiz",
    "その他製品製造": "sonohokaseihinse",
    # 食品
    "健康食品製造": "kenkoushokuhinse",
    "酒・ワイン製造販売": "sake",
    "飲料製造": "inryouseizou",
    "缶詰・レトルト・冷凍食品製造": "kanzume",
    "水産製造・販売関連": "suisanseizou",
    "食肉製造・販売関連": "shokunikuseizou",
    "和菓子製造": "wagashiseizou",
    "農業関連": "nougyoukanren",
    "米飯・惣菜製造": "beihan",
    "調味料製造": "choumiryouseizou",
    "菓子製造全般": "kashiseizouzenpa",
    "コーヒー製造・販売": "koohiiseizou",
    "洋菓子製造": "yougashiseizou",
    "パン製造": "panseizou",
    "乳製品": "nyuuseihin",
    "麺類製造": "menruiseizou",
    "製粉・食用油製造": "seifun",
    "漬物・煮物・大豆製造": "tsukemono",
    "その他食品製造全般": "sonohokashokuhin",
    # 生活用品
    "日用品・雑貨製造販売": "nichiyouhin",
    "オフィス用品・オフィス家具": "ofisuyouhin",
    "タバコ製造": "tabakoseizou",
    "眼鏡・コンタクトレンズ製造": "megane",
    "スポーツ用品製造": "supootsuyouhinse",
    "家具製造": "kaguseizou",
    "洗面用品製品製造": "senmenyouhinseih",
    "玩具・ホビー製造": "gangu",
    "ギフト・お土産": "gifuto",
    "乳幼児用品製造": "nyuuyoujiyouhins",
    "雑貨・インテリア製造": "zakka",
    "文房具・オフィス用品製造": "bunbougu",
    "店舗家具・什器製造": "tenpokagu",
    "仏具・宗教用品": "butsugu",
    "美術品・工芸品": "bijutsuhin",
    "輸入雑貨販売": "yunyuuzakkahanba",
    "その他生活用品全般": "sonotashoukatsuy",
    # 外食
    "和食・家庭料理": "washoku",
    "寿司・海鮮料理関連": "sushi",
    "デリバリー・中食サービス": "deribarii",
    "ファストフード": "fasutofuudo",
    "居酒屋・バー": "izakaya",
    "カフェ・喫茶店": "kafe",
    "給食・食堂": "kyuushoku",
    "ファミリーレストラン": "famiriiresutoran",
    "洋食・西洋料理": "youshoku",
    "麺類店": "menruimise",
    "肉料理専門店": "nikuryourisenmon",
    "アジアン・エスニック料理": "ajian",
    "その他外食": "sonohokagaishoku",
    # 小売
    "自社型オンラインストア": "jishakataonrains",
    "医薬品販売": "iyakuhinhanbai",
    "スーパーマーケット": "suupaamaaketto",
    "アパレルショップ": "aparerushoppu",
    "小売店舗・施設": "kouritenho",
    "ガソリンスタンド": "gasorinsutando",
    "古本・リサイクルショップ": "furuhon",
    "食品関連": "shokuhinkanren",
    "中古車販売": "chuukoshahanbai",
    "eコマース・オンラインモール": "ekomaasu",
    "自動車部品・カーアクセサリー販売": "jidoushabuhin2",
    "自動車・自転車販売": "jidousha",
    "化粧品販売": "keshouhinhanbai",
    "書籍・マルチメディア販売": "shoseki",
    "家具・インテリア販売": "kagu",
    "スポーツ用品販売": "supootsuyouhinha",
    "作業関連用品販売": "sagyoukanrenyouh2",
    "新車販売": "shinshahanbai",
    "フラワーショップ・花屋": "furawaashoppu",
    "眼鏡・コンタクトレンズ販売": "megane2",
    "乳製品宅配": "nyuuseihintakuha",
    "パソコン・スマホ周辺機器販売": "pasokon",
    "ジュエリー・アクセサリーショップ": "juerii",
    "美容グッズ販売": "biyouguzzuhanbai",
    "コンビニ": "konbini",
    "百貨店": "hyakkaten",
    "子供服関連ショップ": "kodomofukukanren",
    "その他小売": "sonohokakouri",
}

# ── スコアリング V1 ────────────────────────
SCORE_THRESHOLDS = {"S": 75, "A": 60, "B": 45}   # 未満はC
EMP_SWEET_SPOT = (5, 50)                          # 従業員数のスイートスポット

# ── チャネル ──────────────────────────────
UNIT_COST_YEN = {"FAX": 12, "郵送DM": 95, "メール": 1, "SMS": 8, "架電": 180}
# 初回の推奨チャネル判定に使う（scoring.recommend_channel）
NEXT_CHANNEL = {
    "メール": ["SMS", "FAX"], "FAX": ["メール", "郵送DM"],
    "郵送DM": ["メール", "FAX"], "SMS": ["メール", "FAX"],
}

# ── シーケンス ────────────────────────────
SEQUENCE_DAYS = {1: 0, 2: 14, 3: 35}
MAX_STEP = 3
STEP_DECAY = {1: 1.0, 2: 0.62, 3: 0.38}

# ── 休眠 ─────────────────────────────────
COOLDOWN_DAYS = 180
CYCLE_OFFER = {
    1: ("無料ツール配布", "図面を送るだけで部材と数量が出るツールを無料開放"),
    2: ("積算代行", "図面を1枚お預かりして、こちらで積算した結果をお返しする"),
    3: ("地域事例訪問", "同じ地域の足場会社の導入結果を持って、10分だけ説明に伺う"),
}

# ── 経済性の前提 ──────────────────────────
LTV_MONTHS = 24            # LTV算定に使う継続月数（IMに明記される前提値）
PRICE_TIERS = [9800, 14800, 19800]

# ── AI ───────────────────────────────────
MODEL = "claude-sonnet-4-6"
ENRICH_SLEEP_SEC = 0.5
COMPOSE_SLEEP_SEC = 0.3

# ── コンプライアンス ──────────────────────
# 特定電子メール法: 送信者情報の明記と、受信拒否の意思表示を受ける窓口が必須。
SENDER_INFO = {
    "name": "ヒラケル",
    "address": "（本番: 登記上の住所を記載）",
    "email": "info@ashibase.jp",
    "optout_url": "https://ashibase.jp/optout",
}
# T44(2026-08-25): 1社あたりの生涯接触上限(旧MAX_LIFETIME_TOUCHES=6)・
# 最短再接触間隔(旧MIN_TOUCH_INTERVAL_DAYS=10日)は、100社×月4,000通規模へ
# 向けた再検討の結果、ユーザーの判断で撤廃した(db.can_contact()参照)。
# 削除の経緯・判断根拠はHANDOFF.md T44を参照。

# ── フォーム自動送信のペーシング ─────────────
# T29(2026-08-24)でテナント公平型に再設計。それまでは「全テナント合算で
# 1日100件」という単一の共有プールしかなく、契約社数が増えるほど1社あたりの
# 実質的な取り分が目減りする作りだった(極端な例: 100社が契約しても合計100件/日
# のまま=1社1件/日)。最低プランでも月4,000件(MIKOMERU最低ランクの水準)を
# 送れることを目標に、「グローバルなサーキットブレーカー」と「テナントごとの
# 公平な取り分」を分離した:
#   - FORM_MAX_PER_HOUR/DAY: 全テナント合算の上限。通常運用では到達しない
#     水準まで引き上げ、バグ・異常時の被害を止める最終防波堤として残す
#     (相手サイト群への負荷や送信元IPの評判悪化を、システム全体の暴走から守る)。
#   - FORM_MAX_PER_TENANT_PER_HOUR: テナント1社が短時間に固め打ちしないための
#     ペーシング(相手サイトへの礼儀・bot判定回避が目的。月間クォータの残りが
#     あっても、これを超える速さでは送らせない)。
#   - tenants.monthly_send_quota / daily_send_quota: プランに応じたテナント別の
#     クォータ(未設定=NULLの場合は下の_DEFAULT値を使う)。月間が契約プランの
#     実体、日次は月間クォータ内での使いすぎ防止のブレーキ。
FORM_MAX_PER_RUN = 50                        # 1回の実行(cron/API呼び出し1回)あたりの上限
FORM_MAX_PER_HOUR = 2000                     # 全テナント合算・直近1時間のサーキットブレーカー
FORM_MAX_PER_DAY = 20000                     # 全テナント合算・直近24時間のサーキットブレーカー
FORM_MAX_PER_TENANT_PER_HOUR = 50            # テナント1社・直近1時間のペーシング上限
FORM_MAX_PER_TENANT_PER_DAY_DEFAULT = 300    # tenants.daily_send_quota未設定時の既定値
FORM_MAX_PER_TENANT_PER_MONTH_DEFAULT = 4000  # tenants.monthly_send_quota未設定時の既定値
                                               # (=最低プランの想定送信数)

# senders.send_campaign()が1回の呼び出し内で同時に処理する件数(T41)。
# フォーム送信はPlaywrightでの実ブラウザ操作(1件あたり数秒〜十数秒)が
# ボトルネックのため、DBの読み書きではなくここが並列化の効果が出る箇所。
# 上げすぎると相手サイト群への同時アクセスが増え、bot判定やこのサーバーの
# メモリ(Chromiumプロセスを同時分だけ起動する)を圧迫するため小さめに抑える。
FORM_SEND_CONCURRENCY = 3

# ── 送信元IPの分散(プロキシ。T42) ─────────────
# T41でフォーム送信を並列化した結果、複数ワーカーが同じサーバーIPから
# 短時間に一斉アクセスする形になり、相手サイト側のWAF/bot判定に
# 引っかかりやすくなる懸念がある。form_navigator.py がPlaywrightで
# ブラウザを起動するたびに、このプールからプロキシを1つ選んで経由させる
# ことでアクセス元IPを分散できるようにする(実際のプロキシサービスの契約は
# インフラ側の判断のため、ここではコード側の受け皿のみ用意する)。
#
# FORM_PROXY_POOL環境変数にカンマ区切りで設定する:
#   FORM_PROXY_POOL="http://user1:pass1@proxy1.example.com:8080,http://proxy2.example.com:8080"
# 未設定(既定=空リスト)ならプロキシを使わず直接アクセスする(現状と同じ挙動、
# 後方互換)。
FORM_PROXY_POOL = [p.strip() for p in os.environ.get("FORM_PROXY_POOL", "").split(",") if p.strip()]

# ── 原価計測(1送信あたりのコスト把握。β版・概算値) ──
# 厳密なクラウド原価配賦ではなく、事業判断に使える推定値を出すのが目的。
# サーバー月額費用を実行時間で按分する(実行時間ベースの単純な比例配分)。
SERVER_MONTHLY_COST_YEN = 15000  # Hetzner等の月額実費。実績に合わせて更新する

def estimate_server_cost_yen(execution_seconds):
    """実行時間(秒)から、月額サーバー費用の按分としての推定原価を返す。"""
    if not execution_seconds:
        return 0.0
    seconds_per_month = 30 * 24 * 3600
    return SERVER_MONTHLY_COST_YEN * (execution_seconds / seconds_per_month)


# モデルごとのAPI単価(1トークンあたり円)。ハードコードで散らばらせず、ここ1箇所を
# 更新すれば全体に反映される構造にする。現状フォーム送信はAIを使っていないため
# 実際の呼び出し箇所は無いが、将来compose.py等をここに接続する前提で用意しておく。
AI_PRICING_YEN_PER_TOKEN = {
    # "claude-sonnet-5": {"input": 0.0045, "output": 0.0225},  # 例: $3/$15 per 1M tokens換算
}

def estimate_ai_cost_yen(model, tokens_input, tokens_output):
    price = AI_PRICING_YEN_PER_TOKEN.get(model)
    if not price or not (tokens_input or tokens_output):
        return 0.0
    return tokens_input * price["input"] + tokens_output * price["output"]


# ── バックアップ(backup.py。T36/T37) ──────────
BACKUP_DIR = OUT_DIR / "backups"
BACKUP_RETENTION_DAYS = 14   # これより古いバックアップファイルは自動削除する
# monitor.pyがこの時間を超えてバックアップが成功していないことを検知したらアラートする
# (毎日1回の実行前提で、1回分の遅延は許容しつつ2日連続の失敗は見逃さない設定)
BACKUP_STALE_HOURS = 30
# オフサイト複製(BACKUP_OFFSITE_TARGET設定時のみ有効)についても同様の考え方。
# ローカルより長めに取っているのは、rsync先が一時的に落ちていても2回失敗する
# までは静観したいため(ローカルのバックアップ自体は既に安全に取れているので、
# オフサイト側はローカルほど緊急性が高くない)。
BACKUP_OFFSITE_STALE_HOURS = 54
