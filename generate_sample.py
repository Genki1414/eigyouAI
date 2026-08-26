"""
generate_sample.py — 動作確認用サンプル200社を生成してDBに投入
(本番運用では ingest.py + enrich.py の実データに置換。分布は業界実態に寄せてある:
 HP保有率~55%, 求人出稿~25%, 従業員中央値8人, 知事許可~93%)
"""
import random
from pathlib import Path
import db

random.seed(42)
DB = Path(__file__).parent / "out" / "companies.db"

PREFS = ["東京都","神奈川県","埼玉県","千葉県","大阪府","愛知県","福岡県","北海道","兵庫県","静岡県"]
N1 = ["丸","大","協","誠","翔","旭","富士","北","東","共"]
N2 = ["和","栄","真","伸","勝","隆","光","成","健","友"]
SUF = ["建設","工業","興業","足場","組","建工","架設","テック","ワークス"]
NOTES = [
    "HPに施工実績多数、積算は手作業の記載あり。実績写真を褒める切り口が有効",
    "indeed出稿中で職長募集。人手不足文脈でツール訴求が刺さる可能性",
    "代表がSNS発信中。DMより先にフォロー接触が自然",
    "元請中心で戸建足場が主力。積算頻度が高くツール適合度が高い",
    "HPなし・タウンページのみ。FAXが唯一の到達チャネル",
    "レビュー高評価。地域の評判を切り口にした紹介文脈が使える",
    "解体併業で案件単価大。見積スピードを訴求点に",
    "設立5年未満の成長企業。デジタルツール受容性が高い",
]

def main():
    DB.parent.mkdir(exist_ok=True)
    # db.migrate()はstorage.table_columns()経由で列名アクセス(r["name"])を
    # 前提にしているため、row_factory=sqlite3.Row を設定するdb.connect()を
    # 使う(素のsqlite3.connect()だとタプルアクセスになりTypeErrorになる)。
    con = db.connect()
    con.executescript("""DROP TABLE IF EXISTS companies; DROP TABLE IF EXISTS touches;
        DROP TABLE IF EXISTS campaigns; DROP TABLE IF EXISTS dormant;
        DROP TABLE IF EXISTS suppression;""")
    db.migrate(con)   # スキーマ定義はdb.pyに一本化
    for i in range(3000):
        name = f"{random.choice(['株式会社','有限会社',''])}{random.choice(N1)}{random.choice(N2)}{random.choice(SUF)}"
        has_hp = random.random() < 0.55
        wq = random.choices([1,2,3],[0.4,0.4,0.2])[0] if has_hp else 0
        hiring = random.random() < (0.4 if wq >= 2 else 0.15)
        emp = max(2, int(random.lognormvariate(2.2, 0.8)))
        trades = random.choices(["tobi","tobi,tosou","tobi,kaitai","tosou","kaitai","tobi,tosou,kaitai"],
                                [0.45,0.12,0.15,0.12,0.08,0.08])[0]
        # HPありの会社のうち一部は問い合わせフォームが見つかった状態にしておく
        # (contact_url。mikomeru由来の実データではこの列がほぼ全件揃っているため、
        # デモデータでも空のままだとフォーム自動送信<target_lists.send_list()>を
        # 一切試せず、tenant向けSaaS機能の動作確認ができない)。
        has_contact_form = has_hp and random.random() < 0.7
        con.execute(
            """INSERT INTO companies
               (license_no,name,pref,city,phone,fax,license_type,trades,capital,founded_year,
                has_website,website_url,contact_url,website_quality,hiring_now,est_employees,
                google_reviews,prime_ratio,enrich_note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"般-{random.randint(1,6)}-第{10000+i}号", name, random.choice(PREFS), "",
             f"0{random.randint(3,9)}-{random.randint(1000,9999)}-{random.randint(1000,9999)}",
             f"0{random.randint(3,9)}-{random.randint(1000,9999)}-{random.randint(1000,9999)}",
             "知事" if random.random() < 0.93 else "大臣",
             trades, random.choice([3000,5000,10000,20000,50000]),
             random.randint(1975, 2024),
             int(has_hp), f"https://example-{i}.co.jp" if has_hp else None,
             f"https://example-{i}.co.jp/contact" if has_contact_form else None, wq,
             int(hiring), emp, random.choices([0,2,5,12,30],[0.5,0.2,0.15,0.1,0.05])[0],
             round(random.betavariate(2,3),2), random.choice(NOTES)))
    con.commit()
    print(f"サンプル3000社を投入 → {DB}")

if __name__ == "__main__":
    main()
