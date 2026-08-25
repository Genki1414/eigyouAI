"""
form_navigator.py — 問い合わせフォームへのPlaywright操作専任モジュール
senders.FormSenderから呼ばれる。ここは「ブラウザ操作」だけを担当し、
企業管理・テナント管理・接触ガード・送信履歴には一切触れない
(それらはsenders.py/db.py側の責務)。

処理の流れ:
  URLを開く → 問い合わせページを探索(無ければ) → フォームを検出 →
  入力欄を判定 → 値を入力 → 確認画面があれば進む → 送信 → 成功判定

ステータス:
  SUCCESS              送信完了を高い確度で確認できた
  SKIP_*               送信を試すべきではないと判断した(CAPTCHA/営業禁止等)
  FAILED_RETRYABLE     一時的な失敗(タイムアウト・通信エラー等)。呼び出し側で再試行してよい
  FAILED_UNSUPPORTED   今のルールでは対応できない構造

使い方:
  from form_navigator import navigate_and_submit
  result = navigate_and_submit(url, values)
  result.status  # "SUCCESS" / "SKIP_CAPTCHA" / ...
"""
import os
import random
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote, urlsplit

MAX_CRAWL_PAGES = 5          # 問い合わせページ探索で開くページ数の上限
NAV_TIMEOUT_MS = 45000
ACTION_TIMEOUT_MS = 10000


def _parse_proxy(proxy_url):
    """"http://user:pass@host:port" 形式の文字列を、Playwrightのproxy引数が
    要求する形({"server","username","password"})へ変換する。Playwrightは
    user:pass@をserver URLへ埋め込む書き方をサポートしない(別引数として渡す
    必要がある)ため、ここで分離する。認証情報が無い場合はserverのみ返す。"""
    parts = urlsplit(proxy_url)
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    proxy = {"server": server}
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy


def _pick_proxy():
    """config.FORM_PROXY_POOL(T42: 送信元IPの分散)からランダムに1つ選ぶ。
    未設定(空リスト)ならNoneを返し、直接接続する(既定・後方互換の挙動)。
    T41で並列化した複数ワーカーそれぞれがブラウザ起動時に呼ぶため、単純な
    ランダム選択で長期的にはプール全体へ分散する(厳密なラウンドロビンは
    ワーカー間の共有カウンタが要るぶん複雑になるだけで、目的<IPの分散>には
    どちらでも十分)。"""
    import config as C
    if not C.FORM_PROXY_POOL:
        return None
    return _parse_proxy(random.choice(C.FORM_PROXY_POOL))


def _launch_browser(p, headless):
    """本番(Dockerイメージ)は`playwright install --with-deps chromium`で正規に
    インストールされたChromiumをそのまま使う(既定の挙動。この関数は何も変えない)。
    開発環境でPlaywright標準のブラウザダウンロードができない場合だけ、
    環境変数PLAYWRIGHT_CHROMIUM_PATHで代替のchromium実行ファイルを指定できる
    (例: サンドボックス環境でcdn.playwright.devへ到達できない場合の開発用途。
    本番では未設定のままにしておくこと)。

    config.FORM_PROXY_POOLが設定されていれば、起動のたびにプールから選んだ
    プロキシを経由させる(T42: 送信元IPの分散)。"""
    proxy = _pick_proxy()
    exe = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
    if exe:
        return p.chromium.launch(
            executable_path=exe, headless=headless, proxy=proxy,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
    return p.chromium.launch(headless=headless, proxy=proxy)

# ── フィールド判定の同義語辞書 ────────────────
# 表記ゆれ・同義語を広めに持つ。name/id/placeholder/aria-label/label文言/
# 周辺テキストを結合した文字列に対して部分一致で見る。
_FIELD_HINTS = {
    "email": ["メールアドレス", "メール", "eメール", "e-mail", "email", "mail"],
    "email_confirm": ["メール確認", "メールアドレス（確認", "確認用メール", "email confirm", "re-enter"],
    "phone": ["電話番号", "電話", "tel", "phone", "fax番号"],
    "postal_code": ["郵便番号", "〒", "zip", "postal"],
    "prefecture": ["都道府県", "都道府県名", "prefecture", "pref"],
    "city": ["市区町村", "市町村", "city"],
    "block": ["丁目番地", "丁目・番地", "町名・番地", "丁目", "番地"],
    "building": ["ビル名", "建物名", "マンション名", "部屋番号", "building"],
    "address": ["住所", "所在地", "address"],
    "message": ["お問い合わせ内容", "ご質問内容", "ご相談内容", "ご要望", "メッセージ", "本文",
                "お問い合わせ詳細", "詳細", "message", "inquiry", "comment"],
    "company": ["会社名", "法人名", "貴社名", "御社名", "団体名", "company", "organization"],
    "subject": ["件名", "題名", "タイトル", "subject", "title"],
    "inquiry_type": ["お問い合わせ種類", "お問い合わせ項目", "ご用件", "カテゴリ", "種別",
                      "inquiry type", "category"],
    "last_name": ["姓", "苗字", "last name", "family name"],
    "first_name": ["名", "first name", "given name"],
    "name": ["お名前", "氏名", "担当者名", "ご担当者", "ご担当者名", "your name", "name"],
    "furigana": ["フリガナ", "ふりがな", "カナ", "かな", "kana"],
    "department": ["部署", "部署名", "所属", "department", "division"],
    "position": ["役職", "役職名", "position", "job title"],
}

# "name"という汎用語を除いた、氏名(フルネーム)固有のフレーズ手がかりのみ。
# 汎用"name"はname="last-name"のようなHTML属性にも紛れ込むため_classify_fieldで別扱いする。
_NAME_HINTS_STRONG = [h for h in _FIELD_HINTS["name"] if h != "name"]

_CONSENT_HINTS = ["プライバシー", "個人情報", "利用規約", "同意します", "同意する", "agree", "privacy"]

_CONTACT_LINK_HINTS = [
    "お問い合わせ", "お問合せ", "問い合わせ", "問合せ", "お問い合せ",
    "contact", "inquiry", "mail",
]
_CONTACT_PATH_HINTS = ["contact", "inquiry", "otoiawase", "toiawase"]

_SUCCESS_HINTS = (
    "ありがとうございます", "ありがとうございました", "送信が完了", "送信しました",
    "送信いたしました", "送信されました", "受け付け", "受付ました", "受付いたしました",
    "受け付けました", "承りました", "お問い合わせいただき", "ご連絡いたします",
    "担当者より", "追ってご連絡", "確認の上", "確認次第", "折り返しご連絡",
    "thank you", "thanks for", "successfully",
)

_CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", ".g-recaptcha",
    "[class*='captcha']", "[id*='captcha']",
)

_NO_SOLICIT_HINTS = [
    "営業目的の", "営業のご連絡", "営業メールはご遠慮", "セールスのご連絡はご遠慮",
    "営業のお電話", "勧誘目的", "営業・勧誘", "セールス・勧誘",
]

_RECRUIT_ONLY_HINTS = ["採用に関するお問い合わせ専用", "採用エントリー", "新卒採用専用", "中途採用専用"]
_SUPPORT_ONLY_HINTS = ["既存のお客様専用", "契約者様専用", "サポート専用窓口", "会員専用"]

# Cloudflare等のボット検証チャレンジ画面。CAPTCHAと同じく自動突破の対象にはしない
_BOT_CHALLENGE_TITLE_HINTS = ["just a moment", "attention required", "checking your browser"]

_SUBMIT_TEXT_RE = re.compile(r"送信|確認する|この内容で送信|次へ進む|送信する|submit", re.I)
_CONFIRM_TEXT_RE = re.compile(r"確認画面|入力内容を確認|内容を確認|次へ|確認する", re.I)


@dataclass
class NavigationResult:
    status: str                              # SUCCESS / SKIP_* / FAILED_RETRYABLE / FAILED_UNSUPPORTED
    reason_code: str = ""
    contact_url_used: Optional[str] = None    # 実際にフォームが見つかったページ
    detected_fields: dict = field(default_factory=dict)   # {kind: 個数}
    filled_fields: list = field(default_factory=list)     # [kind, ...]
    submit_attempted: bool = False
    success_evidence: Optional[str] = None
    error_message: Optional[str] = None
    final_url: Optional[str] = None
    page_title: Optional[str] = None
    page_text_snippet: Optional[str] = None   # 診断用。成功判定できなかった時の原因調査に使う
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    screenshot_before_path: Optional[str] = None   # 問い合わせページ到達直後(入力前)
    screenshot_after_path: Optional[str] = None    # 送信ボタン押下後(送信を試みた場合のみ)


def _text_blob(page, el):
    """要素の判定材料(name/id/placeholder/aria-label/label文言)を1本の文字列にする。"""
    try:
        parts = [
            el.get_attribute("name") or "", el.get_attribute("id") or "",
            el.get_attribute("placeholder") or "", el.get_attribute("aria-label") or "",
            el.get_attribute("autocomplete") or "", _label_for(page, el) or "",
        ]
        return " ".join(parts).lower()
    except Exception:  # noqa: BLE001
        return ""


def _label_for(page, el):
    """input要素のラベル文言。label[for]優先、無ければ祖先/直前要素/直前テキスト。"""
    try:
        el_id = el.get_attribute("id")
        if el_id:
            lbl = page.query_selector(f'label[for="{el_id}"]')
            if lbl:
                return lbl.inner_text()
    except Exception:  # noqa: BLE001
        pass
    try:
        return el.evaluate("""e => {
            const p = e.closest('label');
            if (p) return p.innerText;
            let prev = e.previousElementSibling;
            for (let i = 0; i < 3 && prev; i++) {
                if (prev.innerText && prev.innerText.trim()) return prev.innerText;
                prev = prev.previousElementSibling;
            }
            const parent = e.parentElement;
            if (parent) {
                const txt = Array.from(parent.childNodes)
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent).join(' ').trim();
                if (txt) return txt;
            }
            return '';
        }""") or ""
    except Exception:  # noqa: BLE001
        return ""


def _classify_field(page, el):
    text = _text_blob(page, el)
    try:
        itype = (el.get_attribute("type") or "").lower()
        tag = (el.evaluate("e => e.tagName") or "").lower()
    except Exception:  # noqa: BLE001
        itype, tag = "", ""

    if (itype == "email" or any(h in text for h in _FIELD_HINTS["email"])
            or any(h in text for h in _FIELD_HINTS["email_confirm"])):
        if any(h in text for h in _FIELD_HINTS["email_confirm"]):
            return "email_confirm"
        return "email"
    if itype == "tel" or any(h in text for h in _FIELD_HINTS["phone"]):
        return "phone"
    if any(h in text for h in _FIELD_HINTS["postal_code"]):
        return "postal_code"
    # 都道府県/市区町村/丁目番地/建物名は「住所」より先に判定する(値がある場合のみ
    # senders.py側で個別入力される。無い場合は"address"の連結済み文字列にフォールバック)
    if any(h in text for h in _FIELD_HINTS["prefecture"]):
        return "prefecture"
    if any(h in text for h in _FIELD_HINTS["city"]):
        return "city"
    if any(h in text for h in _FIELD_HINTS["block"]):
        return "block"
    if any(h in text for h in _FIELD_HINTS["building"]):
        return "building"
    if any(h in text for h in _FIELD_HINTS["address"]):
        return "address"
    if tag == "textarea" or any(h in text for h in _FIELD_HINTS["message"]):
        return "message"
    if any(h in text for h in _FIELD_HINTS["company"]):
        return "company"
    if any(h in text for h in _FIELD_HINTS["department"]):
        return "department"
    if any(h in text for h in _FIELD_HINTS["position"]):
        return "position"
    if any(h in text for h in _FIELD_HINTS["subject"]):
        return "subject"
    if any(h in text for h in _FIELD_HINTS["furigana"]):
        return "furigana"
    # 「名」は先頭一致でfirst_nameの短い手がかりだが、「お名前」等の氏名フルネーム表記に
    # 部分文字列として含まれてしまうため、先にnameの固有フレーズ(汎用な"name"は除く)を
    # 優先判定する。汎用"name"はname="last-name"のようなHTML属性にも紛れ込むため、
    # last_name/first_nameの判定より後の最終フォールバックとして残す。
    if any(h in text for h in _NAME_HINTS_STRONG):
        return "name"
    if any(h in text for h in _FIELD_HINTS["last_name"]):
        return "last_name"
    if any(h in text for h in _FIELD_HINTS["first_name"]):
        return "first_name"
    if "name" in text:
        return "name"
    return None


# ── 問い合わせページの探索 ───────────────────
def _looks_like_contact_page(url):
    low = (url or "").lower()
    return any(h in low for h in _CONTACT_PATH_HINTS)


def _has_fillable_form(page):
    try:
        return page.query_selector("input[type=text], input[type=email], "
                                    "input:not([type]), textarea") is not None
    except Exception:  # noqa: BLE001
        return False


def _looks_like_real_contact_form(page):
    """『トップページに検索窓やニュースレター登録欄があるだけ』を問い合わせフォームと
    誤認しないための強めの判定。textarea(お問い合わせ本文欄)の存在を必須にする
    (検索窓・メール登録欄は通常textareaを持たないが、問い合わせフォームはほぼ必ず持つ)。"""
    try:
        return page.query_selector("textarea") is not None
    except Exception:  # noqa: BLE001
        return False


def _find_contact_link(page):
    """ヘッダー/フッター/ナビゲーションから問い合わせページらしいリンクを探す。"""
    try:
        links = page.query_selector_all("a[href]")
    except Exception:  # noqa: BLE001
        return None
    for a in links:
        try:
            text = (a.inner_text() or "").strip()
            href = a.get_attribute("href") or ""
        except Exception:  # noqa: BLE001
            continue
        if any(h in text for h in _CONTACT_LINK_HINTS) or any(h in href.lower() for h in _CONTACT_PATH_HINTS):
            if href and not href.startswith("#") and not href.lower().startswith("javascript:"):
                try:
                    return a.get_attribute("href"), page.evaluate("(h) => new URL(h, location.href).href", href)
                except Exception:  # noqa: BLE001
                    return href, href
    return None


def _resolve_contact_page(page, start_url):
    """トップページ等しか無い場合に、問い合わせページへ1階層だけ辿る。
    既に問い合わせページらしいURL、または『本物の』問い合わせフォームが直接見つかれば
    そのまま使う。単なる入力欄(検索窓等)の存在だけでは早期確定しない。"""
    if _looks_like_contact_page(start_url) or _looks_like_real_contact_form(page):
        return page.url, None

    found = _find_contact_link(page)
    if not found:
        return page.url, "問い合わせページへのリンクが見つからず"

    _, absolute_url = found
    try:
        page.goto(absolute_url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    except Exception as e:  # noqa: BLE001
        return page.url, f"問い合わせページへの遷移に失敗: {type(e).__name__}"
    return page.url, None


# ── 検知系(送るべきでないフォームの判定) ─────────
def _page_text(page):
    try:
        return page.inner_text("body")
    except Exception:  # noqa: BLE001
        return ""


def _detect_captcha(page):
    for sel in _CAPTCHA_SELECTORS:
        try:
            if page.query_selector(sel):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _detect_no_solicit(text):
    return any(h in text for h in _NO_SOLICIT_HINTS)


def _detect_recruit_only(text):
    return any(h in text for h in _RECRUIT_ONLY_HINTS)


def _detect_support_only(text):
    return any(h in text for h in _SUPPORT_ONLY_HINTS)


def _detect_bot_challenge(page):
    try:
        title = (page.title() or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(h in title for h in _BOT_CHALLENGE_TITLE_HINTS)


# ── 送信ボタン ───────────────────────────
def _find_button(page, text_re):
    for sel in ("button[type=submit]", "input[type=submit]"):
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                return btn
        except Exception:  # noqa: BLE001
            pass
    try:
        for el in page.query_selector_all("button, a, input[type=button]"):
            if not el.is_visible():
                continue
            text = (el.inner_text() if el.evaluate("e=>e.tagName") != "INPUT"
                    else (el.get_attribute("value") or "")).strip()
            if text_re.search(text):
                return el
    except Exception:  # noqa: BLE001
        pass
    return None


def _click(el):
    """通常クリックを試し、失敗したらJS経由のクリックにフォールバックする。
    Cookie同意バナーやチャットウィジェットがボタンに重なっていて通常クリックが
    ブロックされるケースがStep3検証で最頻出の一時失敗パターンだったための対策。"""
    try:
        el.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        pass
    try:
        el.click(timeout=ACTION_TIMEOUT_MS)
        return True
    except Exception:  # noqa: BLE001
        pass
    try:
        el.evaluate("e => e.click()")
        return True
    except Exception:  # noqa: BLE001
        return False


_SELECT_PLACEHOLDER_RE = re.compile(
    r"選択してください|お選び|select|please choose|指定なし|未選択", re.I)
_SELECT_INQUIRY_OPTION_RE = re.compile(r"お問い合わせ|その他|general|other", re.I)


def _fill_selects(page):
    """<select>要素(問い合わせ種類・都道府県等のプルダウン)を埋める。
    必須のプルダウンが未選択のままだと送信がブロックされるサイトが多いため対応する。
    「お問い合わせ」寄りの選択肢があればそれを、無ければプレースホルダーではない
    先頭の選択肢を選ぶ(都道府県等、正解が決まらない項目でも「未選択」を避ける方が
    送信を通せる可能性が高いという判断)。"""
    filled = 0
    try:
        selects = page.query_selector_all("select")
    except Exception:  # noqa: BLE001
        return filled
    for sel in selects:
        try:
            if not sel.is_visible():
                continue
            options = sel.query_selector_all("option")
            candidates = []
            for opt in options:
                text = (opt.inner_text() or "").strip()
                value = opt.get_attribute("value") or ""
                if not value or _SELECT_PLACEHOLDER_RE.search(text):
                    continue
                candidates.append((text, value))
            if not candidates:
                continue
            pick = next((v for t, v in candidates if _SELECT_INQUIRY_OPTION_RE.search(t)),
                        candidates[0][1])
            sel.select_option(value=pick, timeout=ACTION_TIMEOUT_MS)
            try:
                sel.dispatch_event("change")
            except Exception:  # noqa: BLE001
                pass
            filled += 1
        except Exception:  # noqa: BLE001
            continue
    return filled


# ── メイン ───────────────────────────────
def _save_screenshot(page, screenshot_dir, run_id, suffix):
    """送信前後の目視確認用スクリーンショット(MIKOMERU同等機能)。
    撮影・保存に失敗しても送信処理自体は止めない(あくまで補助情報のため)。"""
    if not screenshot_dir:
        return None
    try:
        from pathlib import Path
        d = Path(screenshot_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{run_id}_{suffix}.png"
        page.screenshot(path=str(path), timeout=ACTION_TIMEOUT_MS)
        return str(path)
    except Exception:  # noqa: BLE001
        return None


def discover_contact_url(start_url, *, headless=True):
    """指定URLから問い合わせページを探すだけの軽量版(入力・送信は一切行わない、
    閲覧専用の探索)。MIKOMERUの「CSV検索(URLで検索)」相当の機能で使う——
    顧客が持ち込んだ会社名+サイトURLのCSVから、問い合わせページURLを見つけて
    companies.contact_urlを埋める用途(target_lists.create_from_csvから呼ばれる)。
    navigate_and_submit()と探索ロジック(_resolve_contact_page)は完全に共有する。

    戻り値: {"status": "FOUND"|"NO_FORM"|"UNREACHABLE"|"ERROR",
             "contact_url": str|None, "error": str|None}
      FOUND      : 問い合わせフォームが見つかった(contact_urlに実際のページURL)
      NO_FORM    : ページには辿り着けたが、フォームらしきものが見つからなかった
      UNREACHABLE: 開始URL自体に到達できなかった(ドメイン間違い・閉鎖等)
      ERROR      : その他の予期しない失敗(Playwright起動失敗等)"""
    from playwright.sync_api import sync_playwright

    result = {"status": "ERROR", "contact_url": None, "error": None}
    try:
        with sync_playwright() as p:
            browser = _launch_browser(p, headless)
            try:
                page = browser.new_page()
                try:
                    page.goto(start_url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                except Exception as e:  # noqa: BLE001
                    result["status"] = "UNREACHABLE"
                    result["error"] = f"{type(e).__name__}: {e}"
                    return result

                contact_url, discover_err = _resolve_contact_page(page, start_url)
                if _has_fillable_form(page):
                    result["status"] = "FOUND"
                    result["contact_url"] = contact_url
                else:
                    result["status"] = "NO_FORM"
                    result["contact_url"] = contact_url
                    result["error"] = discover_err
                return result
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        return result


def navigate_and_submit(start_url, values, *, headless=True, screenshot_dir=None, allow_no_solicit=False):
    """フォームへの一連の操作を行い、NavigationResultを返す。
    values: {"company","name","email","phone","message","subject", ...} の埋める値の辞書。
    screenshot_dir: 指定すると、問い合わせページ到達直後(送信前)と送信ボタン押下後
    (送信後)のスクリーンショットをこの配下に保存する(Noneなら撮影しない。テストでは
    Playwright未起動のケースが多いため既定でOFF)。
    allow_no_solicit: Trueだと「営業目的お断り」等の記載を検出してもSKIP_NO_SOLICITで
    止めず、そのまま送信を試みる(MIKOMERUの「営業拒否サイトへの送信」相当。
    マニュアル通り「送信テスト用の機能。ご注意ください」——既定はFalseで従来通り
    スキップする安全側)。
    例外は投げない(FAILED_RETRYABLEにしたい一時エラーだけは呼び出し側で判断できるよう
    resultのstatusで表現する。senders.py側でR.Retryableへ変換するかはそちら任せ)。"""
    from playwright.sync_api import sync_playwright

    result = NavigationResult(status="FAILED_UNSUPPORTED")
    try:
        with sync_playwright() as p:
            browser = _launch_browser(p, headless)
            try:
                page = browser.new_page()
                try:
                    page.goto(start_url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                except Exception as e:  # noqa: BLE001
                    msg = str(e)
                    if "ERR_CERT_" in msg or "ERR_SSL_" in msg:
                        # 相手サイト側のTLS証明書不備。再試行しても同じ結果になるだけなので
                        # リトライ対象にしない(FAILED_RETRYABLEにしない)
                        result.status = "FAILED_UNSUPPORTED"
                        result.reason_code = "invalid_certificate"
                    else:
                        result.status = "FAILED_RETRYABLE"
                        result.reason_code = "goto_failed"
                    result.error_message = f"{type(e).__name__}: {e}"
                    return result

                contact_url, discover_err = _resolve_contact_page(page, start_url)
                result.contact_url_used = contact_url
                result.final_url = page.url
                try:
                    result.page_title = page.title()
                except Exception:  # noqa: BLE001
                    pass
                result.screenshot_before_path = _save_screenshot(
                    page, screenshot_dir, result.run_id, "before")

                page_text = _page_text(page)

                if _detect_bot_challenge(page):
                    result.status = "SKIP_BOT_CHALLENGE"
                    result.reason_code = "bot_challenge_detected"
                    return result
                if _detect_captcha(page):
                    result.status = "SKIP_CAPTCHA"
                    result.reason_code = "captcha_detected"
                    return result
                if _detect_no_solicit(page_text) and not allow_no_solicit:
                    result.status = "SKIP_NO_SOLICIT"
                    result.reason_code = "no_solicitation_notice"
                    return result
                if _detect_recruit_only(page_text):
                    result.status = "SKIP_RECRUIT_ONLY"
                    result.reason_code = "recruit_only_form"
                    return result
                if _detect_support_only(page_text):
                    result.status = "SKIP_SUPPORT_ONLY"
                    result.reason_code = "support_only_form"
                    return result

                if not _has_fillable_form(page):
                    result.status = "FAILED_UNSUPPORTED"
                    result.reason_code = discover_err or "form_not_found"
                    result.page_text_snippet = page_text[:400]
                    return result

                fields = page.query_selector_all(
                    "input[type=text], input[type=email], input[type=tel], "
                    "input:not([type]), textarea")
                detected, filled = {}, []
                for el in fields:
                    try:
                        if not el.is_visible():
                            continue
                    except Exception:  # noqa: BLE001
                        continue
                    kind = _classify_field(page, el)
                    if not kind:
                        continue
                    detected[kind] = detected.get(kind, 0) + 1
                    # 呼び出し側(senders.py)が姓・名それぞれの妥当な既定値を
                    # 決めて渡す(未設定の名を会社名で埋める、といった代替は
                    # ここでは行わない。呼び出し側の送信者情報の解釈の話のため)
                    fill_value = values.get(kind)
                    if fill_value:
                        try:
                            el.fill(fill_value, timeout=ACTION_TIMEOUT_MS)
                            # .fill()はinput/changeイベントを発火するはずだが、Vue/React等の
                            # 独自バインディングがそれを拾わず「未入力」表示のまま残るサイトが
                            # あったため、念のため明示的にも発火させておく
                            try:
                                el.dispatch_event("input")
                                el.dispatch_event("change")
                            except Exception:  # noqa: BLE001
                                pass
                            filled.append(kind)
                        except Exception:  # noqa: BLE001
                            pass

                if not filled:
                    result.detected_fields = detected
                    result.filled_fields = filled
                    result.status = "FAILED_UNSUPPORTED"
                    result.reason_code = "no_fields_filled"
                    return result

                # プルダウン(お問い合わせ種類・都道府県等)。必須なのに未選択のままだと
                # 送信がブロックされるサイトが多いため埋める
                n_selects = _fill_selects(page)
                if n_selects:
                    detected["select"] = n_selects
                    filled.append(f"select×{n_selects}")

                result.detected_fields = detected
                result.filled_fields = filled

                # 同意チェックボックス
                try:
                    for cb in page.query_selector_all("input[type=checkbox]"):
                        if not cb.is_visible() or cb.is_checked():
                            continue
                        if any(h in (_label_for(page, cb) or "") for h in _CONSENT_HINTS):
                            cb.check(timeout=ACTION_TIMEOUT_MS)
                except Exception:  # noqa: BLE001
                    pass

                submit_btn = _find_button(page, _SUBMIT_TEXT_RE)
                if not submit_btn:
                    result.status = "FAILED_UNSUPPORTED"
                    result.reason_code = "submit_button_not_found"
                    result.page_text_snippet = _page_text(page)[:400]
                    return result

                result.submit_attempted = True
                if not _click(submit_btn):
                    result.status = "FAILED_RETRYABLE"
                    result.reason_code = "submit_click_failed"
                    return result
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:  # noqa: BLE001
                    pass

                # 入力→確認→送信の2段階フォーム対応。確認画面が残っていればもう一度押す
                confirm_btn = _find_button(page, _CONFIRM_TEXT_RE) or _find_button(page, _SUBMIT_TEXT_RE)
                if confirm_btn:
                    _click(confirm_btn)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:  # noqa: BLE001
                        pass

                # AJAX送信の完了メッセージが非同期で少し遅れて描画されるサイトがあるため、
                # networkidleの後にもう少しだけ待つ
                try:
                    page.wait_for_timeout(1500)
                except Exception:  # noqa: BLE001
                    pass

                result.screenshot_after_path = _save_screenshot(
                    page, screenshot_dir, result.run_id, "after")
                result.final_url = page.url
                final_text = _page_text(page)
                result.page_text_snippet = final_text[:400]
                url_changed = page.url != contact_url
                # フォームがDOM上から消えている(=AJAXで完了画面に差し替わった)ことも
                # 成功の傍証として見る。文言・URLどちらも一致しないAJAX系フォーム向けの保険
                form_gone = not _has_fillable_form(page)
                hit = next((k for k in _SUCCESS_HINTS if k in final_text), None)
                if hit:
                    result.status = "SUCCESS"
                    result.reason_code = "success_text_matched"
                    result.success_evidence = hit
                    return result
                if url_changed:
                    result.status = "SUCCESS"
                    result.reason_code = "url_changed_after_submit"
                    result.success_evidence = page.url
                    return result
                if form_gone:
                    result.status = "SUCCESS"
                    result.reason_code = "form_disappeared_after_submit"
                    result.success_evidence = "form_not_present"
                    return result

                result.status = "FAILED_UNSUPPORTED"
                result.reason_code = "success_not_confirmed"
                return result
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001
        result.status = "FAILED_RETRYABLE"
        result.reason_code = "unexpected_error"
        result.error_message = f"{type(e).__name__}: {e}"
        return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:  # noqa: BLE001
            print(f"⚠ Playwright未使用のためスキップ ({type(e).__name__}: {e})")
            sys.exit(0)

        print("── プロキシ設定の変換(T42: 送信元IPの分散) ──")
        p1 = _parse_proxy("http://user:pa%40ss@myproxy.example.com:8080")
        ok1 = p1 == {"server": "http://myproxy.example.com:8080",
                     "username": "user", "password": "pa@ss"}
        print(f"  {'✓' if ok1 else '✗'} 認証情報付きURLをserver/username/passwordへ分離: {p1}")
        p2 = _parse_proxy("http://myproxy.example.com:3128")
        ok2 = p2 == {"server": "http://myproxy.example.com:3128"}
        print(f"  {'✓' if ok2 else '✗'} 認証情報が無ければserverのみ: {p2}")

        import config as C
        orig_pool = C.FORM_PROXY_POOL
        try:
            C.FORM_PROXY_POOL = []
            ok3 = _pick_proxy() is None
            print(f"  {'✓' if ok3 else '✗'} FORM_PROXY_POOL未設定なら直接接続(None)")
            C.FORM_PROXY_POOL = ["http://onlyone.example.com:8080"]
            ok4 = _pick_proxy() == {"server": "http://onlyone.example.com:8080"}
            print(f"  {'✓' if ok4 else '✗'} FORM_PROXY_POOL設定時はプールから選ぶ: {_pick_proxy()}")
        finally:
            C.FORM_PROXY_POOL = orig_pool

        print("\n── フィールド検出ヒューリスティック ──")
        samples = [
            ("標準的な日本語フォーム", """
                <form>
                  <label for="c">会社名</label><input id="c" name="company">
                  <label for="n">お名前</label><input id="n" name="your-name">
                  <label for="e">メールアドレス</label><input id="e" type="email">
                  <label for="t">電話番号</label><input id="t" type="tel">
                  <label for="m">お問い合わせ内容</label><textarea id="m"></textarea>
                  <input type="checkbox" id="agree"><label for="agree">プライバシーポリシーに同意する</label>
                  <button type="submit">送信する</button>
                </form>""", {"message", "email", "company", "name", "phone"}),
            ("placeholder頼みのフォーム", """
                <form>
                  <input name="field1" placeholder="貴社名をご記入ください">
                  <input name="field2" placeholder="メールアドレス">
                  <textarea name="field3" placeholder="ご相談内容をご記入ください"></textarea>
                  <input type="submit" value="確認する">
                </form>""", {"company", "email", "message"}),
            ("姓名分割 + aria-label", """
                <form>
                  <input name="sei" aria-label="姓"><input name="mei" aria-label="名">
                  <input name="mail_confirm" placeholder="メールアドレス（確認用）">
                  <textarea aria-label="ご質問内容"></textarea>
                </form>""", {"last_name", "first_name", "email_confirm", "message"}),
            ("住所分割(MIKOMERU同様の都道府県/市区町村/丁目番地/ビル名)", """
                <form>
                  <label for="zip">郵便番号</label><input id="zip" name="zip">
                  <label for="pref">都道府県</label><input id="pref" name="pref">
                  <label for="city">市区町村</label><input id="city" name="city">
                  <label for="block">丁目番地</label><input id="block" name="block">
                  <label for="bldg">ビル名・部屋番号</label><input id="bldg" name="bldg">
                </form>""", {"postal_code", "prefecture", "city", "block", "building"}),
        ]
        try:
            pw_ctx = sync_playwright().start()
            browser = _launch_browser(pw_ctx, True)
        except Exception as e:  # noqa: BLE001
            print(f"⚠ Playwrightのブラウザ起動に失敗したためスキップ ({type(e).__name__}: {e})")
            sys.exit(0)
        try:
            page = browser.new_page()
            for label, html, expect in samples:
                page.set_content(html)
                kinds = set()
                for el in page.query_selector_all("input, textarea"):
                    if (el.get_attribute("type") or "") in ("checkbox", "submit"):
                        continue
                    kind = _classify_field(page, el)
                    if kind:
                        kinds.add(kind)
                ok = expect <= kinds
                print(f"  {'✓' if ok else '✗'} {label}: 検出={sorted(kinds)}")

            print("\n── SKIP検知 ──")
            skip_cases = [
                ("CAPTCHA", '<div class="g-recaptcha"></div>', _detect_captcha, "page"),
                ("営業禁止文言", "営業目的の問い合わせはご遠慮ください", _detect_no_solicit, "text"),
                ("採用専用", "新卒採用専用のエントリーフォームです", _detect_recruit_only, "text"),
                ("会員専用", "契約者様専用のお問い合わせ窓口です", _detect_support_only, "text"),
            ]
            for label, payload, fn, kind in skip_cases:
                if kind == "page":
                    page.set_content(payload)
                    ok = fn(page)
                else:
                    ok = fn(payload)
                print(f"  {'✓' if ok else '✗'} {label}")

            print("\n── プロキシ経由の実アクセス(T42。ローカルの疑似ターゲット+"
                  "疑似プロキシで、実際にChromiumがプロキシを通ることを確認) ──")
            import http.server
            import http.client
            import threading as _threading

            proxy_seen = []

            class _FakeTargetHandler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    body = "<html><body>疑似お問い合わせページ</body></html>".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *a):
                    pass

            class _ProxyHandler(http.server.BaseHTTPRequestHandler):
                """プロキシ宛のリクエストを記録してから、実際のターゲットへ転送する
                (絶対URI形式のリクエストラインで届く。httpの平文なのでCONNECTトンネル
                ではなく通常のプロキシ転送になる)。"""

                def do_GET(self):
                    proxy_seen.append(self.path)
                    from urllib.parse import urlsplit as _us
                    parsed = _us(self.path)
                    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
                    conn.request("GET", parsed.path or "/")
                    resp = conn.getresponse()
                    body = resp.read()
                    self.send_response(resp.status)
                    for k, v in resp.getheaders():
                        if k.lower() not in ("transfer-encoding", "connection"):
                            self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(body)
                    conn.close()

                def log_message(self, *a):
                    pass

            target_srv = http.server.HTTPServer(("127.0.0.1", 0), _FakeTargetHandler)
            proxy_srv = http.server.HTTPServer(("127.0.0.1", 0), _ProxyHandler)
            target_port = target_srv.server_address[1]
            proxy_port = proxy_srv.server_address[1]
            for srv in (target_srv, proxy_srv):
                th = _threading.Thread(target=srv.serve_forever, daemon=True)
                th.start()
            try:
                C.FORM_PROXY_POOL = [f"http://127.0.0.1:{proxy_port}"]
                proxy_browser = _launch_browser(pw_ctx, True)
                try:
                    proxy_page = proxy_browser.new_page()
                    proxy_page.goto(f"http://127.0.0.1:{target_port}/",
                                     timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                    loaded_ok = "疑似お問い合わせページ" in _page_text(proxy_page)
                    routed_ok = any(f":{target_port}" in seen for seen in proxy_seen)
                    print(f"  {'✓' if loaded_ok else '✗'} プロキシ経由でもページ内容を正しく取得できる")
                    print(f"  {'✓' if routed_ok else '✗'} 疑似プロキシが実際にリクエストを受けて中継した"
                          f"(記録: {proxy_seen})")
                finally:
                    proxy_browser.close()
            finally:
                C.FORM_PROXY_POOL = orig_pool
                target_srv.shutdown()
                proxy_srv.shutdown()
        finally:
            browser.close()
            pw_ctx.stop()
    else:
        print("使い方: python3 form_navigator.py test")
