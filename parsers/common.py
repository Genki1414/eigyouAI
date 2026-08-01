"""
parsers/common.py — 都道府県別パーサー共通の変換ロジック
県ごとのExcelレイアウトの差は parsers/<pref>.py 側で吸収し、
「業種の表現ゆれの解決」「和暦日付・金額の正規化」はここに一本化する。
companies テーブルのスキーマ(db.py)は変更しない。
"""
import re
import config as C

# 建設業許可 29業種区分（建設業法 別表第一）のコード → 正式名称。
# 都道府県名簿によっては「05」「5」のような数値コードで業種を表すため、
# 一度この表を経由してから config.TARGET_TRADES(とび/土工/塗装/解体)へ収束させる。
TRADE_CODE_NAMES = {
    "01": "土木一式工事", "02": "建築一式工事", "03": "大工工事", "04": "左官工事",
    "05": "とび・土工・コンクリート工事", "06": "石工事", "07": "屋根工事", "08": "電気工事",
    "09": "管工事", "10": "タイル・れんが・ブロック工事", "11": "鋼構造物工事", "12": "鉄筋工事",
    "13": "舗装工事", "14": "しゅんせつ工事", "15": "板金工事", "16": "ガラス工事",
    "17": "塗装工事", "18": "防水工事", "19": "内装仕上工事", "20": "機械器具設置工事",
    "21": "熱絶縁工事", "22": "電気通信工事", "23": "造園工事", "24": "さく井工事",
    "25": "建具工事", "26": "水道施設工事", "27": "消防施設工事", "28": "清掃施設工事",
    "29": "解体工事",
}

# 業種名の略号ゆれ。「とび・土工・コンクリート工事」を全部書かず
# 「とび土工」「とび」等で済ませている名簿があるため、config.TARGET_TRADES の
# キーワード一致(部分一致)に加えて、ここで代表的な略記も吸収する。
TRADE_TEXT_ALIASES = {
    "tobi": ["とび・土工", "とび土工", "とび、土工", "土工"],
    "tosou": ["塗装"],
    "kaitai": ["解体"],
}


def category_from_code(code) -> str | None:
    """業種コード(2桁 or 素の数値。"05"/"5"/5 等)→ tobi/tosou/kaitai。対象外業種はNone。"""
    if code is None:
        return None
    s = str(code).strip()
    if not re.fullmatch(r"\d{1,2}", s):
        return None
    name = TRADE_CODE_NAMES.get(s.zfill(2))
    if not name:
        return None
    for kw, cat in C.TARGET_TRADES.items():
        if kw in name:
            return cat
    return None


def category_from_text(raw: str) -> str | None:
    """業種名・略号のテキスト表記 → tobi/tosou/kaitai。対象外業種はNone。"""
    s = (raw or "").strip()
    if not s:
        return None
    for cat, aliases in TRADE_TEXT_ALIASES.items():
        if any(a in s for a in aliases):
            return cat
    for kw, cat in C.TARGET_TRADES.items():
        if kw in s:
            return cat
    return None


def category_from_any(raw) -> str | None:
    """コードともテキストとも判別つかない1トークンを両方の解釈で試す(コード優先)。"""
    cat = category_from_code(raw)
    if cat:
        return cat
    return category_from_text(str(raw) if raw is not None else "")


def normalize_trades(raw_values) -> str:
    """
    raw_values: 1社分の業種を表す生トークンのリスト
      （"とび・土工工事業" のような業種名、"05" のようなコード、いずれも可）。
    tobi/tosou/kaitai に変換し、重複を除いてカンマ区切りで返す。対象業種が
    無ければ空文字列（呼び出し側で対象外として除外する）。
    """
    found = set()
    for v in raw_values or []:
        if v is None or v == "":
            continue
        cat = category_from_any(v)
        if cat:
            found.add(cat)
    return ",".join(sorted(found))


# ── 和暦日付・金額の正規化（列名の違いに関わらず共通で必要） ──────────
ERA_BASE = {"令和": 2018, "平成": 1988, "昭和": 1925, "大正": 1911}
_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")


def parse_year(raw) -> int | None:
    """許可年月日から西暦年を抜き出す。和暦(令和/平成/昭和/大正)・西暦・
    Excelのdatetime/dateセルのいずれにも対応する。"""
    import datetime as _dt
    if isinstance(raw, (_dt.date, _dt.datetime)):
        return raw.year
    s = (str(raw) if raw is not None else "").translate(_ZEN2HAN)
    for era, base in ERA_BASE.items():
        m = re.search(rf"{era}\s*(元|\d+)\s*年", s)
        if m:
            n = 1 if m.group(1) == "元" else int(m.group(1))
            return base + n
    m = re.search(r"(19|20)\d{2}", s)
    return int(m.group()) if m else None


def parse_capital(raw) -> int:
    """資本金のカンマ区切り・円表記・全角数字・Excelの数値セルを吸収して整数(千円)にする。"""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).translate(_ZEN2HAN)
    s = re.sub(r"[,，円\s]", "", s)
    m = re.search(r"\d+", s)
    return int(m.group()) if m else 0


def find_header_row(rows, candidate_sets, max_scan=15):
    """
    先頭 max_scan 行の中から、ヘッダ行と思われる行を探す。
    candidate_sets: {概念名: [表記候補, ...]} のdict。
    戻り値: (行番号(0始まり), {概念名: 列番号}) 。見つからなければ (None, {}) 。
    県ごとにヘッダの行位置・表記が違っても、既知の表記候補に一致すれば拾えるようにする。
    """
    best = (None, {})
    for i, row in enumerate(rows[:max_scan]):
        cells = ["" if c is None else str(c).strip() for c in row]
        found = {}
        for concept, candidates in candidate_sets.items():
            for j, cell in enumerate(cells):
                if any(cell == cand or (cand in cell and cell) for cand in candidates):
                    found[concept] = j
                    break
        if len(found) > len(best[1]):
            best = (i, found)
    return best
