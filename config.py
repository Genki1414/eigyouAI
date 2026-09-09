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

# api.py側のverify/staff・reset-passwordリンクと同じ環境変数(値はEIGYOUAI_DOMAIN
# <Caddyfileの実際の公開ドメイン>と揃える。本番は app.ashibase.jp)。
API_PUBLIC_URL = os.environ.get("API_PUBLIC_URL", "https://ashibase.jp")

# 特定電子メール法上必須の配信停止URL。2026-09-09発覚: 専用のOPTOUT_URL環境変数を
# 想定していたコード(.env.example)はあったが、どのPythonコードからも参照されて
# おらず、senders.py/api.pyの複数箇所で"https://ashibase.jp/optout"という
# プレースホルダのドメイン・パスがハードコードされたまま使われていた
# (ashibase.jpは実際の公開ドメインapp.ashibase.jpの取り違え、/optoutは実際の
# エンドポイントである/api/optoutの取り違えで、クリックしても機能しない状態
# だった)。API_PUBLIC_URLから自動的に正しいパスを組み立てる形に修正した。
OPTOUT_URL = os.environ.get("OPTOUT_URL", f"{API_PUBLIC_URL}/api/optout")

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
    # 運輸・物流
    "一般貨物輸送サービス": "ippankamotsuyusousaabisu",
    "空運・航空物流": "kuuunkoukuubutsuryuu",
    "バス・公共交通機関": "basukoukyoukoutsuukikan",
    "タクシー・ハイヤー": "takushiihaiyaa",
    "冷凍・冷蔵輸送": "reitoureizouyusou",
    "鉄道・陸運": "tetsudourikuun",
    "海運": "kaiun",
    "倉庫管理・運営": "soukokanriunei",
    "引っ越し・移転サービス": "hikkoshiitensaabisu",
    "重機・機械輸送": "juukikikaiyusou",
    "重量物輸送": "juuryoubutsuyusou",
    "港湾・海上輸送支援": "kouwankaijouyusoushien",
    "その他運輸・物流": "sonohokaunyubutsuryuu",
    # 人材系
    "業務請負サービス": "gyoumuukeoisaabisu",
    "製造業・技術職派遣": "seizougyougijutsushokuhaken",
    "サービス業人材派遣": "saabisugyoujinzaihaken",
    "事務処理アウトソーシング": "jimushoriautosooshingu",
    "医療・福祉人材派遣": "iryoufukushijinzaihaken",
    "物流・倉庫関連人材派遣": "butsuryuusoukokanrenjinzaihaken",
    "事務員・作業員派遣": "jimuinsagyouinhaken",
    "人材紹介": "jinzaishoukai",
    "コールセンター運営": "koorusentaaunei",
    "企業研修・トレーニング": "kigyoukenshuutoreeningu",
    "その他人材全般": "sonotaninzaizenpan",
    "セミナー・個別指導サービス": "seminaakobetsushidousaabisu",
    # 医療・福祉・バイオ
    "調剤薬局・薬局事業": "chouzaiyakkyokuyakkyokujigyou",
    "製薬": "seiyaku",
    "医療機器・実験機器製造": "iryoukikijikkenkiutsuwaseizou",
    "その他医療・療養施設運営": "sonohokairyouryouyoushisetsuunei",
    "高齢者向け福祉": "koureishamukefukushi",
    "介護用品・在宅医療機器": "kaigoyouhinzaitakuiryoukiki",
    "障がい者福祉事業": "shougaimonofukushijigyou",
    "病院": "byouin",
    "医療法人": "iryouhounin",
    "高齢者向け住宅施設": "koureishamukejuutakushisetsu",
    "児童福祉・保育関連": "jidoufukushihoikukanren",
    "介護・福祉": "kaigofukushi",
    "クリニック・医院・診療所": "kurinikkuiinshinryoujo",
    "バイオテクノロジー・先端医療": "baiotekunorojiisentaniryou",
    "動物病院": "doubutsubyouin",
    "歯医者": "haisha",
    "その他医療・福祉サービス": "sonohokairyoufukushisaabisu",
    # 広告
    "広告企画代理店": "koukokukikakudairiten",
    "オンライン広告代理": "onrainkoukokudairi",
    "その他広告": "sonohokakoukoku",
    "展示会・プロモーションイベント": "tenjikaipuromooshonibento",
    # 商社関連
    "総合商社": "sougoushousha",
    "化学品・医薬品商社": "kagakuhiniyakuhinshousha",
    "食品関連専門商社": "shokuhinkanrensenmonshousha",
    "鉄鋼・金属商社": "tekkoukinzokushousha",
    "医療機器・器具商社": "iryoukikikigushousha",
    "工業用機械専門商社": "kougyouyoukikaisenmonshousha",
    "農産物食品商社": "nousanbutsushokuhinshousha",
    "電子部品商社": "denshibuhinshousha",
    "日用品・化粧品商社": "nichiyouhinkeshouhinshousha",
    "紙・パルプ専門商社": "kamiparupusenmonshousha",
    "機械専門商社": "kikaisenmonshousha",
    "建材専門商社": "kenzaisenmonshousha",
    "食肉・卵関連専門商社": "shokunikutamagokanrensenmonshousha",
    "水産物食品専門商社": "suisanbutsushokuhinsenmonshousha",
    "農林水産用機械商社": "nourinsuisanyoukikaishousha",
    "繊維・アパレル商社": "seniaparerushousha",
    "雑貨・日用品専門商社": "zakkanichiyouhinsenmonshousha",
    "その他専門商社": "sonohokasenmonshousha",
    # 不動産
    "マンション・アパート賃貸": "manshonapaatochintai",
    "その他不動産": "sonohokafudousan",
    "総合不動産（デベロッパー）": "sougoufudousandeberoppaa",
    "戸建賃貸": "kodatechintai",
    "戸建売買": "kodatebaibai",
    "事業用物件・テナントビル賃貸": "jigyouyoubukkentenantobiruchintai",
    "マンション・アパート売買": "manshonapaatobaibai",
    "駐車場運営": "chuushajouunei",
    "マンション・ビル管理": "manshonbirukanri",
    "レンタルスペース提供": "rentarusupeesuteikyou",
    "事業用物件・テナントビル売買": "jigyouyoubukkentenantobirubaibai",
    "土地売買・賃貸": "tochibaibaichintai",
    "その他不動産管理": "sonohokafudousankanri",
    # ファッション・美容
    "スキンケア": "sukinkea",
    "コスメティック製造": "kosumeteikkuseizou",
    "レディースアパレル": "rediisuapareru",
    "時計": "tokei",
    "インナーウェア・靴下製造": "innaaueakutsushitaseizou",
    "エステ・リラクゼーション": "esuterirakuzeeshon",
    "バッグ・アパレル雑貨": "bagguapareruzakka",
    "ジュエリー": "juerii2",
    "繊維・織布": "senishokufu",
    "シューズ": "shuuzu",
    "美容サロン・ヘアケア": "biyousaronheakea",
    "制服・ワークウェア製造": "seifukuwaakuueaseizou",
    "その他美容": "sonohokabiyou",
    "子供服": "kodomofuku",
    "メンズアパレル": "menzuapareru",
    "その他アパレル": "sonohokaapareru",
    # エンタメ・レジャー
    "ゴルフ場運営": "gorufubaunei",
    "映像・CM制作": "eizoucmseisaku",
    "パチンコ・アミューズメント": "pachinkoamyuuzumento",
    "旅館・ホテル・宿泊施設": "ryokanhoterushukuhakushisetsu",
    "旅行関連": "ryokoukanren",
    "ペット・動物関連サービス": "pettodoubutsukanrensaabisu",
    "マルチメディア・楽器": "maruchimediagakki",
    "イベント企画・運営": "ibentokikakuunei",
    "スポーツビジネス関連": "supootsubijinesukanren",
    "フィットネス・ジム": "fittonesujimu",
    "海外旅行・留学支援": "kaigairyokouryuugakushien",
    "映画・アニメ": "eigaanime",
    "芸能プロダクション": "geinoupurodakushon",
    "タレント・キャラクターグッズ": "tarentokyarakutaaguzzu",
    "その他エンタメ・レジャー": "sonohokaentamerejaa",
    # コンサル
    "ITコンサルティング": "itkonsaruteingu",
    "財務コンサルティング": "zaimukonsaruteingu",
    "医療関連コンサルティング": "iryoukanrenkonsaruteingu",
    "総合コンサルティング": "sougoukonsaruteingu",
    "プロモーション戦略コンサルティング": "puromooshonsenryakukonsaruteingu",
    "経営コンサルティング": "keieikonsaruteingu",
    "製造業コンサルティング": "seizougyoukonsaruteingu",
    "不動産コンサルティング": "fudousankonsaruteingu",
    "組織・人事戦略コンサルティング": "soshikijinjisenryakukonsaruteingu",
    "デジタルマーケティング": "dejitarumaaketeingu",
    "土木・建築コンサルティング": "dobokukenchikukonsaruteingu",
    "飲食関連コンサルティング": "inshokukanrenkonsaruteingu",
    "コスト削減コンサルティング": "kosutosakugenkonsaruteingu",
    "資産運用アドバイザー": "shisanunyouadobaizaa",
    "広告運用コンサルティング": "koukokuunyoukonsaruteingu",
    "スタートアップ支援": "sutaatoappushien",
    "その他コンサルティング": "sonohokakonsaruteingu",
    # 金融
    "保険サービス": "hokensaabisu",
    "投資・資産運用": "toushishisanunyou",
    "銀行・信用金庫・信用組合": "ginkoushinyoukinkoshinyoukumiai",
    "証券": "shouken",
    "クレジット・信販・決済サービス": "kurejittoshinpankessaisaabisu",
    "保険代理店": "hokendairimise",
    "貸金・ローンサービス": "kashikinroonsaabisu",
    "事業者向け金融サービス": "jigyoushamukekinyuusaabisu",
    "ネット証券": "nettoshouken",
    "その他金融関連サービス": "sonohokakinyuukanrensaabisu",
    # IT
    "サイバーセキュリティサービス": "saibaasekyuriteisaabisu",
    "ソフトウェア専門商社": "sofutoueasenmonshousha",
    "ITインフラ構築・運用": "itinfurakouchikuunyou",
    "受託開発・SI": "jutakukaihatsusi",
    "ソフトウェア開発": "sofutoueakaihatsu",
    "Webデザイン・制作": "webdezainseisaku",
    "Webサービス・アプリ運営": "websaabisuapuriunei",
    "デジタルコンテンツ制作・運用": "dejitarukontentsuseisakuunyou",
    "クラウド・フィンテック": "kuraudofintekku",
    "その他IT": "sonohokait",
    # 教育・スクール関連
    "学習塾・予備校": "gakushuujukuyobikou",
    "スクール・習い事": "sukuurunaraigoto",
    "幼稚園・保育園": "youchienhoikuen",
    "大学": "daigaku",
    "資格取得・通信教育": "shikakushutokutsuushinkyouiku",
    "IT教育関連": "itkyouikukanren",
    "教材製作・販売": "kyouzaiseisakuhanbai",
    "語学学習スクール": "gogakugakushuusukuuru",
    "小学校・中学校・高校": "shougakkouchuugakkoukoukou",
    "専門学校": "senmongakkou",
    "その他学校・教育機関": "sonohokagakkoukyouikukikan",
    # 化学
    "化学品・化学薬品製造": "kagakuhinkagakuyakuhinseizou",
    "塗料製造": "toryouseizou",
    "樹脂製品製造": "jushiseihinseizou",
    "樹脂製部品製造": "jushiseibuhinseizou",
    "肥料・農薬・園芸用品製造": "hiryounouyakuengeiyouhinseizou",
    "接着剤・粘着テープ製造": "setchakuzainenchakuteepuseizou",
    "その他化学": "sonohokakagaku",
    # 公共サービス
    "官公庁": "kankouchou",
    "裁判所・検察庁": "saibanshokensatsuchou",
    # 石炭・鉱石採掘
    "資源メジャー": "shigenmejaa",
    "貴金属採掘・精錬": "kikinzokusaikutsuseiren",
    "採掘・採石関連": "saikutsusaisekikanren",
    "石炭・石灰石開発、販売": "sekitansekkaiishikaihatsuhanbai",
    "石炭開発サービス": "sekitankaihatsusaabisu",
    "その他金属採掘": "sonohokakinzokusaikutsu",
    # エネルギー
    "ガス・燃料関連": "gasunenryoukanren",
    "電力供給": "denryokukyoukyuu",
    "再生可能エネルギー": "saiseikanouenerugii",
    "その他エネルギー": "sonohokaenerugii",
    # ゲーム
    "ソーシャルゲーム": "soosharugeemu",
    "ゲームソフト開発": "geemusofutokaihatsu",
    "アニメーションデザイン": "animeeshondezain",
    "その他ゲーム関連サービス": "sonohokageemukanrensaabisu",
    # 専門サービス
    "専門事務所": "senmonjimusho",
    "翻訳・通訳": "honyakutsuuyaku",
    # 通信及び通信機器
    "通信回線提供": "tsuushinkaisenteikyou",
    "パソコン製造・販売・修理": "pasokonseizouhanbaishuuri",
    "携帯・通信回線販売代理店": "keitaitsuushinkaisenhanbaidairiten",
    "パソコン・スマホ周辺機器製造": "pasokonsumahoshuuhenkikiseizou",
    "電話機製造": "denwakiseizou",
    "スマホ・タブレット製造・修理": "sumahotaburettoseizoushuuri",
    "その他通信": "sonohokatsuushin",
    "その他通信機器": "sonohokatsuushinkiutsuwa",
    # メディア・出版関連
    "テレビ・ラジオ放送局": "terebirajiohousoukyoku",
    "書籍・雑誌出版": "shosekizasshishuppan",
    "新聞": "shinbun",
    "テレビ番組制作": "terebibangumiseisaku",
    "デジタル書籍出版": "dejitarushosekishuppan",
    "ラジオ番組制作": "rajiobangumiseisaku",
    "メディア全般": "mediazenpan",
    # その他サービス業界
    "セキュリティ・警備": "sekyuriteikeibi",
    "クリーニング・清掃サービス": "kuriininguseisousaabisu",
    "調査・検査・研究関連": "chousakensakenkyuukanren",
    "ビル・施設清掃": "birushisetsuseisou",
    "廃棄物収集・運搬サービス": "haikibutsushuushuuunpansaabisu",
    "廃棄物処分": "haikibutsushobun",
    "撮影サービス": "satsueisaabisu",
    "レンタル・リース": "rentaruriisu",
    "その他デザイン・クリエイティブ": "sonohokadezainkurieiteibu",
    "生活関連レンタル・リース": "seikatsukanrenrentaruriisu",
    "リサイクル・リユース": "risaikururiyuusu",
    "ブライダル": "buraidaru",
    "葬儀・葬祭関連": "sougisousaikanren",
    "その他団体業界": "sonotadantaigyoukai",
    "保険組合": "hokenkumiai",
    "その他清掃": "sonohokaseisou",
    "オフィス機器レンタル・リース": "ofisukikirentaruriisu",
    "遺品整理サービス": "ihinseirisaabisu",
    "その他サービス": "sonohokasaabisu",
    # その他業界
    "組合・団体・連合会・協会": "kumiaidantairengoukaikyoukai",
    "NPO": "npo",
    "宗教法人": "shuukyouhoujin",
}

# TARGET_TRADESの各コードが、mikomeruの「業種で絞り込む」画面上でどのグループ
# (見出し)に属するかの対応。list_builder.htmlの業種チップをmikomeruと同じ
# グループ単位でクリック開閉できるようにするために追加した(2026-08-29、
# ユーザー指示)。/api/tenant/trades のレスポンスにもこのグループ名を含める
# (api.py h_tenant_trades_get)。未登録のコードは「その他」扱いにする
# (api.py側でTARGET_TRADE_GROUPS.get(code, "その他")として参照する)。
TARGET_TRADE_GROUPS = {
    # 建設・工事
    "tobi": "建設・工事",
    "tosou": "建設・工事",
    "kaitai": "建設・工事",
    "denki": "建設・工事",
    "zouen": "建設・工事",
    "kucho": "建設・工事",
    "doboku": "建設・工事",
    "eiseisetsubikouj": "建設・工事",
    "purantosetsubiko": "建設・工事",
    "juutaku": "建設・工事",
    "birukensetsu": "建設・工事",
    "tsuushinsetsubik": "建設・工事",
    "kenchikusenmonko": "建設・工事",
    "koutsuukanrenkou": "建設・工事",
    "shougyoushisetsu": "建設・工事",
    "kenchikusekkei": "建設・工事",
    "sougoudobokukouj": "建設・工事",
    "yougyoukeikenzai": "建設・工事",
    "chuumonjuutakuke": "建設・工事",
    "juutakurifoomu": "建設・工事",
    "mokuzai": "建設・工事",
    "kenzoubutsukench": "建設・工事",
    "sougoukensetsu": "建設・工事",
    "kinzokukeikenzai": "建設・工事",
    "jigyouyourifoomu": "建設・工事",
    "jushikeikenzaise": "建設・工事",
    "bunjoukatajuutak": "建設・工事",
    "interiadezain": "建設・工事",
    "rifoomu": "建設・工事",
    "taiyoukoupanerus": "建設・工事",
    "kasen": "建設・工事",
    "manshonkenchiku": "建設・工事",
    "nenryoutankukouj": "建設・工事",
    # 自動車・乗り物
    "jidoushabuhin": "自動車・乗り物",
    "jidoushaseizou": "自動車・乗り物",
    "gomuseihin": "自動車・乗り物",
    "jidoushaseibi": "自動車・乗り物",
    "nirinsha": "自動車・乗り物",
    "rentakaa": "自動車・乗り物",
    "jidoushakanrensa": "自動車・乗り物",
    "uchuukaihatsu": "自動車・乗り物",
    "sonohokanorimono": "自動車・乗り物",
    # 機械関連サービス
    "kikairentaru": "機械関連サービス",
    "purantoenjiniari": "機械関連サービス",
    "kikaishuuri": "機械関連サービス",
    "kikaisekkei": "機械関連サービス",
    "sonohokakikaikan": "機械関連サービス",
    # 電気製品
    "kadenseihinseizo": "電気製品",
    "onkyou": "電気製品",
    "shoumeikiguseizo": "電気製品",
    "sonohokadenkisei": "電気製品",
    # 機械製造
    "denshibuhinseizo": "機械製造",
    "shikenkiseizou": "機械製造",
    "kouguseizou": "機械製造",
    "insatsukikaiseiz": "機械製造",
    "sangyouyourobott": "機械製造",
    "kensetsukikaisei": "機械製造",
    "handoutai": "機械製造",
    "kousakukikaiseiz": "機械製造",
    "kuuchouki": "機械製造",
    "kanagataseizou": "機械製造",
    "sensaa": "機械製造",
    "seimitsukikiseiz": "機械製造",
    "hatsuden": "機械製造",
    "nougyou": "機械製造",
    "douryokusouchise": "機械製造",
    "amyuuzumentokiki": "機械製造",
    "mizushorikikaise": "機械製造",
    "erebeetaa": "機械製造",
    "jidouhanbaiki": "機械製造",
    "shokuhinkakoukik": "機械製造",
    "chuuboukikikanre": "機械製造",
    "koutsuukikiseizo": "機械製造",
    "ponpuseizou": "機械製造",
    "kagakukikaiseizo": "機械製造",
    "yousetsukikaisei": "機械製造",
    "purasuchikkuseik": "機械製造",
    "kougakukiki": "機械製造",
    "hikinzokukakouki": "機械製造",
    "boiraaseizou": "機械製造",
    "sonohokakikaisei": "機械製造",
    # 製造
    "kinzokuseihinsei": "製造",
    "tekkouseizou": "製造",
    "bousai": "製造",
    "kinzokubuhinseiz": "製造",
    "densen": "製造",
    "housoushizaiseiz": "製造",
    "garasuseihinseiz": "製造",
    "seniseizou": "製造",
    "hitetsukinzokuse": "製造",
    "kinzokukakouukeo": "製造",
    "denchiseihinseiz": "製造",
    "seishi": "製造",
    "paipu": "製造",
    "sagyoukanrenyouh": "製造",
    "purasuchikkuhous": "製造",
    "sutenresuseihins": "製造",
    "hikakuseihinseiz": "製造",
    "sonohokaseihinse": "製造",
    # 食品
    "kenkoushokuhinse": "食品",
    "sake": "食品",
    "inryouseizou": "食品",
    "kanzume": "食品",
    "suisanseizou": "食品",
    "shokunikuseizou": "食品",
    "wagashiseizou": "食品",
    "nougyoukanren": "食品",
    "beihan": "食品",
    "choumiryouseizou": "食品",
    "kashiseizouzenpa": "食品",
    "koohiiseizou": "食品",
    "yougashiseizou": "食品",
    "panseizou": "食品",
    "nyuuseihin": "食品",
    "menruiseizou": "食品",
    "seifun": "食品",
    "tsukemono": "食品",
    "sonohokashokuhin": "食品",
    # 生活用品
    "nichiyouhin": "生活用品",
    "ofisuyouhin": "生活用品",
    "tabakoseizou": "生活用品",
    "megane": "生活用品",
    "supootsuyouhinse": "生活用品",
    "kaguseizou": "生活用品",
    "senmenyouhinseih": "生活用品",
    "gangu": "生活用品",
    "gifuto": "生活用品",
    "nyuuyoujiyouhins": "生活用品",
    "zakka": "生活用品",
    "bunbougu": "生活用品",
    "tenpokagu": "生活用品",
    "butsugu": "生活用品",
    "bijutsuhin": "生活用品",
    "yunyuuzakkahanba": "生活用品",
    "sonotashoukatsuy": "生活用品",
    # 外食
    "washoku": "外食",
    "sushi": "外食",
    "deribarii": "外食",
    "fasutofuudo": "外食",
    "izakaya": "外食",
    "kafe": "外食",
    "kyuushoku": "外食",
    "famiriiresutoran": "外食",
    "youshoku": "外食",
    "menruimise": "外食",
    "nikuryourisenmon": "外食",
    "ajian": "外食",
    "sonohokagaishoku": "外食",
    # 小売
    "jishakataonrains": "小売",
    "iyakuhinhanbai": "小売",
    "suupaamaaketto": "小売",
    "aparerushoppu": "小売",
    "kouritenho": "小売",
    "gasorinsutando": "小売",
    "furuhon": "小売",
    "shokuhinkanren": "小売",
    "chuukoshahanbai": "小売",
    "ekomaasu": "小売",
    "jidoushabuhin2": "小売",
    "jidousha": "小売",
    "keshouhinhanbai": "小売",
    "shoseki": "小売",
    "kagu": "小売",
    "supootsuyouhinha": "小売",
    "sagyoukanrenyouh2": "小売",
    "shinshahanbai": "小売",
    "furawaashoppu": "小売",
    "megane2": "小売",
    "nyuuseihintakuha": "小売",
    "pasokon": "小売",
    "juerii": "小売",
    "biyouguzzuhanbai": "小売",
    "konbini": "小売",
    "hyakkaten": "小売",
    "kodomofukukanren": "小売",
    "sonohokakouri": "小売",
    # 運輸・物流
    "ippankamotsuyusousaabisu": "運輸・物流",
    "kuuunkoukuubutsuryuu": "運輸・物流",
    "basukoukyoukoutsuukikan": "運輸・物流",
    "takushiihaiyaa": "運輸・物流",
    "reitoureizouyusou": "運輸・物流",
    "tetsudourikuun": "運輸・物流",
    "kaiun": "運輸・物流",
    "soukokanriunei": "運輸・物流",
    "hikkoshiitensaabisu": "運輸・物流",
    "juukikikaiyusou": "運輸・物流",
    "juuryoubutsuyusou": "運輸・物流",
    "kouwankaijouyusoushien": "運輸・物流",
    "sonohokaunyubutsuryuu": "運輸・物流",
    # 人材系
    "gyoumuukeoisaabisu": "人材系",
    "seizougyougijutsushokuhaken": "人材系",
    "saabisugyoujinzaihaken": "人材系",
    "jimushoriautosooshingu": "人材系",
    "iryoufukushijinzaihaken": "人材系",
    "butsuryuusoukokanrenjinzaihaken": "人材系",
    "jimuinsagyouinhaken": "人材系",
    "jinzaishoukai": "人材系",
    "koorusentaaunei": "人材系",
    "kigyoukenshuutoreeningu": "人材系",
    "sonotaninzaizenpan": "人材系",
    "seminaakobetsushidousaabisu": "人材系",
    # 医療・福祉・バイオ
    "chouzaiyakkyokuyakkyokujigyou": "医療・福祉・バイオ",
    "seiyaku": "医療・福祉・バイオ",
    "iryoukikijikkenkiutsuwaseizou": "医療・福祉・バイオ",
    "sonohokairyouryouyoushisetsuunei": "医療・福祉・バイオ",
    "koureishamukefukushi": "医療・福祉・バイオ",
    "kaigoyouhinzaitakuiryoukiki": "医療・福祉・バイオ",
    "shougaimonofukushijigyou": "医療・福祉・バイオ",
    "byouin": "医療・福祉・バイオ",
    "iryouhounin": "医療・福祉・バイオ",
    "koureishamukejuutakushisetsu": "医療・福祉・バイオ",
    "jidoufukushihoikukanren": "医療・福祉・バイオ",
    "kaigofukushi": "医療・福祉・バイオ",
    "kurinikkuiinshinryoujo": "医療・福祉・バイオ",
    "baiotekunorojiisentaniryou": "医療・福祉・バイオ",
    "doubutsubyouin": "医療・福祉・バイオ",
    "haisha": "医療・福祉・バイオ",
    "sonohokairyoufukushisaabisu": "医療・福祉・バイオ",
    # 広告
    "koukokukikakudairiten": "広告",
    "onrainkoukokudairi": "広告",
    "sonohokakoukoku": "広告",
    "tenjikaipuromooshonibento": "広告",
    # 商社関連
    "sougoushousha": "商社関連",
    "kagakuhiniyakuhinshousha": "商社関連",
    "shokuhinkanrensenmonshousha": "商社関連",
    "tekkoukinzokushousha": "商社関連",
    "iryoukikikigushousha": "商社関連",
    "kougyouyoukikaisenmonshousha": "商社関連",
    "nousanbutsushokuhinshousha": "商社関連",
    "denshibuhinshousha": "商社関連",
    "nichiyouhinkeshouhinshousha": "商社関連",
    "kamiparupusenmonshousha": "商社関連",
    "kikaisenmonshousha": "商社関連",
    "kenzaisenmonshousha": "商社関連",
    "shokunikutamagokanrensenmonshousha": "商社関連",
    "suisanbutsushokuhinsenmonshousha": "商社関連",
    "nourinsuisanyoukikaishousha": "商社関連",
    "seniaparerushousha": "商社関連",
    "zakkanichiyouhinsenmonshousha": "商社関連",
    "sonohokasenmonshousha": "商社関連",
    # 不動産
    "manshonapaatochintai": "不動産",
    "sonohokafudousan": "不動産",
    "sougoufudousandeberoppaa": "不動産",
    "kodatechintai": "不動産",
    "kodatebaibai": "不動産",
    "jigyouyoubukkentenantobiruchintai": "不動産",
    "manshonapaatobaibai": "不動産",
    "chuushajouunei": "不動産",
    "manshonbirukanri": "不動産",
    "rentarusupeesuteikyou": "不動産",
    "jigyouyoubukkentenantobirubaibai": "不動産",
    "tochibaibaichintai": "不動産",
    "sonohokafudousankanri": "不動産",
    # ファッション・美容
    "sukinkea": "ファッション・美容",
    "kosumeteikkuseizou": "ファッション・美容",
    "rediisuapareru": "ファッション・美容",
    "tokei": "ファッション・美容",
    "innaaueakutsushitaseizou": "ファッション・美容",
    "esuterirakuzeeshon": "ファッション・美容",
    "bagguapareruzakka": "ファッション・美容",
    "juerii2": "ファッション・美容",
    "senishokufu": "ファッション・美容",
    "shuuzu": "ファッション・美容",
    "biyousaronheakea": "ファッション・美容",
    "seifukuwaakuueaseizou": "ファッション・美容",
    "sonohokabiyou": "ファッション・美容",
    "kodomofuku": "ファッション・美容",
    "menzuapareru": "ファッション・美容",
    "sonohokaapareru": "ファッション・美容",
    # エンタメ・レジャー
    "gorufubaunei": "エンタメ・レジャー",
    "eizoucmseisaku": "エンタメ・レジャー",
    "pachinkoamyuuzumento": "エンタメ・レジャー",
    "ryokanhoterushukuhakushisetsu": "エンタメ・レジャー",
    "ryokoukanren": "エンタメ・レジャー",
    "pettodoubutsukanrensaabisu": "エンタメ・レジャー",
    "maruchimediagakki": "エンタメ・レジャー",
    "ibentokikakuunei": "エンタメ・レジャー",
    "supootsubijinesukanren": "エンタメ・レジャー",
    "fittonesujimu": "エンタメ・レジャー",
    "kaigairyokouryuugakushien": "エンタメ・レジャー",
    "eigaanime": "エンタメ・レジャー",
    "geinoupurodakushon": "エンタメ・レジャー",
    "tarentokyarakutaaguzzu": "エンタメ・レジャー",
    "sonohokaentamerejaa": "エンタメ・レジャー",
    # コンサル
    "itkonsaruteingu": "コンサル",
    "zaimukonsaruteingu": "コンサル",
    "iryoukanrenkonsaruteingu": "コンサル",
    "sougoukonsaruteingu": "コンサル",
    "puromooshonsenryakukonsaruteingu": "コンサル",
    "keieikonsaruteingu": "コンサル",
    "seizougyoukonsaruteingu": "コンサル",
    "fudousankonsaruteingu": "コンサル",
    "soshikijinjisenryakukonsaruteingu": "コンサル",
    "dejitarumaaketeingu": "コンサル",
    "dobokukenchikukonsaruteingu": "コンサル",
    "inshokukanrenkonsaruteingu": "コンサル",
    "kosutosakugenkonsaruteingu": "コンサル",
    "shisanunyouadobaizaa": "コンサル",
    "koukokuunyoukonsaruteingu": "コンサル",
    "sutaatoappushien": "コンサル",
    "sonohokakonsaruteingu": "コンサル",
    # 金融
    "hokensaabisu": "金融",
    "toushishisanunyou": "金融",
    "ginkoushinyoukinkoshinyoukumiai": "金融",
    "shouken": "金融",
    "kurejittoshinpankessaisaabisu": "金融",
    "hokendairimise": "金融",
    "kashikinroonsaabisu": "金融",
    "jigyoushamukekinyuusaabisu": "金融",
    "nettoshouken": "金融",
    "sonohokakinyuukanrensaabisu": "金融",
    # IT
    "saibaasekyuriteisaabisu": "IT",
    "sofutoueasenmonshousha": "IT",
    "itinfurakouchikuunyou": "IT",
    "jutakukaihatsusi": "IT",
    "sofutoueakaihatsu": "IT",
    "webdezainseisaku": "IT",
    "websaabisuapuriunei": "IT",
    "dejitarukontentsuseisakuunyou": "IT",
    "kuraudofintekku": "IT",
    "sonohokait": "IT",
    # 教育・スクール関連
    "gakushuujukuyobikou": "教育・スクール関連",
    "sukuurunaraigoto": "教育・スクール関連",
    "youchienhoikuen": "教育・スクール関連",
    "daigaku": "教育・スクール関連",
    "shikakushutokutsuushinkyouiku": "教育・スクール関連",
    "itkyouikukanren": "教育・スクール関連",
    "kyouzaiseisakuhanbai": "教育・スクール関連",
    "gogakugakushuusukuuru": "教育・スクール関連",
    "shougakkouchuugakkoukoukou": "教育・スクール関連",
    "senmongakkou": "教育・スクール関連",
    "sonohokagakkoukyouikukikan": "教育・スクール関連",
    # 化学
    "kagakuhinkagakuyakuhinseizou": "化学",
    "toryouseizou": "化学",
    "jushiseihinseizou": "化学",
    "jushiseibuhinseizou": "化学",
    "hiryounouyakuengeiyouhinseizou": "化学",
    "setchakuzainenchakuteepuseizou": "化学",
    "sonohokakagaku": "化学",
    # 公共サービス
    "kankouchou": "公共サービス",
    "saibanshokensatsuchou": "公共サービス",
    # 石炭・鉱石採掘
    "shigenmejaa": "石炭・鉱石採掘",
    "kikinzokusaikutsuseiren": "石炭・鉱石採掘",
    "saikutsusaisekikanren": "石炭・鉱石採掘",
    "sekitansekkaiishikaihatsuhanbai": "石炭・鉱石採掘",
    "sekitankaihatsusaabisu": "石炭・鉱石採掘",
    "sonohokakinzokusaikutsu": "石炭・鉱石採掘",
    # エネルギー
    "gasunenryoukanren": "エネルギー",
    "denryokukyoukyuu": "エネルギー",
    "saiseikanouenerugii": "エネルギー",
    "sonohokaenerugii": "エネルギー",
    # ゲーム
    "soosharugeemu": "ゲーム",
    "geemusofutokaihatsu": "ゲーム",
    "animeeshondezain": "ゲーム",
    "sonohokageemukanrensaabisu": "ゲーム",
    # 専門サービス
    "senmonjimusho": "専門サービス",
    "honyakutsuuyaku": "専門サービス",
    # 通信及び通信機器
    "tsuushinkaisenteikyou": "通信及び通信機器",
    "pasokonseizouhanbaishuuri": "通信及び通信機器",
    "keitaitsuushinkaisenhanbaidairiten": "通信及び通信機器",
    "pasokonsumahoshuuhenkikiseizou": "通信及び通信機器",
    "denwakiseizou": "通信及び通信機器",
    "sumahotaburettoseizoushuuri": "通信及び通信機器",
    "sonohokatsuushin": "通信及び通信機器",
    "sonohokatsuushinkiutsuwa": "通信及び通信機器",
    # メディア・出版関連
    "terebirajiohousoukyoku": "メディア・出版関連",
    "shosekizasshishuppan": "メディア・出版関連",
    "shinbun": "メディア・出版関連",
    "terebibangumiseisaku": "メディア・出版関連",
    "dejitarushosekishuppan": "メディア・出版関連",
    "rajiobangumiseisaku": "メディア・出版関連",
    "mediazenpan": "メディア・出版関連",
    # その他サービス業界
    "sekyuriteikeibi": "その他サービス業界",
    "kuriininguseisousaabisu": "その他サービス業界",
    "chousakensakenkyuukanren": "その他サービス業界",
    "birushisetsuseisou": "その他サービス業界",
    "haikibutsushuushuuunpansaabisu": "その他サービス業界",
    "haikibutsushobun": "その他サービス業界",
    "satsueisaabisu": "その他サービス業界",
    "rentaruriisu": "その他サービス業界",
    "sonohokadezainkurieiteibu": "その他サービス業界",
    "seikatsukanrenrentaruriisu": "その他サービス業界",
    "risaikururiyuusu": "その他サービス業界",
    "buraidaru": "その他サービス業界",
    "sougisousaikanren": "その他サービス業界",
    "sonotadantaigyoukai": "その他サービス業界",
    "hokenkumiai": "その他サービス業界",
    "sonohokaseisou": "その他サービス業界",
    "ofisukikirentaruriisu": "その他サービス業界",
    "ihinseirisaabisu": "その他サービス業界",
    "sonohokasaabisu": "その他サービス業界",
    # その他業界
    "kumiaidantairengoukaikyoukai": "その他業界",
    "npo": "その他業界",
    "shuukyouhoujin": "その他業界",
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
    "optout_url": OPTOUT_URL,
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
