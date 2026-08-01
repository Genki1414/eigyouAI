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

# 元号 → 西暦の起点(元年=1)。国交省系CSVは和暦表記が多い実データ対策。
ERA_BASE = {"令和": 2018, "平成": 1988, "昭和": 1925, "大正": 1911}

def norm_trades(raw: str) -> str:
    found = {v for k, v in TRADE_MAP.items() if k in (raw or "")}
    return ",".join(sorted(found))

def parse_year(raw: str):
    """許可年月日から西暦年を抜き出す。和暦(令和/平成/昭和/大正)と西暦の両方に対応。"""
    raw = (raw or "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    for era, base in ERA_BASE.items():
        m = re.search(rf"{era}\s*(元|\d+)\s*年", raw)
        if m:
            n = 1 if m.group(1) == "元" else int(m.group(1))
            return base + n
    m = re.search(r"(19|20)\d{2}", raw)
    return int(m.group()) if m else None

def parse_capital(raw: str) -> int:
    """資本金のカンマ区切り・円/千円表記・全角数字を吸収して整数(千円)にする。"""
    if raw is None:
        return 0
    s = raw.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    s = re.sub(r"[,，円\s]", "", s)
    if not s or not re.search(r"\d", s):
        return 0
    m = re.search(r"\d+", s)
    return int(m.group()) if m else 0

def main(csv_path: str):
    DB.parent.mkdir(exist_ok=True)
    import db
    con = db.connect()
    db.migrate(con)
    n = 0
    seen_any_row = False
    with open(csv_path, encoding="cp932", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            seen_any_row = True
            trades = norm_trades(row.get("許可業種", ""))
            if not trades:
                continue  # 対象3業種以外はスキップ
            con.execute(
                """INSERT OR IGNORE INTO companies
                   (license_no,name,pref,city,address,phone,fax,license_type,trades,capital,founded_year)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (row.get("許可番号"), row.get("商号又は名称"), row.get("都道府県"),
                 row.get("市区町村"), row.get("所在地"), row.get("電話番号"), row.get("FAX番号"),
                 "大臣" if "大臣" in (row.get("許可区分") or "") else "知事",
                 trades, parse_capital(row.get("資本金")), parse_year(row.get("許可年月日"))))
            n += 1
    con.commit()
    if seen_any_row and n == 0:
        print("警告: 1件も取り込めませんでした。CSVの列名が想定と異なる可能性があります。")
        print(f"  実際の列名: {reader.fieldnames}")
        print("  想定している列名: 許可番号,商号又は名称,都道府県,市区町村,所在地,"
              "電話番号,FAX番号,許可区分,許可業種,資本金,許可年月日")
    print(f"取込完了: {n}社 → {DB}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/kyoka_gyosha.csv")
