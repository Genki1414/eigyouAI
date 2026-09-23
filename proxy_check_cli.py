"""
proxy_check_cli.py — フォーム送信用プロキシが「今」使えるかを確かめる参照専用CLI

2026-09-22の四国2,975社への送信で、プロキシ(FORM_PROXY_POOL)が機能しておらず、
全社が ERR_TUNNEL_CONNECTION_FAILED でページを開けなかった。約480社ぶんの
機会を失ったが、**プロキシが死んでいることを確かめる手段が無かった**ため
8時間気づけなかった。その反省で用意した。

**認証情報は出力しない**。出すのは host:port と成否だけ。プロキシURLには
user:pass が含まれるので、そのまま表示しないこと(実行ログに残るため)。

FORM_PROXY_DISABLED で無効化している最中でも試せるよう、config経由ではなく
生の環境変数を読む(「直す前に通るか確かめてから戻す」ができないと意味がない)。

使い方:
  python3 proxy_check_cli.py
  python3 proxy_check_cli.py --url https://example.com/   # 任意の宛先で試す
"""
import argparse
import os
import socket
import urllib.request
from urllib.parse import urlsplit

# BrightDataが用意している疎通確認用エンドポイント(到達すると経路情報を返す)
DEFAULT_TEST_URL = "https://geo.brdtest.com/welcome.txt?product=dc&method=native"
TIMEOUT_SECONDS = 20

_TRUTHY = ("1", "true", "yes", "on")


def _label(proxy_url):
    """認証情報を落とし、host:port だけにする。ログに残す前提の表示用。"""
    parts = urlsplit(proxy_url)
    return f"{parts.hostname or '?'}:{parts.port or '?'}"


def check_one(proxy_url, url, timeout=TIMEOUT_SECONDS):
    """1つのプロキシを実際に通してみる。(成否, 表示用テキスト) を返す。"""
    who = _label(proxy_url)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    try:
        res = opener.open(url, timeout=timeout)
        return True, f"  OK   {who}   HTTP {res.status}"
    except Exception as e:  # noqa: BLE001
        # 例外文にプロキシURLが混ざることがあるので、認証情報が出ないよう念のため潰す
        detail = str(e).replace(proxy_url, who)[:110]
        return False, f"  NG   {who}   {detail}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_TEST_URL, help="疎通確認に使う宛先")
    ap.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS)
    args = ap.parse_args()

    socket.setdefaulttimeout(args.timeout)
    raw = os.environ.get("FORM_PROXY_POOL", "")
    disabled = os.environ.get("FORM_PROXY_DISABLED", "").strip().lower() in _TRUTHY
    pool = [p.strip() for p in raw.split(",") if p.strip()]

    print("FORM_PROXY_DISABLED : "
          + ("1 → 無効化中(送信は直接接続)" if disabled else "未設定 → 送信はプロキシ経由"))
    print(f"FORM_PROXY_POOL     : {len(pool)}件")
    print(f"疎通確認の宛先      : {args.url}")
    print("-" * 62)

    if not pool:
        print("  プロキシが1つも設定されていません(送信は直接接続)")
        return

    ok = 0
    for proxy_url in pool:
        good, line = check_one(proxy_url, args.url, args.timeout)
        print(line)
        ok += 1 if good else 0

    print("-" * 62)
    print(f"使えるプロキシ: {ok} / {len(pool)}件")
    if ok == 0:
        print("※ 1つも通りません。FORM_PROXY_DISABLED=1 のままにしてください")
        print("  (プロキシ経由にすると全社がERR_TUNNEL_CONNECTION_FAILEDで失敗します)")
    elif disabled:
        print("※ 通っています。FORM_PROXY_DISABLED を空にすれば送信がプロキシ経由に戻ります")
    else:
        print("※ 通っています。設定どおりプロキシ経由で送信されます")


if __name__ == "__main__":
    main()
