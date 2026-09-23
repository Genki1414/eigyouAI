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

**送信が実際に使う経路(Playwright/Chromium)でも必ず確かめる**。2026-09-23に、
urllibでは HTTP 200 が返るのにChromiumでは ERR_TUNNEL_CONNECTION_FAILED になる、
という食い違いが実際に起きた。urllibだけ見て「通っている」と判断すると、
戻した瞬間にまた全社が失敗する。

使い方:
  python3 proxy_check_cli.py
  python3 proxy_check_cli.py --no-browser              # urllibだけで確かめる(速い)
  python3 proxy_check_cli.py --url https://example.com/   # 任意の宛先で試す
"""
import argparse
import os
import socket
import urllib.request
from urllib.parse import urlsplit

# BrightDataが用意している疎通確認用エンドポイント(到達すると経路情報を返す)。
# **これだけでは足りない**: ベンダー自身のドメインは通るのに、ゾーンの宛先制限や
# 課金状態のせいで任意のサイトへは出られない、という状態があり得る。2026-09-23に
# まさにそれを疑う状況になった(brdtest.comは200なのに送信は全社失敗)。
VENDOR_TEST_URL = "https://geo.brdtest.com/welcome.txt?product=dc&method=native"
# ベンダー外の任意ドメイン。出口IPを返すので「本当に外へ出られたか」と
# 「どの国のIPか」を同時に確かめられる(地域制限対策で買ったので国も見たい)
OUTSIDE_TEST_URL = "https://api.ipify.org?format=json"
DEFAULT_TEST_URL = VENDOR_TEST_URL
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


def check_one_browser(proxy_url, url, timeout=TIMEOUT_SECONDS):
    """送信本体と**同じ経路**(Playwright/Chromium + form_navigator._parse_proxy)で
    通るかを確かめる。(成否, 表示用テキスト) を返す。

    urllibで通ってもここで落ちることがある(2026-09-23の実例)。判断に使うのは
    こちらの結果。form_navigatorの変換関数をそのまま使うのが要点——自前で
    proxy引数を組み立て直すと、変換側のバグを見逃す。"""
    who = _label(proxy_url)
    try:
        import form_navigator as FN
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        return False, f"  --   {who}   Playwrightを読み込めません: {str(e)[:70]}"
    try:
        proxy = FN._parse_proxy(proxy_url)
    except Exception as e:  # noqa: BLE001
        return False, f"  NG   {who}   プロキシURLの変換に失敗: {str(e)[:70]}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=proxy)
            try:
                page = browser.new_page()
                page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                return True, f"  OK   {who}   ブラウザでも到達"
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001
        detail = str(e).replace(proxy_url, who).splitlines()[0][:110]
        return False, f"  NG   {who}   {detail}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None,
                    help="疎通確認に使う宛先(既定: ベンダー用と外部ドメインの2つ)")
    ap.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS)
    ap.add_argument("--no-browser", action="store_true",
                    help="Playwrightでの確認を省く(速いが、送信の実経路は確かめられない)")
    ap.add_argument("--concurrency", type=int, default=0,
                    help="同時接続を試す本数(既定0=試さない)。"
                          "送信はFORM_SEND_CONCURRENCY本の並列で動くため、"
                          "単発で通っても同時接続で弾かれることがある")
    args = ap.parse_args()

    socket.setdefaulttimeout(args.timeout)
    raw = os.environ.get("FORM_PROXY_POOL", "")
    disabled = os.environ.get("FORM_PROXY_DISABLED", "").strip().lower() in _TRUTHY
    pool = [p.strip() for p in raw.split(",") if p.strip()]

    print("FORM_PROXY_DISABLED : "
          + ("1 → 無効化中(送信は直接接続)" if disabled else "未設定 → 送信はプロキシ経由"))
    # 既定では2箇所を試す。ベンダー自身のドメインだけ通って任意のサイトへ出られない
    # 状態を見逃さないため(2026-09-23の実例)
    targets = ([("指定", args.url)] if args.url
               else [("ベンダー", VENDOR_TEST_URL), ("外部ドメイン", OUTSIDE_TEST_URL)])
    print(f"FORM_PROXY_POOL     : {len(pool)}件")
    for tag, u in targets:
        print(f"疎通確認の宛先({tag}) : {u}")
    print("-" * 62)

    if not pool:
        print("  プロキシが1つも設定されていません(送信は直接接続)")
        return

    print("[1] urllibで確認(参考)")
    ok_plain = 0
    for tag, u in targets:
        for proxy_url in pool:
            good, line = check_one(proxy_url, u, args.timeout)
            print(f"  [{tag}]{line[2:]}")
            ok_plain += 1 if good else 0

    ok_browser = 0
    outside_ok = 0
    if args.no_browser:
        print()
        print("[2] ブラウザでの確認は --no-browser のため省略しました")
        print("    ※ 送信の実経路を確かめていないので、有効化の判断には使えません")
    else:
        print()
        print("[2] 送信と同じ経路(Playwright/Chromium)で確認 ← **判断に使うのはこちら**")
        for tag, u in targets:
            for proxy_url in pool:
                good, line = check_one_browser(proxy_url, u, args.timeout)
                print(f"  [{tag}]{line[2:]}")
                ok_browser += 1 if good else 0
                if good and tag != "ベンダー":
                    outside_ok += 1

    n = len(pool) * len(targets)
    print("-" * 62)
    print(f"urllibで通った   : {ok_plain} / {n}件")
    if not args.no_browser:
        print(f"ブラウザで通った : {ok_browser} / {n}件")
    print()
    if args.concurrency > 0 and pool and not args.no_browser:
        print("-" * 62)
        print(f"[3] 同時接続{args.concurrency}本で確認"
              f"(送信は並列で動くため、単発で通っても弾かれることがある)")
        import concurrent.futures as _cf
        target = args.url or OUTSIDE_TEST_URL
        with _cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = [ex.submit(check_one_browser, pool[i % len(pool)], target, args.timeout)
                    for i in range(args.concurrency)]
            results = [f.result() for f in futs]
        for i, (good, line) in enumerate(results, 1):
            print(f"  #{i}{line[3:]}")
        ok_c = sum(1 for good, _ in results if good)
        print(f"  → 同時{args.concurrency}本中 {ok_c}本が成功")
        if ok_c < args.concurrency:
            print("  ※ 単発では通るのに同時接続で落ちています。契約の同時セッション上限が")
            print("    原因の可能性が高い。有効化するならFORM_SEND_CONCURRENCYを"
                  f"{max(1, ok_c)}以下にするか、契約を見直してください")
        print()

    if args.no_browser:
        print("※ 判断を保留してください(ブラウザでの確認を省いたため)")
    elif ok_browser == 0:
        print("※ 有効化してはいけません。FORM_PROXY_DISABLED=1 のままにしてください")
        if ok_plain:
            print("  urllibでは通るのにブラウザで落ちています。2026-09-22と同じ状態で、")
            print("  有効化すると全社がERR_TUNNEL_CONNECTION_FAILEDで失敗します")
    elif not args.url and outside_ok == 0:
        print("※ 有効化してはいけません。FORM_PROXY_DISABLED=1 のままにしてください")
        print("  ベンダー自身のドメインは通るのに、外部ドメインへ出られていません。")
        print("  ゾーンの宛先制限か課金状態が原因の可能性があります(送信先は実企業の")
        print("  サイトなので、外部へ出られなければ意味がありません)")
    elif disabled:
        print("※ ブラウザでも通っています。FORM_PROXY_DISABLED を空にすれば")
        print("  送信がプロキシ経由に戻ります")
    else:
        print("※ ブラウザでも通っています。設定どおりプロキシ経由で送信されます")


if __name__ == "__main__":
    main()
