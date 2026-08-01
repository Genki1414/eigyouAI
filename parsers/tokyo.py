"""
parsers/tokyo.py — 東京都「建設業許可業者名簿」Excelパーサー
出典: 東京都都市整備局（建設業情報管理センター登録情報、月1回更新でExcel公開）
      https://www.toshiseibi.metro.tokyo.lg.jp/kenchiku_kaihatsu/kenchiku_shidou/gyosya_shido/kensetsu/tokyou_kensetsu_meibo

⚠ 実ファイル未検証: このセッションからは対象サイトにネットワーク到達できず、
  実データでのヘッダ確認ができていない。ヘッダ行は固定位置決め打ちにせず
  候補語マッチで検出する作りにしてあるが、実ファイルを初めて通す際は
  n_in/n_target のログと「対象業種が1件も取れない」警告を必ず確認すること。

対応する業種表記の形式（どちらでも動く）:
  - 縦持ち: 1列に「とび・土工工事業、塗装工事業」のようなテキストが入っている
  - 横持ち: 業種ごとに列があり、セル値が 1(一般)/2(特定)/空欄(なし) のフラグ

大臣許可業者（許可区分に「大臣」を含む行）はスコープ外として読み飛ばす。
"""
import re

from . import common

PREF_NAME = "東京都"

_HEADER_CANDIDATES = {
    "license_no": ["許可番号", "登録番号"],
    "name": ["商号又は名称", "商号", "名称"],
    "license_type": ["許可行政庁", "許可区分", "許可の種類"],
    "address": ["主たる営業所の所在地", "所在地", "本店所在地", "住所"],
    "city": ["市区町村"],
    "phone": ["電話番号", "電話"],
    "fax": ["FAX番号", "ＦＡＸ番号"],
    "capital": ["資本金の額", "資本金"],
    "license_date": ["許可年月日", "許可(更新)年月日", "許可・更新年月日"],
    "trade_single": ["許可業種", "業種"],
}

_FALSY_FLAG = {"", "0", "－", "-", "ー", "―", "無", "なし"}


def _is_licensed_flag(v) -> bool:
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v not in (0,)
    return str(v).strip() not in _FALSY_FLAG


def parse(path):
    """東京都名簿Excelを読み、companies共通スキーマの行(dict)を対象業種の会社分だけyieldする。"""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        print(f"警告: {path} は空です")
        return

    header_i, cols = common.find_header_row(rows, _HEADER_CANDIDATES)
    if header_i is None or "name" not in cols or "license_no" not in cols:
        raise ValueError(
            f"{path}: ヘッダ行(許可番号/商号又は名称 等)を検出できませんでした。"
            f"先頭行の実際の内容: {rows[0] if rows else None}"
        )

    # 横持ち判定: ヘッダ行のセルが29業種名(とび・土工/塗装/解体)に直接一致する列を探す
    header_cells = ["" if c is None else str(c).strip() for c in rows[header_i]]
    trade_cols = {}  # category -> column index
    for j, cell in enumerate(header_cells):
        cat = common.category_from_text(cell)
        if cat:
            trade_cols[cat] = j
    wide_format = bool(trade_cols)

    if not wide_format and "trade_single" not in cols:
        raise ValueError(
            f"{path}: 業種を表す列(許可業種/業種、または とび・土工/塗装/解体 の個別列)"
            f"を検出できませんでした。ヘッダ検出結果: {cols}"
        )
    if "license_type" not in cols:
        print(f"警告: {path} で許可区分の列が見つからず、大臣許可の除外ができていません。"
              "全行を知事許可として扱っています。実データでは必ず確認すること。")

    def cell(row, concept):
        j = cols.get(concept)
        return row[j] if j is not None and j < len(row) else None

    n_in = n_target = n_daijin = 0
    for row in rows[header_i + 1:]:
        if row is None or all(c is None for c in row):
            continue
        n_in += 1

        license_type_raw = str(cell(row, "license_type") or "")
        if "大臣" in license_type_raw:
            n_daijin += 1
            continue  # 大臣許可は当面スコープ外

        if wide_format:
            trades = ",".join(sorted(
                cat for cat, j in trade_cols.items()
                if j < len(row) and _is_licensed_flag(row[j])))
        else:
            blob = str(cell(row, "trade_single") or "")
            trades = common.normalize_trades(re.split(r"[、,・/\n]+", blob))

        if not trades:
            continue
        n_target += 1

        yield {
            "license_no": cell(row, "license_no"),
            "name": cell(row, "name"),
            "pref": PREF_NAME,
            "city": cell(row, "city"),
            "address": cell(row, "address"),
            "phone": cell(row, "phone"),
            "fax": cell(row, "fax"),
            "license_type": "知事",  # 大臣許可は上で除外済み
            "trades": trades,
            "capital": common.parse_capital(cell(row, "capital")),
            "founded_year": common.parse_year(cell(row, "license_date")),
        }

    print(f"  {PREF_NAME}: 読込{n_in}行 / 大臣許可除外{n_daijin}件 / 対象業種{n_target}件"
          f" ({'横持ち' if wide_format else '縦持ち'}形式で解釈)")
    if n_in > 0 and n_target == 0:
        print(f"警告: {path} から対象業種(とび・土工/塗装/解体)の会社が1件も取れませんでした。"
              f"業種列の形式・表記を確認してください。ヘッダ検出結果: {cols}"
              f" / 横持ち候補列: {trade_cols}")
