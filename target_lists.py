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


def send_list(con, tenant_id, list_id, subject, body, dry_run=True, track_clicks=False):
    """保存済みリストからフォーム自動送信キャンペーンを作り、既存のsenders.send_campaign()
    にそのまま委譲する。can_contact()・冪等性・FormSenderのペーシング上限はすべて
    send_campaign()側の仕組みがそのまま効く(ここで独自の送信経路は作らない)。

    二重クリック対策: このリストが初めて送信される時だけ新しいcampaignを作り、
    以後はtarget_lists.campaign_idに記録した同じcampaignを使い回す。同じ
    campaign_idへtouchesを追加してもUNIQUE(campaign_id, company_id, step)と
    INSERT OR IGNOREで重複しない。既に送信済み(sent_at IS NOT NULL)の行は
    send_campaign()側のWHERE条件で自動的に対象から外れるため、再送信を押しても
    未送信分だけがもう一度試される(リトライにはなるが二重送信にはならない)。
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
                                   track_clicks=track_clicks)
    if not dry_run:
        db.sync_target_list_member_status(con, list_id, campaign_id, step=1)
        _notify_completion(con, tenant_id, lst["name"], len(members), stats)
    return {"campaign_id": campaign_id, "target_count": len(members),
            "dry_run": dry_run, "stats": stats}


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
