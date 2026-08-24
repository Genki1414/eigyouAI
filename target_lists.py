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
from datetime import datetime, timedelta

import config as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS target_lists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  source TEXT NOT NULL,          -- 'filter' | 'csv'
  filter_json TEXT,              -- source='filter'の場合の条件(再現・監査用)
  company_count INTEGER DEFAULT 0,
  campaign_id INTEGER,           -- send_list()で一度送信すると紐づく。二重クリックで
                                  -- 別キャンペーンが増殖しないよう、以後はこれを使い回す
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

-- MIKOMERUの「CSV検索ログ」相当。リスト取得(フィルタ)・CSV検索(会社名/URL)を
-- 実行するたびに1行記録する。target_listsと違い、検索しただけではリストにならない
-- (MIKOMERUの「検索しただけではリストとして保存されない」仕様と同じ)。
-- 実際にリストへ保存するのはsave_search_log_as_list()を呼んだ時だけ。
CREATE TABLE IF NOT EXISTS search_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  kind TEXT NOT NULL,             -- 'filter' | 'csv_name' | 'csv_url'
  label TEXT NOT NULL,            -- 検索条件の要約(絞込条件の説明文 or アップロードしたファイル名)
  filters_json TEXT,              -- kind='filter'の場合の条件(再現用。保存時に再実行する)
  company_ids_json TEXT NOT NULL, -- 見つかった会社ID(成功分)のJSON配列。保存時にそのまま使う
  success_count INTEGER DEFAULT 0,
  not_found_count INTEGER DEFAULT 0,  -- CSVの行のうち一致する会社が見つからなかった数
  no_contact_count INTEGER DEFAULT 0, -- 見つかったが問い合わせページ未確定の数
  csv_rows_json TEXT,             -- kind='csv_*'の場合、ダウンロード用に元CSV行+照合結果を保持
  status TEXT NOT NULL DEFAULT 'DONE',  -- 現状は同期処理のため常にDONE。将来の非同期化に備えて残す
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_search_log_tenant ON search_log(tenant_id);
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


def create_from_filter(con, tenant_id, name, filters, existing_list_id=None):
    """existing_list_idを渡すと新規リストを作らず、そのリストへ追加する
    (MIKOMERUの「リスト保存」モーダルの「既存のリストに追加する」相当)。"""
    where, params = build_filter_sql(tenant_id, filters)
    ids = [r[0] for r in con.execute(
        f"SELECT id FROM companies WHERE {where} LIMIT ?", params + [MAX_LIST_SIZE]).fetchall()]
    if existing_list_id:
        result = add_members_to_list(con, tenant_id, existing_list_id, ids)
        if result is None:
            return {"error": "指定されたリストが見つかりません"}
        return result
    now = datetime.now().isoformat(timespec="seconds")
    cur = con.execute("""INSERT INTO target_lists
        (tenant_id,name,source,filter_json,company_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
        (tenant_id, name, "filter", json.dumps(filters, ensure_ascii=False), len(ids), now, now))
    list_id = cur.lastrowid
    con.executemany("""INSERT OR IGNORE INTO target_list_members
        (list_id, company_id, send_status, created_at, updated_at) VALUES (?,?,'PENDING',?,?)""",
                     [(list_id, cid, now, now) for cid in ids])
    con.commit()
    return {"list_id": list_id, "count": len(ids)}


def run_filter_search(con, tenant_id, filters, sample_limit=200):
    """MIKOMERUの「リストを取得する」の[検索]実行相当。件数と結果テーブル(先頭
    sample_limit件)を返す。MIKOMERUのCSV検索ログはCSV検索(会社名/URL)専用で
    このフィルタ絞込には無いため、こちらはsearch_logへは記録しない(preview_filter()
    と同じ計算だが、ライブ絞込中の軽いプレビューではなく[検索]ボタンを押した時の
    本検索として、結果テーブルに使うのに十分な件数を返す点だけが違う)。"""
    return preview_filter(con, tenant_id, filters, sample_limit=sample_limit)


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


# URLで実際にサイトを開いて問い合わせページを探す(discover_urls=True)のは
# 1件ずつPlaywrightでブラウザを起動する重い処理のため、暴走・誤操作の被害を
# 抑える保守的な上限を設ける(MAX_CSV_ROWSとは別枠)。
MAX_URL_DISCOVERY_ROWS = 30


def create_from_csv(con, tenant_id, name, csv_text, discover_urls=False, existing_list_id=None):
    """顧客が持ち込む企業リストを取り込む。既存の共有マスタ or 自テナントの
    既存データと商号(正規化)+都道府県が一致すればそこに寄せ、無ければ
    owner_tenant_id=自分のtenant_idの新規companyとして追加する
    (=他テナントには一切見えない非公開データになる)。

    discover_urls=True(MIKOMERUの「CSV検索(URLで検索)」相当)にすると、
    CSVにURL列があり、かつcontact_url未確定の企業について、実際にそのURLへ
    アクセスして問い合わせページを探す(form_navigator.discover_contact_url()。
    フォームへの入力・送信は一切行わない、閲覧のみの探索)。1件ずつ実ブラウザを
    起動する重い処理のためMAX_URL_DISCOVERY_ROWS件までしか行わない
    (超過分はurl_discoveryのskipped_over_limitに件数を残す。黙って切り捨てない)。

    existing_list_idを渡すと新規リストを作らず、そのリストへ追加する
    (MIKOMERUの「リスト保存」モーダルの「既存のリストに追加する」相当)。"""
    import db

    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)[:MAX_CSV_ROWS]
    if not rows:
        return {"error": "CSVにデータ行がありません"}

    now = datetime.now().isoformat(timespec="seconds")
    if existing_list_id:
        owns = con.execute("SELECT 1 FROM target_lists WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                            (existing_list_id, tenant_id)).fetchone()
        if not owns:
            return {"error": "指定されたリストが見つかりません"}
        list_id = existing_list_id
    else:
        cur = con.execute("""INSERT INTO target_lists
            (tenant_id,name,source,filter_json,company_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
            (tenant_id, name, "csv", None, 0, now, now))
        list_id = cur.lastrowid

    matched = created = skipped = 0
    url_candidates = []  # discover_urls=True時に後段でクロールする(company_id, url)
    for row in rows:
        raw_name = _pick(row, _NAME_COLS)
        if not raw_name:
            skipped += 1
            continue
        pref = _pick(row, _PREF_COLS)
        name_norm = db.normalize_name(raw_name)
        row_url = _pick(row, _URL_COLS)

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
                 row_url, "customer_upload", tenant_id))
            cid = cur2.lastrowid
            created += 1
        con.execute("""INSERT OR IGNORE INTO target_list_members
            (list_id, company_id, send_status, created_at, updated_at)
            VALUES (?,?,'PENDING',?,?)""", (list_id, cid, now, now))
        if row_url:
            url_candidates.append((cid, row_url))

    con.execute("""UPDATE target_lists SET
        company_count=(SELECT COUNT(*) FROM target_list_members WHERE list_id=?),
        updated_at=? WHERE id=?""", (list_id, now, list_id))
    con.commit()
    total = con.execute("SELECT company_count FROM target_lists WHERE id=?", (list_id,)).fetchone()[0]

    result = {"list_id": list_id, "count": total, "matched_existing": matched,
              "new_companies": created, "skipped_rows": skipped}
    if discover_urls and url_candidates:
        result["url_discovery"] = _discover_contact_urls(con, url_candidates)
    return result


def _discover_contact_urls(con, candidates):
    """discover_urls=True時にcreate_from_csv()から呼ばれる。既にcontact_urlが
    確定済みの企業は実クロールせずスキップする(無駄なアクセスをしない)。"""
    import form_navigator as FN

    to_crawl = []
    for cid, url in candidates:
        row = con.execute("SELECT contact_url FROM companies WHERE id=?", (cid,)).fetchone()
        if row and not row["contact_url"]:
            to_crawl.append((cid, url))

    skipped_over_limit = max(0, len(to_crawl) - MAX_URL_DISCOVERY_ROWS)
    to_crawl = to_crawl[:MAX_URL_DISCOVERY_ROWS]

    found = no_form = unreachable = error = 0
    for cid, url in to_crawl:
        res = FN.discover_contact_url(url)
        if res["status"] == "FOUND":
            con.execute("UPDATE companies SET contact_url=?, has_contact_form=1 WHERE id=?",
                        (res["contact_url"], cid))
            found += 1
        elif res["status"] == "NO_FORM":
            no_form += 1
        elif res["status"] == "UNREACHABLE":
            unreachable += 1
        else:
            error += 1
    con.commit()
    return {"found": found, "no_form": no_form, "unreachable": unreachable,
            "error": error, "skipped_over_limit": skipped_over_limit}


def run_csv_search(con, tenant_id, filename, csv_text, mode="name", name_col=None, url_col=None,
                    pref_col=None):
    """MIKOMERUの「CSV検索でリストを取得する」相当。「会社名で検索」「URLで検索」の
    2モードがあり、検索しただけではリストにならず、まずsearch_logへ記録する
    (保存は別途save_search_log_as_list()を呼ぶ)。

    MIKOMERUとの意図的な違い: MIKOMERUは自社が保有する会社基本情報DBを検索するだけで、
    一致しない行(「会社不明」)は何も作られない。AshiBaseの「CSV検索」は元々
    「自社の企業リストを取り込む」機能(MIKOMERUに無い独自機能)を兼ねているため、
    一致しない行は御社専用の非公開企業として新規に追加する。これは仕様の劣化ではなく、
    自社保有リストを送信対象にできるというAshiBase側の価値をそのまま残すための設計判断。

    mode='url'の場合は、name_col/url_colで指定した列を使い、問い合わせページの探索
    (discover_contact_url)を必ず行う(MIKOMERUの「URLで検索」が問い合わせページURLの
    発見を目的にしているのと同じ)。"""
    import db

    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)[:MAX_CSV_ROWS]
    if not rows:
        return {"error": "CSVにデータ行がありません"}

    now = datetime.now().isoformat(timespec="seconds")
    company_ids = []
    csv_rows_detail = []
    matched = created = skipped = 0
    url_candidates = []

    for row in rows:
        raw_name = ((row.get(name_col) or "").strip() if name_col else _pick(row, _NAME_COLS))
        if not raw_name:
            skipped += 1
            csv_rows_detail.append({"row": row, "status": "not_found"})
            continue
        pref = ((row.get(pref_col) or "").strip() or None if pref_col else _pick(row, _PREF_COLS))
        name_norm = db.normalize_name(raw_name)
        row_url = ((row.get(url_col) or "").strip() if url_col else _pick(row, _URL_COLS))

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
                 row_url, "customer_upload", tenant_id))
            cid = cur2.lastrowid
            created += 1
        company_ids.append(cid)
        csv_rows_detail.append({"row": row, "status": "success", "company_id": cid, "name": raw_name})
        if row_url:
            url_candidates.append((cid, row_url))

    url_discovery = None
    if mode == "url" and url_candidates:
        url_discovery = _discover_contact_urls(con, url_candidates)

    no_contact = 0
    if company_ids:
        placeholders = ",".join("?" * len(company_ids))
        no_contact = con.execute(
            f"SELECT COUNT(*) FROM companies WHERE id IN ({placeholders}) AND contact_url IS NULL",
            company_ids).fetchone()[0]

    label = f"ファイル名: {filename}" if filename else "CSV検索"
    cur = con.execute("""INSERT INTO search_log
        (tenant_id, kind, label, filters_json, company_ids_json, success_count,
         not_found_count, no_contact_count, csv_rows_json, status, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tenant_id, "csv_url" if mode == "url" else "csv_name", label, None,
         json.dumps(company_ids), len(company_ids), skipped, no_contact,
         json.dumps(csv_rows_detail, ensure_ascii=False), "DONE", now, now))
    con.commit()

    result = {"search_log_id": cur.lastrowid, "count": len(company_ids),
              "matched_existing": matched, "new_companies": created, "skipped_rows": skipped,
              "no_contact_count": no_contact}
    if url_discovery:
        result["url_discovery"] = url_discovery
    return result


def list_search_log(con, tenant_id, limit=200):
    rows = con.execute("""SELECT id, kind, label, success_count, not_found_count, no_contact_count,
        status, created_at, updated_at FROM search_log WHERE tenant_id=?
        ORDER BY id DESC LIMIT ?""", (tenant_id, limit)).fetchall()
    return [dict(r) for r in rows]


def get_search_log(con, tenant_id, search_log_id):
    row = con.execute("SELECT * FROM search_log WHERE id=? AND tenant_id=?",
                       (search_log_id, tenant_id)).fetchone()
    if not row:
        return None
    d = dict(row)
    company_ids = json.loads(d["company_ids_json"] or "[]")
    companies = []
    if company_ids:
        placeholders = ",".join("?" * len(company_ids))
        crows = con.execute(f"""SELECT id, name, pref, corporate_no, trades, rank, capital,
            contact_url, has_contact_form FROM companies WHERE id IN ({placeholders})""",
            company_ids).fetchall()
        by_id = {r["id"]: dict(r) for r in crows}
        companies = [by_id[cid] for cid in company_ids if cid in by_id]
    d["companies"] = companies
    d["filters"] = json.loads(d["filters_json"]) if d["filters_json"] else None
    d["csv_rows"] = json.loads(d["csv_rows_json"]) if d["csv_rows_json"] else None
    return d


def save_search_log_as_list(con, tenant_id, search_log_id, name=None, existing_list_id=None):
    """MIKOMERUの検索ログ詳細「リスト保存」相当。検索ログはCSV検索(会社名/URL)
    専用のため(リスト取得のフィルタ絞込にはMIKOMERUにもログが無い)、常に検索時点の
    company_idsをそのまま使う(CSVそのものは既に失われているため再実行できない)。"""
    log = con.execute("SELECT * FROM search_log WHERE id=? AND tenant_id=?",
                       (search_log_id, tenant_id)).fetchone()
    if not log:
        return {"error": "検索ログが見つかりません"}
    if not name and not existing_list_id:
        return {"error": "リスト名を入力するか、既存のリストを選択してください"}

    company_ids = json.loads(log["company_ids_json"] or "[]")
    if not company_ids:
        return {"error": "このログには保存できる企業がありません"}
    if existing_list_id:
        result = add_members_to_list(con, tenant_id, existing_list_id, company_ids)
        if result is None:
            return {"error": "指定されたリストが見つかりません"}
        return result
    now = datetime.now().isoformat(timespec="seconds")
    cur = con.execute("""INSERT INTO target_lists
        (tenant_id,name,source,filter_json,company_count,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?)""",
        (tenant_id, name, "csv", None, len(company_ids), now, now))
    list_id = cur.lastrowid
    con.executemany("""INSERT OR IGNORE INTO target_list_members
        (list_id, company_id, send_status, created_at, updated_at) VALUES (?,?,'PENDING',?,?)""",
        [(list_id, cid, now, now) for cid in company_ids])
    con.commit()
    return {"list_id": list_id, "count": len(company_ids)}


def list_lists(con, tenant_id, include_deleted=False):
    where = "tenant_id=?" if include_deleted else "tenant_id=? AND deleted_at IS NULL"
    rows = con.execute(f"""SELECT id, name, source, company_count, created_at, updated_at, deleted_at
        FROM target_lists WHERE {where} ORDER BY id DESC""", (tenant_id,)).fetchall()
    return [dict(r) for r in rows]


def rename_list(con, tenant_id, list_id, name):
    """MIKOMERUの保存済みリスト詳細画面の「編集」相当(リスト名の変更のみ)。"""
    now = datetime.now().isoformat(timespec="seconds")
    cur = con.execute("""UPDATE target_lists SET name=?, updated_at=?
        WHERE id=? AND tenant_id=? AND deleted_at IS NULL""", (name, now, list_id, tenant_id))
    con.commit()
    return cur.rowcount > 0


def set_lists_deleted(con, tenant_id, list_ids, deleted):
    """複数リストのソフト削除/復元をまとめて行う(MIKOMERUのチェックボックス一括削除・復元相当)。
    物理削除はしない: target_list_members/form_send_log等から参照され続けるため、
    消してしまうと送信履歴の追跡ができなくなる。"""
    if not list_ids:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    value = now if deleted else None
    qmarks = ",".join("?" * len(list_ids))
    cur = con.execute(f"""UPDATE target_lists SET deleted_at=?, updated_at=?
        WHERE tenant_id=? AND id IN ({qmarks})""", [value, now, tenant_id] + list(list_ids))
    con.commit()
    return cur.rowcount


def duplicate_list(con, tenant_id, list_id, new_name):
    """MIKOMERUの保存済みリスト詳細画面「複製」相当。フィルタ条件ではなく
    現時点のメンバー(会社)をそのままコピーする(元リストへの送信結果等の
    履歴には影響しない、新規の別リストとして独立させる)。"""
    src = con.execute("SELECT * FROM target_lists WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                       (list_id, tenant_id)).fetchone()
    if not src:
        return None
    now = datetime.now().isoformat(timespec="seconds")
    cur = con.execute("""INSERT INTO target_lists
        (tenant_id,name,source,filter_json,company_count,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?)""",
        (tenant_id, new_name, src["source"], src["filter_json"], src["company_count"], now, now))
    new_id = cur.lastrowid
    con.execute("""INSERT INTO target_list_members (list_id, company_id, send_status, created_at, updated_at)
        SELECT ?, company_id, 'PENDING', ?, ? FROM target_list_members WHERE list_id=?""",
        (new_id, now, now, list_id))
    con.commit()
    return {"list_id": new_id, "count": src["company_count"]}


def remove_members(con, tenant_id, list_id, company_ids):
    """リストから個別の会社を除外する(MIKOMERUの保存済みリスト詳細画面
    「リスト企業の個別削除」相当)。会社そのもの・送信履歴は消さない。"""
    owns = con.execute("SELECT 1 FROM target_lists WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                        (list_id, tenant_id)).fetchone()
    if not owns or not company_ids:
        return 0
    qmarks = ",".join("?" * len(company_ids))
    cur = con.execute(f"""DELETE FROM target_list_members WHERE list_id=?
        AND company_id IN ({qmarks})""", [list_id] + list(company_ids))
    removed = cur.rowcount
    if removed:
        now = datetime.now().isoformat(timespec="seconds")
        con.execute("""UPDATE target_lists SET
            company_count=(SELECT COUNT(*) FROM target_list_members WHERE list_id=?),
            updated_at=? WHERE id=?""", (list_id, now, list_id))
    con.commit()
    return removed


def add_members_to_list(con, tenant_id, list_id, company_ids):
    """フィルタ検索・CSV取込の結果を、新規リストではなく既存リストへ追加する
    (MIKOMERUの「リスト保存」モーダルの「既存のリストに追加する」相当)。"""
    owns = con.execute("SELECT 1 FROM target_lists WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                        (list_id, tenant_id)).fetchone()
    if not owns:
        return None
    now = datetime.now().isoformat(timespec="seconds")
    con.executemany("""INSERT OR IGNORE INTO target_list_members
        (list_id, company_id, send_status, created_at, updated_at) VALUES (?,?,'PENDING',?,?)""",
        [(list_id, cid, now, now) for cid in company_ids])
    con.execute("""UPDATE target_lists SET
        company_count=(SELECT COUNT(*) FROM target_list_members WHERE list_id=?),
        updated_at=? WHERE id=?""", (list_id, now, list_id))
    con.commit()
    count = con.execute("SELECT company_count FROM target_lists WHERE id=?", (list_id,)).fetchone()[0]
    return {"list_id": list_id, "count": count}


_MEMBER_STATUS_FILTERS = {
    "success": "m.send_status='SUCCESS'",
    "failed": "m.send_status IN ('FAILED_RETRYABLE','FAILED_UNSUPPORTED')",
    "skip": "m.send_status='SKIP'",
    "pending": "m.send_status='PENDING'",
    "replied": "m.replied=1",
    "deal": "m.deal=1",
    "won": "m.won=1",
}


def get_list(con, tenant_id, list_id, limit=200, offset=0, status_filter=None):
    """テナント境界を必ずここで確認する。list_idだけを信じてtenant_id一致を
    省略すると、他テナントがIDを推測して中身を覗けてしまう。
    status_filterは_MEMBER_STATUS_FILTERSのキーのみ受け付ける(SQLインジェクション
    防止のため、フリーテキストでの絞込条件は組み立てない)。"""
    lst = con.execute("SELECT * FROM target_lists WHERE id=? AND tenant_id=?",
                       (list_id, tenant_id)).fetchone()
    if not lst:
        return None
    where = "m.list_id=?"
    params = [list_id]
    if status_filter in _MEMBER_STATUS_FILTERS:
        where += " AND " + _MEMBER_STATUS_FILTERS[status_filter]
    members = con.execute(f"""SELECT c.id, c.name, c.pref, c.rank, c.trades, c.phone, c.email,
            c.website_url, c.contact_url,
            m.send_status, m.reason_code, m.retry_count, m.last_error, m.latest_result,
            m.started_at, m.completed_at, m.contacted_at,
            m.replied, m.replied_at, m.deal, m.deal_at, m.won, m.won_at, m.memo
        FROM target_list_members m JOIN companies c ON c.id=m.company_id
        WHERE {where} ORDER BY m.company_id LIMIT ? OFFSET ?""",
        params + [limit, offset]).fetchall()
    return {"list": dict(lst), "members": [dict(r) for r in members]}


def send_list(con, tenant_id, list_id, subject, body, dry_run=True, track_clicks=False,
              sender_template_id=None, staff_id=None, allow_no_solicit=False,
              sender_override=None, cancel_recent_days=None):
    """保存済みリストからフォーム自動送信キャンペーンを作り、既存のsenders.send_campaign()
    にそのまま委譲する。can_contact()・冪等性・FormSenderのペーシング上限はすべて
    send_campaign()側の仕組みがそのまま効く(ここで独自の送信経路は作らない)。

    二重クリック対策: このリストが初めて送信される時だけ新しいcampaignを作り、
    以後はtarget_lists.campaign_idに記録した同じcampaignを使い回す。同じ
    campaign_idへtouchesを追加してもUNIQUE(campaign_id, company_id, step)と
    INSERT OR IGNOREで重複しない。既に送信済み(sent_at IS NOT NULL)の行は
    send_campaign()側のWHERE条件で自動的に対象から外れるため、再送信を押しても
    未送信分だけがもう一度試される(リトライにはなるが二重送信にはならない)。

    allow_no_solicit/sender_overrideはそのままsenders.send_campaign()へ渡す
    (MIKOMERUの「営業拒否サイトへの送信」「送信元テンプレートの内容を送信直前に
    上書きする」相当。詳細はsend_campaign()のdocstring参照)。

    cancel_recent_days: MIKOMERUの「過去送信対象キャンセル(期間指定可)」相当。
    指定した日数以内に(このリストに限らず)実送信済みの会社は、今回の送信対象から
    除外する(ドライランでの送信は対象にしない。can_contact()の生涯上限・最短間隔
    ガードとは別の、ユーザーが都度選べる追加フィルタという位置づけ)。
    """
    import senders
    import db

    lst = con.execute("SELECT * FROM target_lists WHERE id=? AND tenant_id=?",
                       (list_id, tenant_id)).fetchone()
    if not lst:
        return None

    offer = con.execute("SELECT id FROM offers WHERE tenant_id=? ORDER BY id LIMIT 1",
                         (tenant_id,)).fetchone()
    if not offer:
        return {"error": "このテナントにはオファーが設定されていません。管理者に連絡してください"}

    # フォーム自動送信は問い合わせURLが分かっている企業にしか行えない
    members = con.execute("""SELECT c.id FROM target_list_members m
        JOIN companies c ON c.id = m.company_id
        WHERE m.list_id=? AND c.contact_url IS NOT NULL""", (list_id,)).fetchall()
    if not members:
        return {"error": "このリストにはフォーム送信可能な企業がありません"
                          "(問い合わせURLが確認できた企業のみ送信対象になります)"}

    cancelled_recent = 0
    if cancel_recent_days:
        cutoff = (datetime.now() - timedelta(days=cancel_recent_days)).isoformat(timespec="seconds")
        member_ids = [m["id"] for m in members]
        placeholders = ",".join("?" * len(member_ids))
        recently_sent = {row["company_id"] for row in con.execute(
            f"""SELECT DISTINCT company_id FROM touches
                WHERE company_id IN ({placeholders}) AND sent_at IS NOT NULL AND sent_at>=?
                  AND instr(COALESCE(note,''), 'provider_id=mock_')=0""",
            member_ids + [cutoff]).fetchall()}
        if recently_sent:
            cancelled_recent = len(recently_sent)
            members = [m for m in members if m["id"] not in recently_sent]
        if not members:
            return {"error": f"過去送信対象キャンセルにより、送信可能な企業がありません"
                              f"(直近{cancel_recent_days}日以内に送信済み)"}

    if lst["campaign_id"]:
        campaign_id = lst["campaign_id"]
    else:
        # 「まだcampaign_idが無ければ作る」はcheck-then-actなので、同じリストへの
        # 2つの同時リクエスト(ボタン連打・2人の担当者)が両方とも「無い」と判定して
        # 別々のcampaignを作り、結果的に同じ企業へ二重に実送信してしまう恐れがある。
        # campaign行の作成自体は先にしてよいが、target_lists側への「採用」は
        # UPDATE...WHERE campaign_id IS NULLで原子的に行い、負けた側は自分の
        # campaignを捨てて勝者のcampaign_idを使う(負けたcampaign行はtouchesが
        # 紐付かないまま残るだけで実害は無い)。
        now = datetime.now().isoformat(timespec="seconds")
        cur = con.execute("""INSERT INTO campaigns (name, started_at, target_rule, offer_id)
            VALUES (?,?,?,?)""",
            (f"[リスト送信] {lst['name']}", now, f"target_list:{list_id}", offer["id"]))
        new_campaign_id = cur.lastrowid
        con.commit()
        claimed = con.execute(
            "UPDATE target_lists SET campaign_id=? WHERE id=? AND campaign_id IS NULL",
            (new_campaign_id, list_id)).rowcount
        con.commit()
        if claimed:
            campaign_id = new_campaign_id
        else:
            campaign_id = con.execute("SELECT campaign_id FROM target_lists WHERE id=?",
                                       (list_id,)).fetchone()["campaign_id"]

    # 「自動送信ログ」一覧(MIKOMERU同等。1リスト=1実行として集計表示する)用の
    # スナップショット。誰が・どの送信元で・いつ実行したかをtarget_listsへ記録する。
    # dry_run/本番どちらでも更新する(既存のform_send_log自体、両方に対して
    # 記録される設計に合わせる。押すたびに最新の実行内容へ上書きする)。
    con.execute("""UPDATE target_lists SET sent_by_staff_id=?, sent_sender_template_id=?,
        last_send_started_at=? WHERE id=?""",
        (staff_id, sender_template_id, datetime.now().isoformat(timespec="seconds"), list_id))
    con.commit()

    now2 = datetime.now().isoformat(timespec="seconds")
    for m in members:
        # INSERT OR IGNOREだけだと、ドライランで一度作られた行の件名・本文が
        # 以後上書きされず古いまま残ってしまう(まだ本番送信していない行に限り、
        # 直前に画面で入力した最新の件名・本文へ更新する)。
        con.execute("""INSERT INTO touches
            (campaign_id, company_id, channel, variant, step, subject, body)
            VALUES (?,?,'フォーム','A',1,?,?)
            ON CONFLICT(campaign_id, company_id, step) DO UPDATE SET
                subject=excluded.subject, body=excluded.body
            WHERE touches.sent_at IS NULL OR instr(touches.note, 'provider_id=mock_') > 0""",
            (campaign_id, m["id"], subject, body))
    if not dry_run:
        # 実送信の直前に「処理中」を記録しておく。サーバー再起動等で送信が
        # 途中で止まった場合でも、PROCESSINGのまま残った行=結果不明な行として
        # 後から目視で気づけるようにするため(PENDINGのままだと「未着手」と
        # 「処理中に落ちた」の区別がつかない)。send_campaign()が今回実際に
        # 対象とする行(sent_atがまだ無い行)だけに絞る。既に送信済み(SUCCESS等)の
        # 行まで一律PROCESSINGへ戻すと、今回の対象外なのに更新されないまま
        # 「処理中」で止まって見えてしまう。
        pending_ids = {row["company_id"] for row in con.execute(
            "SELECT company_id FROM touches WHERE campaign_id=? AND step=1 AND sent_at IS NULL",
            (campaign_id,)).fetchall()}
        con.executemany("""UPDATE target_list_members SET send_status='PROCESSING',
            started_at=?, updated_at=? WHERE list_id=? AND company_id=?""",
            [(now2, now2, list_id, m["id"]) for m in members if m["id"] in pending_ids])
    con.commit()

    stats = senders.send_campaign(con, campaign_id, step=1, dry_run=dry_run,
                                   track_clicks=track_clicks, sender_template_id=sender_template_id,
                                   allow_no_solicit=allow_no_solicit, sender_override=sender_override)
    if not dry_run:
        db.sync_target_list_member_status(con, list_id, campaign_id, step=1)
        _notify_completion(con, tenant_id, lst["name"], len(members), stats)
    return {"campaign_id": campaign_id, "target_count": len(members),
            "dry_run": dry_run, "stats": stats, "cancelled_recent": cancelled_recent}


def _notify_completion(con, tenant_id, list_name, target_count, stats):
    """送信完了を担当者へメール通知する(MIKOMERU同等の完了通知)。
    実際のメール送信基盤(SendGrid等)はまだ未実装(HANDOFF.md T2)のため、
    今はsenders.MailSenderがNotImplementedErrorを投げるだけの状態——それでも
    ここで先に呼び出しておき、T2が実装された瞬間から追加のコード変更なしで
    通知が届き始めるようにする。あくまで補助機能なので、失敗しても
    (未実装であっても)呼び出し元の送信処理自体は絶対に止めない。"""
    import senders

    recipients = [r["email"] for r in con.execute(
        "SELECT email FROM staff WHERE tenant_id=? AND email IS NOT NULL AND email!=''",
        (tenant_id,)).fetchall()]
    if not recipients:
        tn = con.execute("SELECT sender_email FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if tn and tn["sender_email"]:
            recipients = [tn["sender_email"]]
    if not recipients:
        return

    subject = f"【AshiBase】送信完了: {list_name}"
    body = (f"リスト「{list_name}」への送信が完了しました。\n\n"
            f"対象企業数: {target_count}\n"
            f"送信成功: {stats.get('sent', 0)}\n"
            f"失敗: {stats.get('failed', 0)}\n"
            f"ガードで中止: {stats.get('blocked', 0)}\n"
            f"配信停止: {stats.get('suppressed', 0)}\n"
            f"Kill Switchで中止: {stats.get('stopped', 0)}\n")
    default_sender = senders.Sender(name="AshiBase（足場ベース）", email="info@ashibase.jp",
                                     address="", optout_url="https://ashibase.jp/optout")
    mailer = senders.MailSender(con, dry_run=False)
    for email in recipients:
        try:
            mailer._deliver(senders.Recipient(company_id=0, name="", email=email),
                            default_sender, subject, body)
        except NotImplementedError:
            # T2(メール送信実装)が完了するまでは想定内。cron.log等で気づけるよう
            # ログにだけ残し、呼び出し元(send_list())には一切伝播させない
            print(f"  [完了通知] メール送信基盤が未実装のため送信できません "
                  f"(宛先: {email}。HANDOFF.md T2参照)")
        except Exception as e:  # noqa: BLE001
            print(f"  [完了通知] 送信に失敗しました(宛先: {email}): {e}")


# MIKOMERUの「結果ステータス」(成功/失敗/フォームなし)に合わせた集計区分。
# form_send_log.statusはこちらより細かく原因を持つ(LOG_STATUS_LABELS参照)ため、
# 「フォームなし」だけreason_codeで判定し、それ以外の失敗系はまとめて「失敗」にする。
_EXEC_SUCCESS_SQL = "SUM(CASE WHEN l.status='SUCCESS' THEN 1 ELSE 0 END)"
_EXEC_NO_FORM_SQL = ("SUM(CASE WHEN l.status='FAILED_UNSUPPORTED' "
                      "AND l.reason_code='form_not_found' THEN 1 ELSE 0 END)")
_EXEC_FAILED_SQL = ("SUM(CASE WHEN l.status!='SUCCESS' AND NOT "
                     "(l.status='FAILED_UNSUPPORTED' AND l.reason_code='form_not_found') "
                     "THEN 1 ELSE 0 END)")


def list_send_executions(con, tenant_id, list_id=None, date_from=None, date_to=None):
    """MIKOMERUの「自動送信ログ」一覧相当(送信結果の会社別明細ではなく、
    「いつ・誰が・どのリストへ送ったか」という実行単位の集計)。既存設計
    (1リスト=1campaignを使い回す。send_list()参照)にそのまま乗せ、target_listsの
    1行=1実行として扱う。まだ一度も送信していないリスト(campaign_id IS NULL)は
    実行ログに出さない。"""
    where = "tl.tenant_id=? AND tl.campaign_id IS NOT NULL"
    params = [tenant_id]
    if list_id:
        where += " AND tl.id=?"
        params.append(list_id)
    if date_from:
        where += " AND tl.last_send_started_at>=?"
        params.append(date_from)
    if date_to:
        where += " AND tl.last_send_started_at<=?"
        params.append(date_to + "T23:59:59")

    rows = con.execute(f"""SELECT tl.id, tl.name, tl.campaign_id, tl.send_note,
            tl.last_send_started_at, tl.sent_by_staff_id, tl.sent_sender_template_id,
            tn.name tenant_name, tn.sender_name tenant_sender_name, tn.sender_email tenant_sender_email,
            st.name staff_name,
            sn.sender_last_name, sn.sender_first_name, sn.sender_name tmpl_sender_name,
            sn.sender_email tmpl_sender_email
        FROM target_lists tl
        JOIN tenants tn ON tn.id = tl.tenant_id
        LEFT JOIN staff st ON st.id = tl.sent_by_staff_id
        LEFT JOIN sender_templates sn ON sn.id = tl.sent_sender_template_id
        WHERE {where}
        ORDER BY tl.last_send_started_at DESC""", params).fetchall()

    out = []
    for r in rows:
        counts = con.execute(f"""SELECT COUNT(*) total, {_EXEC_SUCCESS_SQL} success,
                {_EXEC_FAILED_SQL} failed, {_EXEC_NO_FORM_SQL} no_form
            FROM form_send_log l WHERE l.list_id=?""", (r["id"],)).fetchone()
        clicks = con.execute("""SELECT COALESCE(SUM(email_click_count),0) clicks,
                MAX(email_clicked_at) last_clicked_at
            FROM touches WHERE campaign_id=?""", (r["campaign_id"],)).fetchone()
        sample = con.execute("SELECT subject, body FROM touches WHERE campaign_id=? LIMIT 1",
                              (r["campaign_id"],)).fetchone()

        if r["sender_last_name"] or r["sender_first_name"]:
            sender_last, sender_first = r["sender_last_name"] or "", r["sender_first_name"] or ""
        else:
            src = r["tmpl_sender_name"] if r["sent_sender_template_id"] else r["tenant_sender_name"]
            parts = (src or "").split(maxsplit=1)
            sender_last = parts[0] if parts else ""
            sender_first = parts[1] if len(parts) > 1 else ""
        sender_email = (r["tmpl_sender_email"] if r["sent_sender_template_id"] else r["tenant_sender_email"]) or ""

        out.append({
            "list_id": r["id"], "list_name": r["name"], "send_note": r["send_note"] or "",
            "staff_id": r["sent_by_staff_id"], "staff_name": r["staff_name"],
            "company_name": r["tenant_name"], "sender_last_name": sender_last,
            "sender_first_name": sender_first, "sender_email": sender_email,
            "subject": (sample["subject"] if sample else "") or "",
            "body_preview": ((sample["body"] if sample else "") or "")[:60],
            "success": counts["success"] or 0, "failed": counts["failed"] or 0,
            "no_form": counts["no_form"] or 0, "total": counts["total"] or 0,
            "click_count": clicks["clicks"] or 0, "last_clicked_at": clicks["last_clicked_at"],
            "started_at": r["last_send_started_at"],
        })
    return out


def update_send_note(con, tenant_id, list_id, note):
    """自動送信ログ一覧の「備考」(実行=リスト単位のメモ。会社ごとの
    form_send_log.noteとは別物)を更新する。他テナントのリストは更新できない。"""
    n = con.execute("UPDATE target_lists SET send_note=? WHERE id=? AND tenant_id=?",
                     (note, list_id, tenant_id)).rowcount
    con.commit()
    return n > 0


def activity_log(con, tenant_id, limit=100):
    """「その他ログ」画面向けの、テナントの操作履歴を時系列でまとめたもの。
    新しい記録用テーブルは作らず、既存のtarget_lists/campaignsをそのまま
    突き合わせて作る(リスト作成イベントと、初回送信のタイミングの2種類)。
    再送信は同じcampaign_idを使い回す仕様(send_list()参照)なので、
    「送信」イベントは初回送信時刻のみを表す(リストごとに1件)。"""
    events = []
    for l in con.execute("""SELECT id, name, source, company_count, created_at
            FROM target_lists WHERE tenant_id=?""", (tenant_id,)).fetchall():
        kind = "CSVから作成" if l["source"] == "csv" else "条件から作成"
        events.append({
            "at": l["created_at"], "type": "list_created",
            "detail": f"リスト「{l['name']}」を{kind}({l['company_count']:,}社)",
        })
    for r in con.execute("""SELECT tl.name, cp.started_at FROM target_lists tl
            JOIN campaigns cp ON cp.id = tl.campaign_id
            WHERE tl.tenant_id=?""", (tenant_id,)).fetchall():
        events.append({
            "at": r["started_at"], "type": "list_sent",
            "detail": f"リスト「{r['name']}」への送信を開始",
        })
    events.sort(key=lambda e: e["at"], reverse=True)
    return events[:limit]


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
