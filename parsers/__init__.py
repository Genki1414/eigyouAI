"""
parsers — 都道府県別Excel名簿パーサーのレジストリ。
新しい県を追加するときは parsers/<pref>.py に parse(path) -> Iterator[dict] を実装し、
ここに登録するだけでよい。ヘッダ位置・業種表記の違いなど県ごとの差分は
各parserモジュールに閉じ込め、companies共通スキーマへの変換はcommon.pyの
ヘルパーで揃える。
"""
from . import tokyo

REGISTRY = {
    "東京都": tokyo.parse,
}


def get_parser(pref_name: str):
    parser = REGISTRY.get(pref_name)
    if parser is None:
        raise ValueError(
            f"未対応の都道府県: {pref_name!r}。対応済み: {', '.join(REGISTRY) or '(なし)'}。"
            f"parsers/<pref>.py に parse(path) を実装して REGISTRY に登録してください。"
        )
    return parser
