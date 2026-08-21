"""
target_lists.py — テナントごとの送信先リスト作成
「他社に売るSaaS」として、顧客テナントが自分の送信先リストを作れるようにする。
作り方は2種類:
  1. フィルタ型: 共有マスタ(companies)から条件で絞り込んでスナップショット保存
  2. CSV型: 顧客が自分の企業リストをアップロードして取り込む

companies.owner_tenant_id: NULL=全テナント共有の国交省/mikomeru由来マスタ。
値あり=そのテナントがCSVで持ち込んだ非公開データ(他テナントには一切見えない)。
フィルタ・一覧・詳細のすべてでこの境界を必ず通すこと(他テナント漏洩の防止)。

api.pyの /api/tenant/* エンドポイントがこのモジュールを呼ぶ。CLIは検証用。
  python3 target_lists.py list --api-key <key>
  python3 target_lists.py preview --api-key <key> --pref 東京都 --trade tobi
"""
import argparse
import csv
import io
import json
from datetime import datetime

import config as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS target_lists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  source TEXT NOT NULL,          -- 'filter' | 'csv'
  filter_json TEXT,              -- source='filter'の場合の条件(再現・監査用)
  company_count INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS target_list_members (
  list_id INTEGER NOT NULL,
  company_id INTEGER NOT NULL,
  PRIMARY KEY (list_id, company_id),
  FOREIGN KEY(list_id) REFERENCES target_lists(id),
  FOREIGN KEY(company_id) REFERENCES companies(id)
);
"""

# 暴走・誤操作の被害を抑えるための保守的な初期値(FormSenderのペーシングと同じ考え方)。
# 実績を見てから引き上げる想定。
MAX_LIST_SIZE = 20000
MAX_CSV_ROWS = 20000

# フィルタ項目の許可リスト。顧客からの入力を直接SQLへ混ぜないための唯一の入口。
_ALLOWED_TRADES = set(C.TARGET_TRADES.values())  # {"tobi","tosou","kaitai"}
_ALLOWED_RANKS = {"S", "A", "B", "C"}
_ALLOWED_PREFS = {
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府",
    "兵庫県", "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県",
    "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県",
    "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
}


def _base_where(tenant_id):
    """全フィルタ共通の絞り込み: 重複統合済みの代表行のみ、かつ
    自テナントの非公開データ or 全テナント共有マスタのみ(他テナントの
    非公開データは決して見えないようにする)。"""
    return "dedup_of IS NULL AND (owner_tenant_id IS NULL OR owner_tenant_id=?)", [tenant_id]


def build_filter_sql(tenant_id, filters):
    """filtersはAPIから来たdict。許可した項目だけをパラメータ化SQLに変換する。
    未知のキーは無視する(将来のフロント拡張で余剰キーが増えても壊れないように)。"""
    where, params = _base_where(tenant_id)
    clauses = [where]

    # 都道府県は複数選択(チェックボックス)。エリア単位の一括選択はフロント側で
    # 都道府県のチェックへ展開してから送られてくる(サーバ側にエリアの概念は持たない)
    prefs = filters.get("prefs") or []
    if isinstance(prefs, str):
        prefs = [prefs]
    prefs = [p for p in prefs if p in _ALLOWED_PREFS]
    if prefs:
        clauses.append("(" + " OR ".join(["pref=?"] * len(prefs)) + ")")
        params.extend(prefs)

    trades = filters.get("trades") or []
    if isinstance(trades, str):
        trades = [trades]
    trades = [t for t in trades if t in _ALLOWED_TRADES]
    if trades:
        clauses.append("(" + " OR ".join(["trades LIKE ?"] * len(trades)) + ")")
        params.extend(f"%{t}%" for t in trades)

    ranks = filters.get("ranks") or []
    if isinstance(ranks, str):
        ranks = [ranks]
    ranks = [r for r in ranks if r in _ALLOWED_RANKS]
    if ranks:
        clauses.append("(" + " OR ".join(["rank=?"] * len(ranks)) + ")")
        params.extend(ranks)

    capital_max = filters.get("capital_max")
    if isinstance(capital_max, (int, float)) and capital_max > 0:
        clauses.append("(capital <= ? OR capital IS NULL)"); params.append(int(capital_max))

    if filters.get("hiring_now"):
        clauses.append("hiring_now=1")
    if filters.get("has_website"):
        clauses.append("has_website=1")
    if filters.get("contact_ready"):
        clauses.append("contact_url IS NOT NULL")

    return " AND ".join(clauses), params


def preview_filter(con, tenant_id, filters, sample_limit=10):
    where, params = build_filter_sql(tenant_id, filters)
    total = con.execute(f"SELECT COUNT(*) FROM companies WHERE {where}", params).fetchone()[0]
    sample = con.execute(
        f"SELECT id, name, pref, rank, trades FROM companies WHERE {where} LIMIT ?",
        params + [sample_limit]).fetchall()
    return {"count": min(total, MAX_LIST_SIZE), "count_before_cap": total,
            "capped": total > MAX_LIST_SIZE, "sample": [dict(r) for r in sample]}


def create_from_filter(con, tenant_id, name, filters):
    where, params = build_filter_sql(tenant_id, filters)
    ids = [r[0] for r in con.execute(
        f"SELECT id FROM companies WHERE {where} LIMIT ?", params + [MAX_LIST_SIZE]).fetchall()]
    now = datetime.now().isoformat(timespec="seconds")
    cur = con.execute("""INSERT INTO target_lists
        (tenant_id,name,source,filter_json,company_count,created_at) VALUES (?,?,?,?,?,?)""",
        (tenant_id, name, "filter", json.dumps(filters, ensure_ascii=False), len(ids), now))
    list_id = cur.lastrowid
    con.executemany("INSERT OR IGNORE INTO target_list_members (list_id, company_id) VALUES (?,?)",
                     [(list_id, cid) for cid in ids])
    con.commit()
    return {"list_id": list_id, "count": len(ids)}


# CSVの列名ゆれを吸収する(顧客が用意するファイルなので厳密な形式を要求しない)
_NAME_COLS = ["name", "会社名", "企業名", "法人名", "商号"]
_PREF_COLS = ["pref", "都道府県", "県"]
_PHONE_COLS = ["phone", "電話番号", "tel"]
_EMAIL_COLS = ["email", "メール", "メールアドレス"]
_URL_COLS = ["website_url", "url", "hp", "ホームページ", "サイト"]


def _pick(row, cols):
    for c in cols:
        for k in row:
            if k and k.strip().lower() == c.lower():
                v = (row[k] or "").strip()
                if v:
                    return v
    return None


def create_from_csv(con, tenant_id, name, csv_text):
    """顧客が持ち込む企業リストを取り込む。既存の共有マスタ or 自テナントの
    既存データと商号(正規化)+都道府県が一致すればそこに寄せ、無ければ
    owner_tenant_id=自分のtenant_idの新規companyとして追加する
    (=他テナントには一切見えない非公開データになる)。"""
    import db

    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)[:MAX_CSV_ROWS]
    if not rows:
        return {"error": "CSVにデータ行がありません"}

    now = datetime.now().isoformat(timespec="seconds")
    cur = con.execute("""INSERT INTO target_lists
        (tenant_id,name,source,filter_json,company_count,created_at) VALUES (?,?,?,?,?,?)""",
        (tenant_id, name, "csv", None, 0, now))
    list_id = cur.lastrowid

    matched = created = skipped = 0
    for row in rows:
        raw_name = _pick(row, _NAME_COLS)
        if not raw_name:
            skipped += 1
            continue
        pref = _pick(row, _PREF_COLS)
        name_norm = db.normalize_name(raw_name)

        # pref未入力の行は商号一致だけで照合する(prefがNULLなら`pref=? OR ? IS NULL`がTRUEになる)
        existing = con.execute("""SELECT id FROM companies
            WHERE name_norm=? AND (pref=? OR ? IS NULL)
              AND dedup_of IS NULL AND (owner_tenant_id IS NULL OR owner_tenant_id=?)
            LIMIT 1""", (name_norm, pref, pref, tenant_id)).fetchone()
        if existing:
            cid = existing["id"]
            matched += 1
        else:
            cur2 = con.execute("""INSERT INTO companies
                (name, name_norm, pref, phone, email, website_url, data_source, owner_tenant_id)
                VALUES (?,?,?,?,?,?,?,?)""",
                (raw_name, name_norm, pref, _pick(row, _PHONE_COLS), _pick(row, _EMAIL_COLS),
                 _pick(row, _URL_COLS), "customer_upload", tenant_id))
            cid = cur2.lastrowid
            created += 1
        con.execute("INSERT OR IGNORE INTO target_list_members (list_id, company_id) VALUES (?,?)",
                     (list_id, cid))

    total = matched + created
    con.execute("UPDATE target_lists SET company_count=? WHERE id=?", (total, list_id))
    con.commit()
    return {"list_id": list_id, "count": total, "matched_existing": matched,
            "new_companies": created, "skipped_rows": skipped}


def list_lists(con, tenant_id):
    rows = con.execute("""SELECT id, name, source, company_count, created_at
        FROM target_lists WHERE tenant_id=? ORDER BY id DESC""", (tenant_id,)).fetchall()
    return [dict(r) for r in rows]


def get_list(con, tenant_id, list_id, limit=200, offset=0):
    """テナント境界を必ずここで確認する。list_idだけを信じてtenant_id一致を
    省略すると、他テナントがIDを推測して中身を覗けてしまう。"""
    lst = con.execute("SELECT * FROM target_lists WHERE id=? AND tenant_id=?",
                       (list_id, tenant_id)).fetchone()
    if not lst:
        return None
    members = con.execute("""SELECT c.id, c.name, c.pref, c.rank, c.trades, c.phone, c.email,
            c.website_url, c.contact_url
        FROM target_list_members m JOIN companies c ON c.id=m.company_id
        WHERE m.list_id=? LIMIT ? OFFSET ?""", (list_id, limit, offset)).fetchall()
    return {"list": dict(lst), "members": [dict(r) for r in members]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-key", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("preview")
    p.add_argument("--pref", action="append", default=[])
    p.add_argument("--trade", action="append", default=[])
    p.add_argument("--rank", action="append", default=[])
    args = ap.parse_args()

    import db, offers as OF
    con = db.connect(); db.migrate(con)
    tenant = OF.resolve_tenant_by_key(con, args.api_key)
    if not tenant:
        raise SystemExit("api_keyが不正です")

    if args.cmd == "list":
        for l in list_lists(con, tenant["id"]):
            print(f"  [{l['id']}] {l['name']} ({l['source']}) {l['company_count']}社 {l['created_at']}")
    elif args.cmd == "preview":
        filters = {"prefs": args.pref, "trades": args.trade, "ranks": args.rank}
        res = preview_filter(con, tenant["id"], filters)
        print(f"該当 {res['count_before_cap']}社" + ("(上限20,000件でカット)" if res["capped"] else ""))
        for s in res["sample"]:
            print(f"  {s['name']} ({s['pref']})")
