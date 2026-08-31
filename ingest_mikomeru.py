"""
ingest_mikomeru.py — mikomeru保存済みリスト(CSV)の取込
既存companies.dbとは別データソース(法人番号ベースの業種横断ディレクトリ)。
国交省名簿は config.TARGET_TRADES の許可業者のみだが、mikomeruは業種を問わない
一般的な建設業者ディレクトリで、ホームページ/問い合わせページ/フォーム有無が
ほぼ全件揃っている(=Webフォーム経由の営業チャネルに使える一次情報)。

【建設業以外(警備・情報処理・清掃サービス・廃棄物処理・給食等)は対象外】
mikomeruも建設業者ディレクトリなので、これらの業種はそもそも収録が無い。
AI入札連携(AInyusatsu)が必要とする業種の一部(電気・造園)は建設業許可の
枠内なのでTRADE_KEYWORDSに追加したが、それ以外は別のデータソースが要る
(eigyouAI HANDOFF.md「5. 連絡すべき判断」に選定未着手として記録)。

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
}


def map_trades(gyoshu: str) -> str:
    hits = [code for code, kws in TRADE_KEYWORDS.items() if any(k in (gyoshu or "") for k in kws)]
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
