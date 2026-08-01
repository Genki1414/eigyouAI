"""
ingest.py — 国交省「建設業者・宅建業者等企業情報検索システム」CSVの取込
実データの入手先: https://etsuran2.mlit.go.jp/TAKKEN/ (建設業者検索 → CSVダウンロード)
対象業種コード: とび・土工工事業(05) / 塗装工事業(14) / 解体工事業(29)

使い方:
  python3 ingest.py data/kyoka_gyosha.csv
→ SQLite (out/companies.db) に正規化して格納
"""
import csv, sqlite3, sys, re
from pathlib import Path

DB = Path(__file__).parent / "out" / "companies.db"

SCHEMA_UNUSED = """  # スキーマは db.py に一本化（参照用に残置）
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  license_no TEXT UNIQUE,          -- 許可番号
  name TEXT NOT NULL,              -- 商号
  pref TEXT,                       -- 都道府県
  city TEXT,                       -- 市区町村
  address TEXT,
  phone TEXT,
  fax TEXT,
  license_type TEXT,               -- 知事/大臣
  trades TEXT,                     -- 業種(カンマ区切り: tobi/tosou/kaitai...)
  capital INTEGER,                 -- 資本金(千円)
  founded_year INTEGER,            -- 設立年(許可年月日から推定)
  -- ▼ AIエンリッチメントで埋める列 (enrich.py)
  has_website INTEGER DEFAULT NULL,     -- 0/1
  website_url TEXT,
  website_quality INTEGER DEFAULT NULL, -- 0-3 (AI判定)
  hiring_now INTEGER DEFAULT NULL,      -- 0/1 求人出稿中
  est_employees INTEGER DEFAULT NULL,   -- 従業員数推定
  google_reviews INTEGER DEFAULT NULL,  -- Googleレビュー数
  prime_ratio REAL DEFAULT NULL,        -- 元請比率推定 0-1
  enrich_note TEXT,                     -- AI所見(営業トークの種)
  -- ▼ スコアリング結果 (scoring.py)
  score REAL,
  rank TEXT,                       -- S/A/B/C
  score_detail TEXT                -- JSON内訳
);
CREATE INDEX IF NOT EXISTS idx_rank ON companies(rank);
CREATE INDEX IF NOT EXISTS idx_pref ON companies(pref);
"""

TRADE_MAP = {"とび": "tobi", "土工": "tobi", "塗装": "tosou", "解体": "kaitai"}

def norm_trades(raw: str) -> str:
    found = {v for k, v in TRADE_MAP.items() if k in (raw or "")}
    return ",".join(sorted(found))

def main(csv_path: str):
    DB.parent.mkdir(exist_ok=True)
    import db
    con = db.connect()
    db.migrate(con)
    n = 0
    with open(csv_path, encoding="cp932", errors="replace") as f:
        for row in csv.DictReader(f):
            trades = norm_trades(row.get("許可業種", ""))
            if not trades:
                continue  # 対象3業種以外はスキップ
            year = None
            m = re.search(r"(19|20)\d{2}", row.get("許可年月日", ""))
            if m:
                year = int(m.group())
            con.execute(
                """INSERT OR IGNORE INTO companies
                   (license_no,name,pref,city,address,phone,fax,license_type,trades,capital,founded_year)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (row.get("許可番号"), row.get("商号又は名称"), row.get("都道府県"),
                 row.get("市区町村"), row.get("所在地"), row.get("電話番号"), row.get("FAX番号"),
                 "大臣" if "大臣" in (row.get("許可区分") or "") else "知事",
                 trades, int(row.get("資本金") or 0), year))
            n += 1
    con.commit()
    print(f"取込完了: {n}社 → {DB}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/kyoka_gyosha.csv")
