"""
ingest_mikomeru.py — mikomeru保存済みリスト(CSV)の取込
既存companies.dbとは別データソース(法人番号ベースの業種横断ディレクトリ)。
国交省名簿は「とび・土工/塗装/解体」の許可業者のみだが、mikomeruは業種を問わない
一般的な建設業者ディレクトリで、ホームページ/問い合わせページ/フォーム有無が
ほぼ全件揃っている(=Webフォーム経由の営業チャネルに使える一次情報)。

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

# mikomeruの業種名(自由記述)からのtobi/kaitai/tosou推定変換。
# 国交省名簿のような許可情報ではなく事業内容の自己申告文言なので、あくまで参考値。
TRADE_KEYWORDS = {
    "tobi": ["とび", "土工"],
    "kaitai": ["解体"],
    "tosou": ["塗装"],
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
            cur = con.execute("SELECT website_url, has_website FROM companies WHERE id=?",
                               (rep_id,)).fetchone()
            new_website = cur["website_url"] or website_url
            new_has_website = 1 if (cur["has_website"] == 1 or website_url) else cur["has_website"]
            con.execute("""UPDATE companies SET
                website_url=?, has_website=?,
                contact_url=COALESCE(NULLIF(contact_url,''), ?),
                has_contact_form=COALESCE(has_contact_form, ?),
                corporate_no=COALESCE(NULLIF(corporate_no,''), ?)
                WHERE id=?""",
                (new_website, new_has_website, contact_url, has_form, corporate_no, rep_id))
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
