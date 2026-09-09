"""
ingest_mikomeru.py — mikomeru保存済みリスト(CSV)の取込
既存companies.dbとは別データソース(法人番号ベースの業種横断ディレクトリ)。
国交省名簿は config.TARGET_TRADES の許可業者のみだが、mikomeruは業種を問わない
一般的な業者ディレクトリで、ホームページ/問い合わせページ/フォーム有無が
ほぼ全件揃っている(=Webフォーム経由の営業チャネルに使える一次情報)。

【2026-09-09追記: 建設業限定ではなく全業種を扱うディレクトリだった】
T60時点では「mikomeruも建設業者ディレクトリなので警備・情報処理・清掃・
廃棄物処理・給食等は収録が無い」としていたが誤りだった。T63でユーザーが
mikomeruの「業種で絞り込む」を最後まで確認したところ、建設・製造・小売以外にも
運輸/人材/医療/広告/商社/不動産/美容/エンタメ/コンサル/金融/IT/教育/化学/
公共サービス/鉱業/エネルギー/ゲーム/専門サービス/通信/メディア/その他サービス業界
(警備・清掃・廃棄物処理はここに実在した)/その他業界まで全394業種が確認でき、
TRADE_KEYWORDSに登録済み。給食は元々「外食」グループの「給食・食堂」で対応済み。

既存レコードとの重複はdb.normalize_name()+prefで名寄せ判定し、一致した場合は
新規行を作らず既存レコードにURL情報を書き足すだけにする
(company_idの分裂によるサプレッションリスト無効化を防ぐため)。

使い方:
  python3 ingest_mikomeru.py out/mikomeru_list1993.csv
"""
import csv
import re
import sys
from pathlib import Path

import db as D

_ZEN2HAN_DIGIT = str.maketrans("０１２３４５６７８９，", "0123456789,")

# mikomeruの業種名(自由記述)からの推定変換。
# 国交省名簿のような許可情報ではなく事業内容の自己申告文言なので、あくまで参考値。
# 「電気工事」は「電気通信工事」(別業種)と誤って一致しないよう、単に「電気」
# ではなく「電気工事」で判定する(config.py TARGET_TRADESと同じ考え方)。
# 「電気設備工事」も追加(2026-08-29: 実データで確認したところ、mikomeruの電気工事業者は
# 「電気工事」ではなくほぼ全て「電気設備工事」「産業用電気設備工事」という表記だった。
# 後者は前者を含むため1つの追加で両方拾える)。
#
# 【mikomeruの業種分類を全件登録(2026-08-29、ユーザー指示)】
# config.py TARGET_TRADESと同じ理由・同じ範囲(「業種で絞り込む」の10グループ・
# 176項目)をここにも登録した。ここのキーはconfig.TARGET_TRADESの値(コード)と
# 対応させること(片方だけ増やしても機能しない)。
TRADE_KEYWORDS = {
    "tobi": ["とび", "土工"],
    "kaitai": ["解体"],
    "tosou": ["塗装"],
    "denki": ["電気工事", "電気設備工事"],
    "zouen": ["造園", "造園・庭園設計工事"],
    "kucho": ["空調設備工事"],

    # 建設・工事
    "doboku": ["土木・インフラ工事"],
    "eiseisetsubikouj": ["衛生設備工事"],
    "purantosetsubiko": ["プラント設備工事"],
    "juutaku": ["住宅・オフィス向け設備工事"],
    "birukensetsu": ["ビル建設"],
    "tsuushinsetsubik": ["通信設備工事"],
    "kenchikusenmonko": ["建築専門工事"],
    "koutsuukanrenkou": ["交通関連工事"],
    "shougyoushisetsu": ["商業施設・公共施設建設"],
    "kenchikusekkei": ["建築設計・施工管理"],
    "sougoudobokukouj": ["総合土木工事"],
    "yougyoukeikenzai": ["窯業系建材製造"],
    "chuumonjuutakuke": ["注文住宅建築"],
    "juutakurifoomu": ["住宅リフォーム・改修工事"],
    "mokuzai": ["木材・建材製造"],
    "kenzoubutsukench": ["建造物建築・設計"],
    "sougoukensetsu": ["総合建設・ゼネコン"],
    "kinzokukeikenzai": ["金属系建材製造"],
    "jigyouyourifoomu": ["事業用リフォーム"],
    "jushikeikenzaise": ["樹脂系建材製造"],
    "bunjoukatajuutak": ["分譲型住宅建築"],
    "interiadezain": ["インテリアデザイン・空間設計"],
    "rifoomu": ["リフォーム"],
    "taiyoukoupanerus": ["太陽光パネル設置"],
    "kasen": ["河川・港湾工事"],
    "manshonkenchiku": ["マンション建築・施工"],
    "nenryoutankukouj": ["燃料タンク工事"],
    # 自動車・乗り物
    "jidoushabuhin": ["自動車部品・カーアクセサリー製造"],
    "jidoushaseizou": ["自動車製造"],
    "gomuseihin": ["ゴム製品・タイヤ製造"],
    "jidoushaseibi": ["自動車整備・修理"],
    "nirinsha": ["二輪車・バイク製造"],
    "rentakaa": ["レンタカー・リースサービス"],
    "jidoushakanrensa": ["自動車関連サービス"],
    "uchuukaihatsu": ["宇宙開発・宇宙産業"],
    "sonohokanorimono": ["その他乗り物"],
    # 機械関連サービス
    "kikairentaru": ["機械レンタル・リース"],
    "purantoenjiniari": ["プラントエンジニアリング"],
    "kikaishuuri": ["機械修理"],
    "kikaisekkei": ["機械設計"],
    "sonohokakikaikan": ["その他機械関連サービス"],
    # 電気製品
    "kadenseihinseizo": ["家電製品製造"],
    "onkyou": ["音響・映像機器製造"],
    "shoumeikiguseizo": ["照明器具製造"],
    "sonohokadenkisei": ["その他電気製品製造"],
    # 機械製造
    "denshibuhinseizo": ["電子部品製造"],
    "shikenkiseizou": ["試験機製造"],
    "kouguseizou": ["工具製造"],
    "insatsukikaiseiz": ["印刷機械製造"],
    "sangyouyourobott": ["産業用ロボット・オートメーション機器製造"],
    "kensetsukikaisei": ["建設機械製造"],
    "handoutai": ["半導体・半導体関連装置製造"],
    "kousakukikaiseiz": ["工作機械製造"],
    "kuuchouki": ["空調機"],
    "kanagataseizou": ["金型製造"],
    "sensaa": ["センサー・計測機器製造"],
    "seimitsukikiseiz": ["精密機器製造"],
    "hatsuden": ["発電・電力設備製造"],
    "nougyou": ["農業・漁業機械製造"],
    "douryokusouchise": ["動力装置製造"],
    "amyuuzumentokiki": ["アミューズメント機器製造"],
    "mizushorikikaise": ["水処理機械製造"],
    "erebeetaa": ["エレベーター・エスカレーター製造"],
    "jidouhanbaiki": ["自動販売機・自動サービス機"],
    "shokuhinkakoukik": ["食品加工機械製造"],
    "chuuboukikikanre": ["厨房機器関連製造"],
    "koutsuukikiseizo": ["交通機器製造"],
    "ponpuseizou": ["ポンプ製造"],
    "kagakukikaiseizo": ["化学機械製造"],
    "yousetsukikaisei": ["溶接機械製造"],
    "purasuchikkuseik": ["プラスチック成形機械製造"],
    "kougakukiki": ["光学機器・レンズ製造"],
    "hikinzokukakouki": ["非金属加工機械製造"],
    "boiraaseizou": ["ボイラー製造"],
    "sonohokakikaisei": ["その他機械製造"],
    # 製造
    "kinzokuseihinsei": ["金属製品製造"],
    "tekkouseizou": ["鉄鋼製造"],
    "bousai": ["防災・防犯機器"],
    "kinzokubuhinseiz": ["金属部品製造"],
    "densen": ["電線・ケーブル製造"],
    "housoushizaiseiz": ["包装資材製造"],
    "garasuseihinseiz": ["ガラス製品製造"],
    "seniseizou": ["繊維製造"],
    "hitetsukinzokuse": ["非鉄金属製造"],
    "kinzokukakouukeo": ["金属加工請負"],
    "denchiseihinseiz": ["電池製品製造"],
    "seishi": ["製紙・パルプ製造"],
    "paipu": ["パイプ・バルブ製造"],
    "sagyoukanrenyouh": ["作業関連用品製造"],
    "purasuchikkuhous": ["プラスチック包装資材製造"],
    "sutenresuseihins": ["ステンレス製品製造"],
    "hikakuseihinseiz": ["皮革製品製造"],
    "sonohokaseihinse": ["その他製品製造"],
    # 食品
    "kenkoushokuhinse": ["健康食品製造"],
    "sake": ["酒・ワイン製造販売"],
    "inryouseizou": ["飲料製造"],
    "kanzume": ["缶詰・レトルト・冷凍食品製造"],
    "suisanseizou": ["水産製造・販売関連"],
    "shokunikuseizou": ["食肉製造・販売関連"],
    "wagashiseizou": ["和菓子製造"],
    "nougyoukanren": ["農業関連"],
    "beihan": ["米飯・惣菜製造"],
    "choumiryouseizou": ["調味料製造"],
    "kashiseizouzenpa": ["菓子製造全般"],
    "koohiiseizou": ["コーヒー製造・販売"],
    "yougashiseizou": ["洋菓子製造"],
    "panseizou": ["パン製造"],
    "nyuuseihin": ["乳製品"],
    "menruiseizou": ["麺類製造"],
    "seifun": ["製粉・食用油製造"],
    "tsukemono": ["漬物・煮物・大豆製造"],
    "sonohokashokuhin": ["その他食品製造全般"],
    # 生活用品
    "nichiyouhin": ["日用品・雑貨製造販売"],
    "ofisuyouhin": ["オフィス用品・オフィス家具"],
    "tabakoseizou": ["タバコ製造"],
    "megane": ["眼鏡・コンタクトレンズ製造"],
    "supootsuyouhinse": ["スポーツ用品製造"],
    "kaguseizou": ["家具製造"],
    "senmenyouhinseih": ["洗面用品製品製造"],
    "gangu": ["玩具・ホビー製造"],
    "gifuto": ["ギフト・お土産"],
    "nyuuyoujiyouhins": ["乳幼児用品製造"],
    "zakka": ["雑貨・インテリア製造"],
    "bunbougu": ["文房具・オフィス用品製造"],
    "tenpokagu": ["店舗家具・什器製造"],
    "butsugu": ["仏具・宗教用品"],
    "bijutsuhin": ["美術品・工芸品"],
    "yunyuuzakkahanba": ["輸入雑貨販売"],
    "sonotashoukatsuy": ["その他生活用品全般"],
    # 外食
    "washoku": ["和食・家庭料理"],
    "sushi": ["寿司・海鮮料理関連"],
    "deribarii": ["デリバリー・中食サービス"],
    "fasutofuudo": ["ファストフード"],
    "izakaya": ["居酒屋・バー"],
    "kafe": ["カフェ・喫茶店"],
    "kyuushoku": ["給食・食堂"],
    "famiriiresutoran": ["ファミリーレストラン"],
    "youshoku": ["洋食・西洋料理"],
    "menruimise": ["麺類店"],
    "nikuryourisenmon": ["肉料理専門店"],
    "ajian": ["アジアン・エスニック料理"],
    "sonohokagaishoku": ["その他外食"],
    # 小売
    "jishakataonrains": ["自社型オンラインストア"],
    "iyakuhinhanbai": ["医薬品販売"],
    "suupaamaaketto": ["スーパーマーケット"],
    "aparerushoppu": ["アパレルショップ"],
    "kouritenho": ["小売店舗・施設"],
    "gasorinsutando": ["ガソリンスタンド"],
    "furuhon": ["古本・リサイクルショップ"],
    "shokuhinkanren": ["食品関連"],
    "chuukoshahanbai": ["中古車販売"],
    "ekomaasu": ["eコマース・オンラインモール"],
    "jidoushabuhin2": ["自動車部品・カーアクセサリー販売"],
    "jidousha": ["自動車・自転車販売"],
    "keshouhinhanbai": ["化粧品販売"],
    "shoseki": ["書籍・マルチメディア販売"],
    "kagu": ["家具・インテリア販売"],
    "supootsuyouhinha": ["スポーツ用品販売"],
    "sagyoukanrenyouh2": ["作業関連用品販売"],
    "shinshahanbai": ["新車販売"],
    "furawaashoppu": ["フラワーショップ・花屋"],
    "megane2": ["眼鏡・コンタクトレンズ販売"],
    "nyuuseihintakuha": ["乳製品宅配"],
    "pasokon": ["パソコン・スマホ周辺機器販売"],
    "juerii": ["ジュエリー・アクセサリーショップ"],
    "biyouguzzuhanbai": ["美容グッズ販売"],
    "konbini": ["コンビニ"],
    "hyakkaten": ["百貨店"],
    "kodomofukukanren": ["子供服関連ショップ"],
    "sonohokakouri": ["その他小売"],
    # 運輸・物流
    "ippankamotsuyusousaabisu": ["一般貨物輸送サービス"],
    "kuuunkoukuubutsuryuu": ["空運・航空物流"],
    "basukoukyoukoutsuukikan": ["バス・公共交通機関"],
    "takushiihaiyaa": ["タクシー・ハイヤー"],
    "reitoureizouyusou": ["冷凍・冷蔵輸送"],
    "tetsudourikuun": ["鉄道・陸運"],
    "kaiun": ["海運"],
    "soukokanriunei": ["倉庫管理・運営"],
    "hikkoshiitensaabisu": ["引っ越し・移転サービス"],
    "juukikikaiyusou": ["重機・機械輸送"],
    "juuryoubutsuyusou": ["重量物輸送"],
    "kouwankaijouyusoushien": ["港湾・海上輸送支援"],
    "sonohokaunyubutsuryuu": ["その他運輸・物流"],
    # 人材系
    "gyoumuukeoisaabisu": ["業務請負サービス"],
    "seizougyougijutsushokuhaken": ["製造業・技術職派遣"],
    "saabisugyoujinzaihaken": ["サービス業人材派遣"],
    "jimushoriautosooshingu": ["事務処理アウトソーシング"],
    "iryoufukushijinzaihaken": ["医療・福祉人材派遣"],
    "butsuryuusoukokanrenjinzaihaken": ["物流・倉庫関連人材派遣"],
    "jimuinsagyouinhaken": ["事務員・作業員派遣"],
    "jinzaishoukai": ["人材紹介"],
    "koorusentaaunei": ["コールセンター運営"],
    "kigyoukenshuutoreeningu": ["企業研修・トレーニング"],
    "sonotaninzaizenpan": ["その他人材全般"],
    "seminaakobetsushidousaabisu": ["セミナー・個別指導サービス"],
    # 医療・福祉・バイオ
    "chouzaiyakkyokuyakkyokujigyou": ["調剤薬局・薬局事業"],
    "seiyaku": ["製薬"],
    "iryoukikijikkenkiutsuwaseizou": ["医療機器・実験機器製造"],
    "sonohokairyouryouyoushisetsuunei": ["その他医療・療養施設運営"],
    "koureishamukefukushi": ["高齢者向け福祉"],
    "kaigoyouhinzaitakuiryoukiki": ["介護用品・在宅医療機器"],
    "shougaimonofukushijigyou": ["障がい者福祉事業"],
    "byouin": ["病院"],
    "iryouhounin": ["医療法人"],
    "koureishamukejuutakushisetsu": ["高齢者向け住宅施設"],
    "jidoufukushihoikukanren": ["児童福祉・保育関連"],
    "kaigofukushi": ["介護・福祉"],
    "kurinikkuiinshinryoujo": ["クリニック・医院・診療所"],
    "baiotekunorojiisentaniryou": ["バイオテクノロジー・先端医療"],
    "doubutsubyouin": ["動物病院"],
    "haisha": ["歯医者"],
    "sonohokairyoufukushisaabisu": ["その他医療・福祉サービス"],
    # 広告
    "koukokukikakudairiten": ["広告企画代理店"],
    "onrainkoukokudairi": ["オンライン広告代理"],
    "sonohokakoukoku": ["その他広告"],
    "tenjikaipuromooshonibento": ["展示会・プロモーションイベント"],
    # 商社関連
    "sougoushousha": ["総合商社"],
    "kagakuhiniyakuhinshousha": ["化学品・医薬品商社"],
    "shokuhinkanrensenmonshousha": ["食品関連専門商社"],
    "tekkoukinzokushousha": ["鉄鋼・金属商社"],
    "iryoukikikigushousha": ["医療機器・器具商社"],
    "kougyouyoukikaisenmonshousha": ["工業用機械専門商社"],
    "nousanbutsushokuhinshousha": ["農産物食品商社"],
    "denshibuhinshousha": ["電子部品商社"],
    "nichiyouhinkeshouhinshousha": ["日用品・化粧品商社"],
    "kamiparupusenmonshousha": ["紙・パルプ専門商社"],
    "kikaisenmonshousha": ["機械専門商社"],
    "kenzaisenmonshousha": ["建材専門商社"],
    "shokunikutamagokanrensenmonshousha": ["食肉・卵関連専門商社"],
    "suisanbutsushokuhinsenmonshousha": ["水産物食品専門商社"],
    "nourinsuisanyoukikaishousha": ["農林水産用機械商社"],
    "seniaparerushousha": ["繊維・アパレル商社"],
    "zakkanichiyouhinsenmonshousha": ["雑貨・日用品専門商社"],
    "sonohokasenmonshousha": ["その他専門商社"],
    # 不動産
    "manshonapaatochintai": ["マンション・アパート賃貸"],
    "sonohokafudousan": ["その他不動産"],
    "sougoufudousandeberoppaa": ["総合不動産（デベロッパー）"],
    "kodatechintai": ["戸建賃貸"],
    "kodatebaibai": ["戸建売買"],
    "jigyouyoubukkentenantobiruchintai": ["事業用物件・テナントビル賃貸"],
    "manshonapaatobaibai": ["マンション・アパート売買"],
    "chuushajouunei": ["駐車場運営"],
    "manshonbirukanri": ["マンション・ビル管理"],
    "rentarusupeesuteikyou": ["レンタルスペース提供"],
    "jigyouyoubukkentenantobirubaibai": ["事業用物件・テナントビル売買"],
    "tochibaibaichintai": ["土地売買・賃貸"],
    "sonohokafudousankanri": ["その他不動産管理"],
    # ファッション・美容
    "sukinkea": ["スキンケア"],
    "kosumeteikkuseizou": ["コスメティック製造"],
    "rediisuapareru": ["レディースアパレル"],
    "tokei": ["時計"],
    "innaaueakutsushitaseizou": ["インナーウェア・靴下製造"],
    "esuterirakuzeeshon": ["エステ・リラクゼーション"],
    "bagguapareruzakka": ["バッグ・アパレル雑貨"],
    "juerii2": ["ジュエリー"],
    "senishokufu": ["繊維・織布"],
    "shuuzu": ["シューズ"],
    "biyousaronheakea": ["美容サロン・ヘアケア"],
    "seifukuwaakuueaseizou": ["制服・ワークウェア製造"],
    "sonohokabiyou": ["その他美容"],
    "kodomofuku": ["子供服"],
    "menzuapareru": ["メンズアパレル"],
    "sonohokaapareru": ["その他アパレル"],
    # エンタメ・レジャー
    "gorufubaunei": ["ゴルフ場運営"],
    "eizoucmseisaku": ["映像・CM制作"],
    "pachinkoamyuuzumento": ["パチンコ・アミューズメント"],
    "ryokanhoterushukuhakushisetsu": ["旅館・ホテル・宿泊施設"],
    "ryokoukanren": ["旅行関連"],
    "pettodoubutsukanrensaabisu": ["ペット・動物関連サービス"],
    "maruchimediagakki": ["マルチメディア・楽器"],
    "ibentokikakuunei": ["イベント企画・運営"],
    "supootsubijinesukanren": ["スポーツビジネス関連"],
    "fittonesujimu": ["フィットネス・ジム"],
    "kaigairyokouryuugakushien": ["海外旅行・留学支援"],
    "eigaanime": ["映画・アニメ"],
    "geinoupurodakushon": ["芸能プロダクション"],
    "tarentokyarakutaaguzzu": ["タレント・キャラクターグッズ"],
    "sonohokaentamerejaa": ["その他エンタメ・レジャー"],
    # コンサル
    "itkonsaruteingu": ["ITコンサルティング"],
    "zaimukonsaruteingu": ["財務コンサルティング"],
    "iryoukanrenkonsaruteingu": ["医療関連コンサルティング"],
    "sougoukonsaruteingu": ["総合コンサルティング"],
    "puromooshonsenryakukonsaruteingu": ["プロモーション戦略コンサルティング"],
    "keieikonsaruteingu": ["経営コンサルティング"],
    "seizougyoukonsaruteingu": ["製造業コンサルティング"],
    "fudousankonsaruteingu": ["不動産コンサルティング"],
    "soshikijinjisenryakukonsaruteingu": ["組織・人事戦略コンサルティング"],
    "dejitarumaaketeingu": ["デジタルマーケティング"],
    "dobokukenchikukonsaruteingu": ["土木・建築コンサルティング"],
    "inshokukanrenkonsaruteingu": ["飲食関連コンサルティング"],
    "kosutosakugenkonsaruteingu": ["コスト削減コンサルティング"],
    "shisanunyouadobaizaa": ["資産運用アドバイザー"],
    "koukokuunyoukonsaruteingu": ["広告運用コンサルティング"],
    "sutaatoappushien": ["スタートアップ支援"],
    "sonohokakonsaruteingu": ["その他コンサルティング"],
    # 金融
    "hokensaabisu": ["保険サービス"],
    "toushishisanunyou": ["投資・資産運用"],
    "ginkoushinyoukinkoshinyoukumiai": ["銀行・信用金庫・信用組合"],
    "shouken": ["証券"],
    "kurejittoshinpankessaisaabisu": ["クレジット・信販・決済サービス"],
    "hokendairimise": ["保険代理店"],
    "kashikinroonsaabisu": ["貸金・ローンサービス"],
    "jigyoushamukekinyuusaabisu": ["事業者向け金融サービス"],
    "nettoshouken": ["ネット証券"],
    "sonohokakinyuukanrensaabisu": ["その他金融関連サービス"],
    # IT
    "saibaasekyuriteisaabisu": ["サイバーセキュリティサービス"],
    "sofutoueasenmonshousha": ["ソフトウェア専門商社"],
    "itinfurakouchikuunyou": ["ITインフラ構築・運用"],
    "jutakukaihatsusi": ["受託開発・SI"],
    "sofutoueakaihatsu": ["ソフトウェア開発"],
    "webdezainseisaku": ["Webデザイン・制作"],
    "websaabisuapuriunei": ["Webサービス・アプリ運営"],
    "dejitarukontentsuseisakuunyou": ["デジタルコンテンツ制作・運用"],
    "kuraudofintekku": ["クラウド・フィンテック"],
    "sonohokait": ["その他IT"],
    # 教育・スクール関連
    "gakushuujukuyobikou": ["学習塾・予備校"],
    "sukuurunaraigoto": ["スクール・習い事"],
    "youchienhoikuen": ["幼稚園・保育園"],
    "daigaku": ["大学"],
    "shikakushutokutsuushinkyouiku": ["資格取得・通信教育"],
    "itkyouikukanren": ["IT教育関連"],
    "kyouzaiseisakuhanbai": ["教材製作・販売"],
    "gogakugakushuusukuuru": ["語学学習スクール"],
    "shougakkouchuugakkoukoukou": ["小学校・中学校・高校"],
    "senmongakkou": ["専門学校"],
    "sonohokagakkoukyouikukikan": ["その他学校・教育機関"],
    # 化学
    "kagakuhinkagakuyakuhinseizou": ["化学品・化学薬品製造"],
    "toryouseizou": ["塗料製造"],
    "jushiseihinseizou": ["樹脂製品製造"],
    "jushiseibuhinseizou": ["樹脂製部品製造"],
    "hiryounouyakuengeiyouhinseizou": ["肥料・農薬・園芸用品製造"],
    "setchakuzainenchakuteepuseizou": ["接着剤・粘着テープ製造"],
    "sonohokakagaku": ["その他化学"],
    # 公共サービス
    "kankouchou": ["官公庁"],
    "saibanshokensatsuchou": ["裁判所・検察庁"],
    # 石炭・鉱石採掘
    "shigenmejaa": ["資源メジャー"],
    "kikinzokusaikutsuseiren": ["貴金属採掘・精錬"],
    "saikutsusaisekikanren": ["採掘・採石関連"],
    "sekitansekkaiishikaihatsuhanbai": ["石炭・石灰石開発、販売"],
    "sekitankaihatsusaabisu": ["石炭開発サービス"],
    "sonohokakinzokusaikutsu": ["その他金属採掘"],
    # エネルギー
    "gasunenryoukanren": ["ガス・燃料関連"],
    "denryokukyoukyuu": ["電力供給"],
    "saiseikanouenerugii": ["再生可能エネルギー"],
    "sonohokaenerugii": ["その他エネルギー"],
    # ゲーム
    "soosharugeemu": ["ソーシャルゲーム"],
    "geemusofutokaihatsu": ["ゲームソフト開発"],
    "animeeshondezain": ["アニメーションデザイン"],
    "sonohokageemukanrensaabisu": ["その他ゲーム関連サービス"],
    # 専門サービス
    "senmonjimusho": ["専門事務所"],
    "honyakutsuuyaku": ["翻訳・通訳"],
    # 通信及び通信機器
    "tsuushinkaisenteikyou": ["通信回線提供"],
    "pasokonseizouhanbaishuuri": ["パソコン製造・販売・修理"],
    "keitaitsuushinkaisenhanbaidairiten": ["携帯・通信回線販売代理店"],
    "pasokonsumahoshuuhenkikiseizou": ["パソコン・スマホ周辺機器製造"],
    "denwakiseizou": ["電話機製造"],
    "sumahotaburettoseizoushuuri": ["スマホ・タブレット製造・修理"],
    "sonohokatsuushin": ["その他通信"],
    "sonohokatsuushinkiutsuwa": ["その他通信機器"],
    # メディア・出版関連
    "terebirajiohousoukyoku": ["テレビ・ラジオ放送局"],
    "shosekizasshishuppan": ["書籍・雑誌出版"],
    "shinbun": ["新聞"],
    "terebibangumiseisaku": ["テレビ番組制作"],
    "dejitarushosekishuppan": ["デジタル書籍出版"],
    "rajiobangumiseisaku": ["ラジオ番組制作"],
    "mediazenpan": ["メディア全般"],
    # その他サービス業界
    "sekyuriteikeibi": ["セキュリティ・警備"],
    "kuriininguseisousaabisu": ["クリーニング・清掃サービス"],
    "chousakensakenkyuukanren": ["調査・検査・研究関連"],
    "birushisetsuseisou": ["ビル・施設清掃"],
    "haikibutsushuushuuunpansaabisu": ["廃棄物収集・運搬サービス"],
    "haikibutsushobun": ["廃棄物処分"],
    "satsueisaabisu": ["撮影サービス"],
    "rentaruriisu": ["レンタル・リース"],
    "sonohokadezainkurieiteibu": ["その他デザイン・クリエイティブ"],
    "seikatsukanrenrentaruriisu": ["生活関連レンタル・リース"],
    "risaikururiyuusu": ["リサイクル・リユース"],
    "buraidaru": ["ブライダル"],
    "sougisousaikanren": ["葬儀・葬祭関連"],
    "sonotadantaigyoukai": ["その他団体業界"],
    "hokenkumiai": ["保険組合"],
    "sonohokaseisou": ["その他清掃"],
    "ofisukikirentaruriisu": ["オフィス機器レンタル・リース"],
    "ihinseirisaabisu": ["遺品整理サービス"],
    "sonohokasaabisu": ["その他サービス"],
    # その他業界
    "kumiaidantairengoukaikyoukai": ["組合・団体・連合会・協会"],
    "npo": ["NPO"],
    "shuukyouhoujin": ["宗教法人"],
}


# TRADE_KEYWORDSは部分文字列一致なので、短い業種名がより具体的な複合語の一部として
# 誤ヒットすることがある(例: 「病院」が「動物病院」に、「食品関連」が「食品関連専門商社」に
# 一致してしまう)。同じ業種内の広い/狭いの関係(「その他不動産」⊂「その他不動産管理」等)は
# 実害が無いため許容するが、別業種にまたがるものだけここで個別に除外する(T63)。
TRADE_EXCLUDE_KEYWORDS = {
    "byouin": ["動物病院"],
    "shokuhinkanren": ["食品関連専門商社"],
}


def map_trades(gyoshu: str) -> str:
    g = gyoshu or ""
    hits = [
        code for code, kws in TRADE_KEYWORDS.items()
        if any(k in g for k in kws)
        and not any(ex in g for ex in TRADE_EXCLUDE_KEYWORDS.get(code, []))
    ]
    return ",".join(hits)


def parse_capital_sen(raw: str):
    """資本金の自由記述("1,000万円"/"3千万円"/"5,000,000円"等) → 千円単位の整数。
    companies.capitalの既存単位(千円)に合わせる。パース不能ならNone。"""
    if not raw:
        return None
    s = raw.strip().translate(_ZEN2HAN_DIGIT)
    if s in ("", "-", "－", "不明", "非公開"):
        return None
    s = re.sub(r"[\s　]", "", s)
    m = re.search(r"(\d[\d,]*)\s*億\s*(\d[\d,]*)?\s*万", s)
    if m:
        oku = int(m.group(1).replace(",", ""))
        man = int(m.group(2).replace(",", "")) if m.group(2) else 0
        return (oku * 10000 + man) * 10
    m = re.search(r"(\d[\d,]*)\s*億", s)
    if m:
        return int(m.group(1).replace(",", "")) * 10000 * 10
    m = re.search(r"(\d+)千(\d+)百万円", s)
    if m:
        return (int(m.group(1)) * 1000 + int(m.group(2)) * 100) * 10
    m = re.search(r"(\d+)千万円", s)
    if m:
        return int(m.group(1)) * 1000 * 10
    m = re.search(r"(\d+)百万円", s)
    if m:
        return int(m.group(1)) * 100 * 10
    m = re.search(r"(\d[\d,]*)\s*万円?", s)
    if m:
        return int(m.group(1).replace(",", "")) * 10
    m = re.search(r"(\d[\d,]*)\s*千円", s)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.search(r"(\d[\d,]*)\s*円", s)
    if m:
        return round(int(m.group(1).replace(",", "")) / 1000)
    return None


def parse_employees(raw: str):
    if not raw:
        return None
    s = raw.strip().translate(_ZEN2HAN_DIGIT)
    if s in ("", "-", "－"):
        return None
    if s.isdigit():
        return int(s)
    m = re.search(r"(\d+)\s*[名人]", s)
    return int(m.group(1)) if m else None


def clean_url(raw: str):
    """"https://example.com [URL:https://example.com/]" 形式から実URLを取り出す
    (取込に使ったブラウザ側スクレイピングスクリプトが、表示テキストとhrefが
    食い違う場合にこの形式で埋め込んでいる)。"""
    if not raw or raw.strip() in ("", "-"):
        return None
    m = re.search(r"\[URL:(.*?)\]", raw)
    return m.group(1) if m else raw.strip()


def find_representative(con, name_norm, pref):
    """name_norm+prefで一致する既存company_idのうち代表社(dedup_of未設定側)を返す。
    複数ヒット時は代表社優先、無ければ最小id。1件もヒットしなければNone。"""
    rows = con.execute(
        "SELECT id, dedup_of FROM companies WHERE name_norm=? AND pref=?",
        (name_norm, pref)).fetchall()
    if not rows:
        return None
    reps = [r["id"] for r in rows if r["dedup_of"] is None]
    return min(reps) if reps else min(r["id"] for r in rows)


def main(csv_path):
    con = D.connect()
    D.migrate(con)
    # 照合精度のため、全行のname_normをその場で再計算する(NULLのみ埋める方式だと
    # normalize_name()のロジック変更が既存行のキャッシュ値に反映されず古い基準のまま
    # 照合してしまう事故が起きるため、都度フル再計算にしている)
    for r in con.execute("SELECT id, name FROM companies").fetchall():
        con.execute("UPDATE companies SET name_norm=? WHERE id=?", (D.normalize_name(r["name"]), r["id"]))
    con.commit()

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    updated, inserted, skipped = 0, 0, 0
    for r in rows:
        name = (r.get("商号又は名称") or "").strip()
        if not name:
            skipped += 1
            continue
        pref = (r.get("国内所在地（都道府県）") or "東京都").strip() or "東京都"
        name_norm = D.normalize_name(name)
        website_url = clean_url(r.get("ホームページ"))
        contact_url = clean_url(r.get("問い合わせページ"))
        has_form = 1 if (r.get("フォームの有無") or "").strip().startswith("✓") else 0
        corporate_no = (r.get("法人番号") or "").strip() or None

        rep_id = find_representative(con, name_norm, pref)
        if rep_id:
            cur = con.execute("SELECT website_url, has_website, trades FROM companies WHERE id=?",
                               (rep_id,)).fetchone()
            new_website = cur["website_url"] or website_url
            new_has_website = 1 if (cur["has_website"] == 1 or website_url) else cur["has_website"]
            # trades も既存レコード更新のたびに合わせる(足すだけで既存分は消さない)。
            # TRADE_KEYWORDSを直したあとの再取込で、既存社の業種判定も追いつくようにするため
            # (以前はここでtradesを一切更新しておらず、初回INSERT時の判定のまま固定されていた)
            existing_trades = {t for t in (cur["trades"] or "").split(",") if t}
            csv_trades = {t for t in (map_trades(r.get("業種")) or "").split(",") if t}
            merged_trades = ",".join(sorted(existing_trades | csv_trades)) or None
            con.execute("""UPDATE companies SET
                website_url=?, has_website=?,
                contact_url=COALESCE(NULLIF(contact_url,''), ?),
                has_contact_form=COALESCE(has_contact_form, ?),
                corporate_no=COALESCE(NULLIF(corporate_no,''), ?),
                trades=?
                WHERE id=?""",
                (new_website, new_has_website, contact_url, has_form, corporate_no, merged_trades, rep_id))
            updated += 1
            continue

        capital = parse_capital_sen(r.get("資本金"))
        est_employees = parse_employees(r.get("従業員数"))
        trades = map_trades(r.get("業種")) or None
        city = (r.get("国内所在地（市区町村）") or "").strip() or None
        address = (r.get("国内所在地（丁目番地等）") or "").strip() or None

        con.execute("""INSERT INTO companies
            (name, name_norm, pref, city, address, capital, est_employees, trades,
             has_website, website_url, contact_url, has_contact_form, corporate_no, data_source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, name_norm, pref, city, address, capital, est_employees, trades,
             1 if website_url else 0, website_url, contact_url, has_form, corporate_no, "mikomeru"))
        inserted += 1

    con.commit()
    print(f"取込完了: 既存更新 {updated}件 / 新規追加 {inserted}件 / スキップ {skipped}件 (CSV合計 {len(rows)}件)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方: python3 ingest_mikomeru.py <CSVファイルパス>")
        sys.exit(1)
    main(sys.argv[1])
