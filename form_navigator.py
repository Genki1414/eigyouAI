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
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote, urlsplit

MAX_CRAWL_PAGES = 5          # 問い合わせページ探索で開くページ数の上限
# T107: 1社あたりの所要時間を削るため、待ち時間はすべて.envで調整できるようにした。
# 既定値の根拠: 実運用のフォームはほぼ2秒以内に落ち着く。一方で広告・計測タグが常時通信して
# いるサイトでは"networkidle"が永久に来ず、旧実装(15秒×2回)はその上限を丸ごと待っていた。
NAV_TIMEOUT_MS = int(os.environ.get("FORM_NAV_TIMEOUT_MS", "30000"))
ACTION_TIMEOUT_MS = int(os.environ.get("FORM_ACTION_TIMEOUT_MS", "10000"))
SETTLE_TIMEOUT_MS = int(os.environ.get("FORM_SETTLE_TIMEOUT_MS", "6000"))
POST_SUBMIT_WAIT_MS = int(os.environ.get("FORM_POST_SUBMIT_WAIT_MS", "1200"))
# ページを開いてから入力欄が描画されるまでの待ち上限(2026-09-20)。入力欄が現れた
# 時点で即座に抜けるため、フォームがあるサイトでの所要時間はほとんど増えない。
# 逆に「そもそもフォームが無いページ」ではこの秒数だけ待つことになるので、
# 送信全体の所要時間とのトレードオフで.envから調整できるようにしている。
FORM_RENDER_WAIT_MS = int(os.environ.get("FORM_RENDER_WAIT_MS", "3000"))
# 問い合わせページの探索にかけてよい時間の上限(2026-09-20)。多段探索(MAX_CRAWL_PAGES)と
# 描画待ち(FORM_RENDER_WAIT_MS)を素直に掛け算すると、「どこにもフォームが無い会社」1社に
# 30秒以上かけてしまい、送信全体の所要時間が伸びる。フォームが見つかる会社は数秒で
# 抜けるので、この上限は実質「見つからない会社を早めに諦める」ためのもの。
FORM_DISCOVER_BUDGET_MS = int(os.environ.get("FORM_DISCOVER_BUDGET_MS", "20000"))
# ブラウザ再利用(T107)。旧実装は1社ごとにPlaywrightドライバ起動+Chromium起動+終了を
# していて、これだけで1社あたり数秒かかっていた。スレッドごとに1つ起動して使い回し、
# 会社ごとにはコンテキスト(Cookie等は毎回まっさら)だけ作り直す。
# MAX_USESごとに起動し直すのは、長時間稼働でのメモリ肥大と、プロキシプール(T42)使用時に
# 送信元IPが固定され続けるのを避けるため。
BROWSER_REUSE = os.environ.get("FORM_BROWSER_REUSE", "1").lower() not in ("0", "false", "no")
BROWSER_MAX_USES = int(os.environ.get("FORM_BROWSER_MAX_USES", "20"))
_BROWSER_TLS = threading.local()


def _close_browser_state(state):
    if not state:
        return
    for key, closer in (("browser", "close"), ("pw", "stop")):
        try:
            getattr(state[key], closer)()
        except Exception:  # noqa: BLE001
            pass


def close_thread_browser():
    """このスレッドが使い回しているブラウザを閉じる。送信ワーカーは1件の送信ごとではなく
    担当分を送り終えたときにこれを呼ぶこと(呼ばないとChromiumが残る)。
    Playwrightのsync APIはスレッドをまたいで触れないため、必ず使ったスレッド自身が呼ぶ。"""
    state = getattr(_BROWSER_TLS, "state", None)
    _BROWSER_TLS.state = None
    _close_browser_state(state)


def _acquire_browser(headless):
    """(state, owned) を返す。owned=Trueなら呼び出し側がその場で閉じる(再利用しない設定)。"""
    from playwright.sync_api import sync_playwright

    if not BROWSER_REUSE:
        pw = sync_playwright().start()
        return {"pw": pw, "browser": _launch_browser(pw, headless), "uses": 1}, True
    state = getattr(_BROWSER_TLS, "state", None)
    if state is not None and state["uses"] >= BROWSER_MAX_USES:
        close_thread_browser()
        state = None
    if state is None:
        pw = sync_playwright().start()
        state = {"pw": pw, "browser": _launch_browser(pw, headless), "uses": 0}
        _BROWSER_TLS.state = state
    state["uses"] += 1
    return state, False


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
    "phone": ["電話番号", "電話", "tel", "phone", "fax番号", "ご連絡先", "連絡先電話", "連絡先"],
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

# 2026-09-20: 実在の四国企業82社で検出ロジックだけを走らせたところ、到達できた77社のうち
# 28社(36%)が「問い合わせフォームが見つからない」だった。内訳を見ると原因は探索側に偏って
# いたため、以下を足がかりに作り直す(本番CSVの form_not_found 598件に対応)。
#
# パス側の手がかり。"form"は information に、"mail"は mailmagazine に部分一致してしまうため、
# 紛れ込みやすい語だけは区切り文字付きで見る(単純な部分一致リストに足すと誤爆する)。
_CONTACT_PATH_RE = re.compile(
    r"contact|inquiry|inquire|otoiawase|toiawase|o-toiawase|mailform|formmail|"
    r"soudan|consult|(^|[/_\-.])(form|mail|entry)([/_\-.0-9]|$)|"
    # 日本語パス(「お問い合わせ」「問合せ」)のパーセントエンコード
    r"%e3%81%8a%e5%95%8f|%e5%95%8f%e5%90%88|%e5%95%8f%e3%81%84%e5%90%88",
    re.I)

# リンク文言。表記ゆれを広めに取る(「お問い合せ」「ご相談」「お見積り」まで)
_CONTACT_TEXT_STRONG = ("お問い合わせ", "お問合せ", "お問合わせ", "お問い合せ", "問い合わせ",
                        "問合せ", "問い合せ", "ご相談", "お見積", "見積り依頼", "資料請求")
_CONTACT_TEXT_WEAK = ("contact", "inquiry", "inquiries", "enquiry", "get in touch")

# 「お問い合わせ」を含んでいても営業の宛先として不適切なリンク。実測で拾ってしまった例:
# 「お問合せ伝票番号検索」(配送追跡の検索ページ)、「採用に関するお問い合わせ」(採用窓口)。
_CONTACT_TEXT_NEGATIVE = ("採用", "求人", "エントリー", "recruit", "応募", "ログイン", "login",
                          "マイページ", "会員", "伝票番号", "追跡", "検索", "よくあるご質問",
                          "faq", "サイトマップ", "個人情報", "プライバシー")

_SUCCESS_HINTS = (
    "ありがとうございます", "ありがとうございました", "送信が完了", "送信しました",
    "送信いたしました", "送信されました", "受け付け", "受付ました", "受付いたしました",
    "受け付けました", "承りました", "お問い合わせいただき", "ご連絡いたします",
    "担当者より", "追ってご連絡", "確認の上", "確認次第", "折り返しご連絡",
    "thank you", "thanks for", "successfully",
)

# 旧CAPTCHA判定のセレクタ。広すぎて誤検出が多かったためT109で使用をやめた
# (判定本体は _BLOCKING_CAPTCHA_JS / _detect_captcha)。復活させないこと。
_CAPTCHA_SELECTORS_DEPRECATED = (
    "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", ".g-recaptcha",
    "[class*='captcha']", "[id*='captcha']",
)

_NO_SOLICIT_HINTS = [
    "営業目的の", "営業のご連絡", "営業メールはご遠慮", "セールスのご連絡はご遠慮",
    "営業のお電話", "勧誘目的", "営業・勧誘", "セールス・勧誘",
]

_RECRUIT_ONLY_HINTS = ["採用に関するお問い合わせ専用", "採用エントリー", "新卒採用専用", "中途採用専用"]
_SUPPORT_ONLY_HINTS = ["既存のお客様専用", "契約者様専用", "サポート専用窓口", "会員専用"]

# 送信後ページに明確な拒否・エラー文言が出ているのに、_SUCCESS_HINTS/url_changed/
# form_goneのどれかに引っかかって誤ってSUCCESS判定されるケースへの対策(2026-08-28、
# 「このフォームは日本国内からのみ送信可能です」という地域制限エラーが出ているのに
# フォームがエラーメッセージへ差し替わった<form_gone=True>ためSUCCESSと記録された
# 実インシデントで発見)。地域制限は本番サーバーが日本国外(Hetzner)にあることが
# 原因で、T42のプロキシプール(FORM_PROXY_POOL、国内IP)を実際に契約すれば
# 解消しうる。ここではまず「エラーを誤ってSUCCESSと記録しない」ことだけを担保する。
_ERROR_HINTS = (
    "日本国内からのみ", "国内からのみご利用", "国内からのみ送信", "海外からのアクセス",
    "国外からのアクセス", "海外からの送信", "送信に失敗しました", "送信できませんでした",
    "送信できません", "エラーが発生しました", "現在ご利用いただけません",
    "アクセスが制限されています", "不正なリクエスト",
    # 2026-09-19: フォーム側の入力検証エラー(確認画面やその場のエラー表示)。
    # 「入力内容に問題があります」「【お問い合わせ種別】が未選択です」「ご連絡先が未入力です」
    # が出ているのに、URL変化・文言一致でSUCCESSと記録された実インシデントへの対策。
    # 「選択してください」「入力してください」はプレースホルダーや注記として成功ページにも
    # 出うるため入れない(誤って失敗にする方向の副作用を避ける)
    "入力内容に問題", "入力にエラー", "入力エラー", "エラーがあります", "不備があります",
    "問題があります", "未選択です", "未入力です", "が未選択", "が未入力", "入力されていません",
    "選択されていません", "チェックされていません", "正しく入力してください", "正しくありません",
    "再度お試しください", "もう一度お試しください", "確認して再度", "必須項目が入力", "必須項目を入力",
    "is required", "required field", "please fill", "please enter a valid",
)

# Cloudflare等のボット検証チャレンジ画面。CAPTCHAと同じく自動突破の対象にはしない
_BOT_CHALLENGE_TITLE_HINTS = ["just a moment", "attention required", "checking your browser"]

# 2026-09-20: 実在サイトでの計測で、フォームは見つかっているのに送信ボタンだけ
# 見つからない例が多数あった(本番CSVの submit_button_not_found 271件)。実際に外した例:
#   「確認画面へ」(Contact Form 7の確認ステップ) / 「次へ」(旧実装は「次へ進む」のみ) /
#   「確認」(input[type=submit] value="確認") / 画像ボタン(input[type=image] のalt文言)
# 文字の間に空白を入れる表記(「送 信」「送　信」)も実在するため、判定前に空白を潰す。
_SUBMIT_TEXT_RE = re.compile(
    r"送信|送付|確認画面|確認する|確認へ|内容を確認|入力内容の確認|この内容で|上記内容で|"
    r"次へ|進む|申し込|申込|お申し?込み|問い合わせる|問合せる|相談する|submit|send|confirm|next",
    re.I)
_CONFIRM_TEXT_RE = re.compile(r"確認画面|入力内容を確認|内容を確認|次へ|確認する", re.I)

# 「送信」らしく見えても押してはいけないもの。サイト内検索ボタンを押すと検索結果へ
# 遷移してしまい、URLが変わったことで「送信成功」と誤記録されうる(実測で
# 「お問合せ伝票番号検索開始」という画像ボタンを拾った例がある)。
_NOT_SUBMIT_TEXT_RE = re.compile(
    r"検索|search|クリア|clear|リセット|reset|取り消|キャンセル|cancel|戻る|back|"
    r"閉じる|close|ログイン|login|絞り込|ダウンロード|download|同意|accept|拒否|deny",
    re.I)


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
FILLABLE_SELECTOR = ("input[type=text], input[type=email], input[type=tel], "
                      "input:not([type]), textarea")

# iframeで埋め込まれた外部フォーム(Googleフォーム・formrun・HubSpot等)も対象にする。
# ただしチャットウィジェットや広告・計測系のiframeは「入力欄がある」だけで拾ってしまう
# ので除外する(そこへ営業文を打ち込んでも相手には届かない)。
_FRAME_URL_DENY_RE = re.compile(
    r"recaptcha|hcaptcha|turnstile|googletagmanager|google-analytics|doubleclick|"
    r"youtube\.com|facebook\.com|twitter\.com|zendesk|intercom|chatplus|tawk|crisp|"
    r"channel\.io|karte|sync\.|adservice", re.I)


def _looks_like_contact_page(url):
    low = (url or "").lower()
    return any(h in low for h in _CONTACT_PATH_HINTS)


def _has_fillable_form(scope):
    """scopeはPageでもFrameでもよい(query_selectorの使い方が同じ)。"""
    try:
        return scope.query_selector(FILLABLE_SELECTOR) is not None
    except Exception:  # noqa: BLE001
        return False


def _form_scopes(page):
    """入力欄を持つスコープ(メインフレーム優先、次にiframe)を返す。
    2026-09-20: 旧実装はメインフレームしか見ておらず、外部フォームサービスを
    iframeで埋め込んでいるサイトが一律 form_not_found になっていた。"""
    scopes = []
    try:
        frames = list(page.frames)
    except Exception:  # noqa: BLE001
        return [page] if _has_fillable_form(page) else []
    main = None
    try:
        main = page.main_frame
    except Exception:  # noqa: BLE001
        pass
    for f in frames:
        if f is main:
            continue
        url = ""
        try:
            url = f.url or ""
        except Exception:  # noqa: BLE001
            pass
        if not url or url == "about:blank" or _FRAME_URL_DENY_RE.search(url):
            continue
        if _has_fillable_form(f):
            scopes.append(f)
    if _has_fillable_form(page):
        scopes.insert(0, page)   # メインフレームにあるならそれを最優先
    return scopes


def _wait_for_form(page, timeout_ms=None):
    """JSで後から描画されるフォームを待つ。実測(大塚テクノ等)では
    domcontentloadedの時点で入力欄0件、2秒後に5件というサイトがあり、
    待たずに判定していたことが form_not_found の一因だった。
    入力欄が現れた時点で即座に返るので、フォームがあるサイトでは待ち時間はほぼ増えない。"""
    if timeout_ms is None:
        timeout_ms = FORM_RENDER_WAIT_MS
    if timeout_ms <= 0:
        return
    try:
        page.wait_for_selector(FILLABLE_SELECTOR, timeout=timeout_ms, state="attached")
    except Exception:  # noqa: BLE001
        pass


def _looks_like_real_contact_form(page):
    """『トップページに検索窓やニュースレター登録欄があるだけ』を問い合わせフォームと
    誤認しないための強めの判定。

    2026-09-20に強化: 旧実装は「textareaが1つでもあれば本物」としていたため、
    フッターに使われていない入力欄を持つトップページで探索が止まり、実際の
    /contact/ まで辿り着けないサイトが実測で複数あった(赤松化成・喜多機械ほか)。
    いまは『同じform要素の中に 本文欄(textarea) と 連絡先らしい入力欄 が揃っている』
    ことを条件にする(検索窓・メール登録欄はこの条件を満たさない)。"""
    try:
        return bool(page.evaluate("""() => {
          for (const f of document.querySelectorAll('form')) {
            if (!f.querySelector('textarea')) continue;
            const others = f.querySelectorAll(
              'input[type=text], input[type=email], input[type=tel], input:not([type])');
            if (others.length >= 1) return true;
          }
          return false;
        }"""))
    except Exception:  # noqa: BLE001
        return False


def _contact_link_score(text, href):
    """問い合わせページらしさの点数。0以下なら候補にしない。
    旧実装は「条件に当たった最初のリンク」を無条件に採用していたため、
    mailto:リンク(page.gotoが必ず失敗する)や「お問合せ伝票番号検索」のような
    別物のページを選んでしまっていた。"""
    t = (text or "").strip()
    tl = t.lower()
    h = (href or "").strip()
    hl = h.lower()
    if not h or h.startswith("#") or hl.startswith(("javascript:", "mailto:", "tel:", "fax:")):
        # mailto:/tel: は「ページ」ではないので辿れない。旧実装はこれを選んで
        # 「問い合わせページへの遷移に失敗」で終わっていた
        return 0
    score = 0
    if any(k in t for k in _CONTACT_TEXT_STRONG):
        score += 10
    if any(k in tl for k in _CONTACT_TEXT_WEAK):
        score += 8
    if _CONTACT_PATH_RE.search(hl):
        score += 6
    if any(k in t or k in tl for k in _CONTACT_TEXT_NEGATIVE):
        # 減点方式だと「採用に関するお問い合わせ」(強い文言+contactを含むパス)が
        # 残ってしまうため、これらは点数に関わらず候補から外す
        return 0
    return score


def _find_contact_link(page, exclude=()):
    """ヘッダー/フッター/ナビゲーションから問い合わせページらしいリンクを探す。
    候補が複数あるときは点数の高いものを選ぶ(同点ならDOM順で先のもの)。
    リンク文言とhrefの取得は1回のevaluateでまとめて行う——1リンクずつCDPを往復する
    旧実装は、リンクが数百ある企業サイトで無視できない時間がかかっていた。"""
    try:
        links = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]'))
            .slice(0, 800).map(a => ({
              text: (a.innerText || a.getAttribute('title') ||
                     (a.querySelector('img') ? a.querySelector('img').getAttribute('alt') || '' : '')),
              href: a.getAttribute('href') || '',
              abs: a.href || ''}))""")
    except Exception:  # noqa: BLE001
        return None
    best, best_score = None, 0
    for l in links or []:
        absolute = (l.get("abs") or "").split("#")[0]
        if not absolute or absolute in exclude:
            continue
        score = _contact_link_score(l.get("text"), l.get("href"))
        if score > best_score:
            best, best_score = (l.get("href") or "", absolute), score
    return best


def _resolve_contact_page(page, start_url):
    """問い合わせページへ辿る。MAX_CRAWL_PAGESの範囲で「次に有力なリンク」を順に試す。

    2026-09-20に多段化: 旧実装は1階層しか辿らず、しかも最初に当たったリンクが外れ
    (会社案内・電話番号へのアンカー等)だとそこで打ち切っていた。実測では
    『トップ → 会社情報 → お問い合わせ』や『1つ目のリンクは外れだが2つ目が当たり』の
    サイトが少なくない。フォームが見つかった時点で打ち切るので、当たりのサイトでは
    往復は増えない。"""
    _wait_for_form(page)
    if _looks_like_real_contact_form(page):
        return page.url, None
    if _looks_like_contact_page(start_url) and _has_fillable_form(page):
        return page.url, None

    visited = set()
    for u in (start_url, page.url):
        if u:
            visited.add(u.split("#")[0])
    last_err = "問い合わせページへのリンクが見つからず"
    deadline = time.monotonic() + FORM_DISCOVER_BUDGET_MS / 1000.0
    for _ in range(MAX_CRAWL_PAGES):
        if time.monotonic() > deadline:
            last_err = "問い合わせページを探す時間の上限に達した"
            break
        found = _find_contact_link(page, exclude=visited)
        if not found:
            break
        _, absolute_url = found
        visited.add(absolute_url)
        try:
            page.goto(absolute_url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            last_err = f"問い合わせページへの遷移に失敗: {type(e).__name__}"
            continue
        _wait_for_form(page)
        if _form_scopes(page):
            return page.url, None
        last_err = "問い合わせページにフォームが見つからず"
    return page.url, last_err


# ── 検知系(送るべきでないフォームの判定) ─────────
def _page_text(page):
    try:
        return page.inner_text("body")
    except Exception:  # noqa: BLE001
        return ""


# T109: 「実際に人手が要るチャレンジ」だけを検出する。
# 旧実装は iframe[src*='recaptcha'] / [class*='captcha'] / [id*='captcha'] の存在だけで
# SKIP_CAPTCHAにしていたため、送信自体は普通に通る reCAPTCHA v3(スコア判定。画面には
# 右下のバッジが出るだけ)や、非表示のv2、単にclass名にcaptchaを含む枠まで除外していた。
# 実測(四国6,425件)ではこれが1,429件=22%を占め、最大の取りこぼしだった。
# 判定できないものは「とりあえず送ってみる」側に倒す(通らなければ結果として失敗が残るだけで、
# 相手に迷惑はかからない)。
_BLOCKING_CAPTCHA_JS = """() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity) > 0.1;
  };
  // reCAPTCHA v2「私はロボットではありません」/ hCaptcha のチェックボックス枠
  for (const f of document.querySelectorAll('iframe')) {
    const src = f.getAttribute('src') || '';
    if (!/recaptcha\/api2\/anchor|hcaptcha\.com\/captcha|turnstile/.test(src)) continue;
    if (src.includes('size=invisible')) continue;
    if (visible(f)) return 'checkbox_challenge';
  }
  for (const el of document.querySelectorAll('.g-recaptcha, .h-captcha, .cf-turnstile')) {
    if ((el.getAttribute('data-size') || '') === 'invisible') continue;
    if (visible(el)) return 'checkbox_challenge';
  }
  // 画像認証(表示された画像+それを書き写す入力欄)
  for (const img of document.querySelectorAll('img')) {
    const hint = ((img.getAttribute('src') || '') + ' ' + (img.getAttribute('alt') || '')
                  + ' ' + (img.className || '') + ' ' + (img.id || '')).toLowerCase();
    if (!/captcha|認証画像/.test(hint)) continue;
    if (visible(img)) return 'image_challenge';
  }
  return '';
}"""


def _detect_captcha(page):
    """人手でないと突破できないCAPTCHAがあるときだけTrueを返す(T109)。
    reCAPTCHA v3・非表示のv2(バッジのみ)は送信が通るので対象にしない。"""
    try:
        return bool(page.evaluate(_BLOCKING_CAPTCHA_JS))
    except Exception:  # noqa: BLE001
        # JSが動かないページでは、確実に人手が要るものだけ従来どおりの要素判定で拾う
        for sel in ("iframe[src*='recaptcha/api2/anchor']", ".g-recaptcha", ".h-captcha"):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
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


def _detect_submission_error(text):
    """送信後ページに明確な拒否・エラー文言があれば、その文言を返す(無ければNone)。
    マッチした場合はSUCCESS判定より優先させること(_ERROR_HINTSのコメント参照)。"""
    return next((h for h in _ERROR_HINTS if h in text), None)


def _detect_bot_challenge(page):
    try:
        title = (page.title() or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(h in title for h in _BOT_CHALLENGE_TITLE_HINTS)


# ── 送信ボタン ───────────────────────────
def _button_labels(el):
    """(見えている文言, 属性まで含めた手がかり)を返す。
    画像だけのボタン(<button><img alt="送信"></button> / <input type="image" alt="送信">)は
    innerTextが空で、旧実装では永久に見つけられなかった。value/alt/aria-label/title、
    さらにname/id/classまで手がかりにする(class="btn-submit"のような命名を拾うため)。
    「送 信」「送　信」のような字間空けの表記に当てるため空白は潰す。"""
    try:
        parts = el.evaluate("""e => {
            const tag = e.tagName.toLowerCase();
            const img = e.querySelector ? e.querySelector('img') : null;
            const visible = tag === 'input'
                ? (e.getAttribute('value') || e.getAttribute('alt') || '')
                : ((e.innerText || '') + ' ' + (img ? (img.getAttribute('alt') || '') : ''));
            const extra = [e.getAttribute('aria-label') || '', e.getAttribute('title') || '',
                           e.getAttribute('name') || '', e.id || '',
                           typeof e.className === 'string' ? e.className : '',
                           img ? (img.getAttribute('src') || '') : ''].join(' ');
            return [visible, visible + ' ' + extra];
        }""")
    except Exception:  # noqa: BLE001
        return "", ""
    squash = lambda t: re.sub(r"[\s\u3000]+", "", t or "")
    return squash(parts[0]), squash(parts[1])


def _is_submit_candidate(el, text_re):
    visible_text, blob = _button_labels(el)
    if _NOT_SUBMIT_TEXT_RE.search(visible_text):
        return False
    if text_re.search(visible_text):
        return True
    # 文言が無い(画像だけ等)ときに限り、属性まで含めた手がかりで判定する
    return not visible_text and bool(text_re.search(blob))


def _visible(el):
    try:
        return el.is_visible()
    except Exception:  # noqa: BLE001
        return False


def _find_button(scope, text_re, form_el=None):
    """送信ボタンを探す。form_elを渡すと、まずそのフォームの中だけを見る。

    2026-09-20の作り直し(submit_button_not_found対策):
    - query_selector(最初の1件だけ)をやめた。旧実装はページ先頭の非表示の
      input[type=submit](サイト内検索の残骸など)を1件だけ見て「見つからない」と
      判定し、そのすぐ下にある本物の送信ボタンへ辿り着けていなかった。
      しかも文言による探索の対象セレクタに input[type=submit] が入っていないため、
      一度ここで外すと二度と拾えない構造だった。
    - 対象フォームの中を優先する(ヘッダーのサイト内検索ボタンを押さないため)。
    - input[type=image](画像の送信ボタン)を対象に加えた。
    """
    roots = [r for r in (form_el, scope) if r is not None]
    # (1) type=submit / image を持つ要素。これが最も確実
    for root in roots:
        for sel in ("button[type=submit]", "input[type=submit]", "input[type=image]"):
            try:
                els = root.query_selector_all(sel)
            except Exception:  # noqa: BLE001
                continue
            for el in els:
                if _visible(el) and not _NOT_SUBMIT_TEXT_RE.search(_button_labels(el)[0]):
                    return el
    # (2) 文言・属性から送信ボタンらしいものを探す
    for root in roots:
        try:
            els = root.query_selector_all(
                "button, a, input[type=button], input[type=submit], input[type=image], [role=button]")
        except Exception:  # noqa: BLE001
            continue
        for el in els:
            if _visible(el) and _is_submit_candidate(el, text_re):
                return el
    return None


def _submit_form_directly(scope, form_el):
    """押せる送信ボタンが1つも無いとき、フォーム自体を送信する最後の手段。
    実測(大輪総合運輸)では <input type="submit" value="確認"> がCSSで潰されていて
    クリック対象にならず、送信ボタンが見つからない扱いになっていた。
    requestSubmit()はブラウザのHTML5検証(required等)をそのまま通すので、
    「必須欄が埋まっていないのに送ったことにする」誤判定は増えない。
    送信ボタンが1つも無いフォーム(検索窓等)では何もせずFalseを返す。"""
    if form_el is None:
        return False
    try:
        return bool(form_el.evaluate("""f => {
            const btn = f.querySelector(
              'input[type=submit], button[type=submit], input[type=image], button:not([type])');
            if (!btn) return false;
            if (typeof f.requestSubmit === 'function') { f.requestSubmit(btn.disabled ? undefined : btn); }
            else { btn.click(); }
            return true;
        }"""))
    except Exception:  # noqa: BLE001
        return False


def _owning_form(el):
    """入力欄が属する<form>要素。送信ボタンの探索範囲を絞るために使う。"""
    try:
        handle = el.evaluate_handle("e => e.closest('form')")
        return handle.as_element()
    except Exception:  # noqa: BLE001
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


def _invalid_visible_fields(page):
    """ブラウザのHTML5検証(required/pattern/type=email等)で「無効」になっている可視の入力欄の
    ラベルを返す。1つでもあればブラウザは送信をブロックし、ページは変わらず
    「Please fill out this field」の吹き出しが出るだけになる(2026-09-19の実インシデント:
    ふりがなの必須欄が未入力のままなのに、ページ内の文言一致でSUCCESSと記録されていた)。"""
    try:
        return page.evaluate("""() => {
          const out = [];
          for (const el of document.querySelectorAll('input, textarea, select')) {
            if (!el.willValidate || el.disabled) continue;
            const t = (el.type || '').toLowerCase();
            if (['hidden', 'submit', 'button', 'image', 'file', 'reset'].includes(t)) continue;
            if (!el.getClientRects().length) continue;
            if (el.checkValidity()) continue;
            let label = '';
            try {
              if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) label = l.textContent; }
              if (!label && el.closest('label')) label = el.closest('label').textContent;
            } catch (e) {}
            const name = (label || el.getAttribute('aria-label') || el.placeholder || el.name || el.id || t) + '';
            const clean = name.replace(/\s+/g, ' ').trim().slice(0, 40);
            if (!out.includes(clean)) out.push(clean);
          }
          return out;
        }""") or []
    except Exception:  # noqa: BLE001
        return []


def _form_keeps_our_values(page, values):
    """送信後もフォームに自分たちが入れた値(メールアドレス・本文)が残っているか。
    AJAXで送信成功後にフォームをリセットするサイトでは必須欄が空=無効に見えるため、
    「無効な欄がある」だけで失敗と決めず、値が残っている(=送られていない)ときだけ失敗にする。"""
    markers = [v for v in (values.get("email"), (values.get("message") or "")[:30]) if v]
    if not markers:
        return False
    try:
        return bool(page.evaluate("""(markers) => {
          for (const el of document.querySelectorAll('input, textarea')) {
            const v = (el.value || '');
            if (v && markers.some(m => v.includes(m))) return true;
          }
          return false;
        }""", markers))
    except Exception:  # noqa: BLE001
        return False


def _check_required_radios(page):
    """ラジオボタン群(お問い合わせ種別など)で1つも選ばれていないものは、
    「お問い合わせ/その他」寄りの選択肢があればそれを、無ければ先頭の可視の選択肢を選ぶ
    (プルダウンと同じ方針)。required属性が無くてもサーバー側で「未選択です」と弾かれる
    サイトが多い(2026-09-19の実インシデント)ため、必須に限らず全ての未選択の群に適用する。"""
    try:
        return int(page.evaluate("""() => {
          let n = 0;
          const groups = {};
          for (const el of document.querySelectorAll('input[type=radio]')) {
            const key = el.name || el.id;
            if (!key) continue;
            (groups[key] = groups[key] || []).push(el);
          }
          const labelOf = (el) => {
            let t = '';
            try {
              if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t = l.textContent; }
              if (!t && el.closest('label')) t = el.closest('label').textContent;
              if (!t && el.parentElement) t = el.parentElement.textContent;
            } catch (e) {}
            return (t || el.value || '').trim();
          };
          const prefer = /お問い合わせ|お問合せ|その他|ご相談|general|other/i;
          for (const name of Object.keys(groups)) {
            const els = groups[name].filter(e => !e.disabled && e.getClientRects().length);
            if (!els.length || groups[name].some(e => e.checked)) continue;
            const pick = els.find(e => prefer.test(labelOf(e))) || els[0];
            pick.checked = true;
            pick.dispatchEvent(new Event('input', { bubbles: true }));
            pick.dispatchEvent(new Event('change', { bubbles: true }));
            n++;
          }
          return n;
        }""") or 0)
    except Exception:  # noqa: BLE001
        return 0


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
                # navigate_and_submit()と同じ判定にする(iframe内の埋め込みフォームも
                # 「見つかった」とみなす)。ここだけメインフレームしか見ないと、
                # CSV取込で埋まるcontact_urlと実際に送信できる範囲がズレる
                if _form_scopes(page):
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
    state = None
    owned = False
    try:
        state, owned = _acquire_browser(headless)
        context = state["browser"].new_context()
        try:
            page = context.new_page()
        except Exception:  # noqa: BLE001
            context.close()
            raise
        try:
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

            # フォームはメインフレームとは限らない(Googleフォーム等のiframe埋め込み)。
            # 以降の入力・送信はこのスコープに対して行う(PageでもFrameでもAPIは同じ)。
            scopes = _form_scopes(page)
            if not scopes:
                result.status = "FAILED_UNSUPPORTED"
                result.reason_code = discover_err or "form_not_found"
                result.page_text_snippet = page_text[:400]
                return result
            scope = scopes[0]

            fields = scope.query_selector_all(
                "input[type=text], input[type=email], input[type=tel], "
                "input:not([type]), textarea")
            detected, filled = {}, []
            first_filled_el = None
            for el in fields:
                try:
                    if not el.is_visible():
                        continue
                except Exception:  # noqa: BLE001
                    continue
                kind = _classify_field(scope, el)
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
                        if first_filled_el is None:
                            first_filled_el = el
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
            n_selects = _fill_selects(scope)
            if n_selects:
                detected["select"] = n_selects
                filled.append(f"select×{n_selects}")

            result.detected_fields = detected
            result.filled_fields = filled

            # 同意チェックボックス
            try:
                for cb in scope.query_selector_all("input[type=checkbox]"):
                    if not cb.is_visible() or cb.is_checked():
                        continue
                    if any(h in (_label_for(scope, cb) or "") for h in _CONSENT_HINTS):
                        cb.check(timeout=ACTION_TIMEOUT_MS)
            except Exception:  # noqa: BLE001
                pass

            # 必須のラジオ群が未選択なら先頭を選ぶ(2026-09-19)
            n_radios = _check_required_radios(scope)
            if n_radios:
                filled.append(f"radio×{n_radios}")
                result.filled_fields = filled

            # 送信前検証(2026-09-19): 埋められなかった必須欄(ふりがな等)や形式不一致が
            # 残っていればブラウザが送信をブロックするので、押しても送られない。
            # その状態を「成功」と誤記録しないため、ここで失敗として記録し、
            # どの欄が埋まらなかったかを残す(「自動入力」での手動フォローに使える)
            missing = _invalid_visible_fields(scope)
            if missing:
                result.status = "FAILED_UNSUPPORTED"
                result.reason_code = "required_field_unfilled"
                result.error_message = "自動で埋められない必須欄があるため送信していません: " + "・".join(missing)
                result.page_text_snippet = _page_text(page)[:400]
                result.screenshot_after_path = _save_screenshot(
                    page, screenshot_dir, result.run_id, "after")
                return result

            # 埋めた欄が属する<form>の中を優先して探す(ヘッダーのサイト内検索ボタンを
            # 押してしまうと、URLが変わるだけで「送信成功」と誤記録されうる)
            form_el = _owning_form(first_filled_el) if first_filled_el is not None else None
            submit_btn = _find_button(scope, _SUBMIT_TEXT_RE, form_el=form_el)
            if not submit_btn:
                # 入力欄より少し遅れて送信ボタンが描画されるサイトがある
                # (実測: 四国トーセロ。入力欄はある状態で送信ボタンだけ未描画だった)。
                # 諦める前に一度だけ待ち直す
                try:
                    page.wait_for_timeout(POST_SUBMIT_WAIT_MS)
                except Exception:  # noqa: BLE001
                    pass
                submit_btn = _find_button(scope, _SUBMIT_TEXT_RE, form_el=form_el)
            if submit_btn:
                result.submit_attempted = True
                if not _click(submit_btn):
                    result.status = "FAILED_RETRYABLE"
                    result.reason_code = "submit_click_failed"
                    return result
            elif _submit_form_directly(scope, form_el):
                # 押せる送信ボタンは無かったが、フォーム自体は送信できた
                # (CSSで潰された input[type=submit] 等。_submit_form_directly参照)
                result.submit_attempted = True
            else:
                result.status = "FAILED_UNSUPPORTED"
                result.reason_code = "submit_button_not_found"
                result.page_text_snippet = _page_text(page)[:400]
                return result
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
            except Exception:  # noqa: BLE001
                pass

            # 入力→確認→送信の2段階フォーム対応。確認画面が残っていればもう一度押す
            confirm_btn = (_find_button(scope, _CONFIRM_TEXT_RE)
                           or _find_button(scope, _SUBMIT_TEXT_RE))
            if confirm_btn:
                _click(confirm_btn)
                try:
                    page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
                except Exception:  # noqa: BLE001
                    pass

            # AJAX送信の完了メッセージが非同期で少し遅れて描画されるサイトがあるため、
            # networkidleの後にもう少しだけ待つ
            try:
                page.wait_for_timeout(POST_SUBMIT_WAIT_MS)
            except Exception:  # noqa: BLE001
                pass

            result.screenshot_after_path = _save_screenshot(
                page, screenshot_dir, result.run_id, "after")
            result.final_url = page.url
            # 完了文言はiframe側に出ることがあるので、フォームのスコープの文言も足して見る
            final_text = _page_text(page)
            if scope is not page:
                final_text += "\n" + _page_text(scope)
            result.page_text_snippet = final_text[:400]
            # 明確な拒否・エラー文言が出ていれば、以下のSUCCESS判定(文言一致/URL変化/
            # フォーム消失)より優先する。「日本国内からのみ送信可能です」という
            # 地域制限エラーで、フォームがエラーメッセージへ差し替わった
            # (=form_gone成立)ためSUCCESSと誤記録された実インシデントへの対策。
            # 送信後もブラウザ検証で無効な欄が残り、入力した値もそのまま残っている
            # =ブラウザが送信をブロックした(JSで後から必須になった欄など)。
            # 文言一致・URL変化・フォーム消失の判定より優先する(2026-09-19)
            still_invalid = _invalid_visible_fields(scope)
            if still_invalid and _has_fillable_form(scope) and _form_keeps_our_values(scope, values):
                result.status = "FAILED_UNSUPPORTED"
                result.reason_code = "required_field_empty"
                result.error_message = "必須欄が未入力のまま送信がブロックされました: " + "・".join(still_invalid)
                return result
            error_hit = _detect_submission_error(final_text)
            if error_hit:
                result.status = "FAILED_UNSUPPORTED"
                result.reason_code = "error_message_detected"
                result.error_message = f"送信後ページにエラー文言を検知: {error_hit}"
                return result
            url_changed = page.url != contact_url
            # フォームがDOM上から消えている(=AJAXで完了画面に差し替わった)ことも
            # 成功の傍証として見る。文言・URLどちらも一致しないAJAX系フォーム向けの保険
            form_gone = not _form_scopes(page)
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
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        # 使い回しているブラウザ自体が壊れている可能性があるので捨てる(次の会社で起動し直す)
        close_thread_browser()
        result.status = "FAILED_RETRYABLE"
        result.reason_code = "unexpected_error"
        result.error_message = f"{type(e).__name__}: {e}"
        return result
    finally:
        if owned:
            _close_browser_state(state)


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
                # T109以降は「見えるチェックボックス枠」だけがCAPTCHA扱い(大きさ指定が必要)
                ("CAPTCHA", '<div class="g-recaptcha" style="width:304px;height:78px"></div>',
                 _detect_captcha, "page"),
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

            print("\n── 送信後エラー文言の誤SUCCESS化防止(2026-08-28、実インシデントで発見) ──")
            e1 = _detect_submission_error("エラー: このフォームは日本国内からのみ送信可能です。")
            print(f"  {'✓' if e1 else '✗'} 地域制限エラー文言を検知できる: {e1!r}")
            e2 = _detect_submission_error(
                "お問い合わせいただきありがとうございます。担当者より追ってご連絡いたします。")
            print(f"  {'✓' if e2 is None else '✗'} 通常の完了ページはエラー扱いにしない")

            print("\n── CAPTCHA判定(T109。人手が要るものだけ除外する) ──")
            captcha_cases = [
                ("reCAPTCHA v3(バッジのみ。送信は通る)", """
                    <form><input name=x><button type=submit>送信</button></form>
                    <div class="grecaptcha-badge" style="width:70px;height:60px"></div>
                    <iframe src="https://www.google.com/recaptcha/api2/anchor?size=invisible&k=x"
                            style="width:0;height:0"></iframe>""", False),
                ("非表示のv2(data-size=invisible)", """
                    <div class="g-recaptcha" data-size="invisible" style="width:300px;height:78px"></div>""", False),
                ("class名にcaptchaを含むだけの枠", """
                    <div class="captcha-wrapper" style="display:none"><input name="captcha"></div>""", False),
                ("v2チェックボックス(人手が要る)", """
                    <div class="g-recaptcha" style="width:304px;height:78px"></div>""", True),
                ("v2チェックボックスのiframe", """
                    <iframe src="https://www.google.com/recaptcha/api2/anchor?k=x"
                            style="width:304px;height:78px"></iframe>""", True),
                ("画像認証(書き写し式)", """
                    <img src="/inc/captcha_image.php" alt="認証画像" style="width:120px;height:40px">
                    <input name="auth">""", True),
            ]
            for label, html, expect in captcha_cases:
                page.set_content(html)
                got = _detect_captcha(page)
                print(f"  {'✓' if got == expect else '✗'} {label}: "
                      f"{'除外する' if got else '送信を試みる'}(期待: {'除外' if expect else '送信'})")

            print("\n── 問い合わせページの探索(2026-09-20。実在82社の計測で作り直し) ──")
            link_cases = [
                ("お問い合わせ", "/contact/", True, "ふつうの問い合わせリンク"),
                ("お問い合わせはこちら", "mailto:info@example.co.jp", False,
                 "mailto:は辿れない(page.gotoが必ず失敗する)ので候補にしない"),
                ("お電話でのお問い合わせ", "tel:088-000-0000", False, "tel:も同様"),
                ("お問合せ伝票番号検索開始", "/webtrace/", False,
                 "『お問合せ』を含むが配送追跡の検索ページ"),
                ("採用に関するお問い合わせ", "/recruit/contact/", False, "採用窓口は営業の宛先ではない"),
                ("Contact", "/en/contact/", True, "英語表記(大文字small文字を問わない)"),
                ("会社概要", "/company/", False, "無関係なリンク"),
                ("ご相談・お見積り", "/estimate/", True, "『お問い合わせ』以外の言い回し"),
                ("トップへ", "#top", False, "ページ内アンカー"),
            ]
            for text, href, expect, why in link_cases:
                got = _contact_link_score(text, href) > 0
                print(f"  {'✓' if got == expect else '✗'} {'候補にする' if expect else '候補にしない'}: "
                      f"{text!r} → {href} ({why})")
            # 点数順: 「お問い合わせ」(本命)がDOM順で後ろにあっても選べること
            page.set_content("""
                <a href="/company/">会社概要</a>
                <a href="mailto:a@example.co.jp">お問い合わせはこちら</a>
                <a href="/recruit/">採用に関するお問い合わせ</a>
                <a href="/contact/">お問い合わせ</a>""")
            picked = _find_contact_link(page)
            print(f"  {'✓' if picked and picked[0] == '/contact/' else '✗'} "
                  f"複数候補から点数の高いものを選ぶ(DOM順の最初ではない): {picked[0] if picked else None}")

            print("\n── 『本物の問い合わせフォーム』の判定(トップページで探索を止めない) ──")
            form_cases = [
                ("検索窓だけのトップページ",
                 '<form><input type="text" name="s"><button>検索</button></form>', False),
                ("使われていないtextareaが1つあるだけのトップページ",
                 '<textarea id="memo"></textarea><a href="/contact/">お問い合わせ</a>', False),
                ("本文欄と連絡先欄が揃った問い合わせフォーム",
                 '<form><input type="text" name="name"><textarea name="body"></textarea>'
                 '<button type="submit">送信</button></form>', True),
            ]
            for label, html, expect in form_cases:
                page.set_content(html)
                got = _looks_like_real_contact_form(page)
                print(f"  {'✓' if got == expect else '✗'} {label}: "
                      f"{'ここで確定' if got else '問い合わせページを探しに行く'}")

            print("\n── iframeで埋め込まれたフォーム(Googleフォーム等) ──")
            page.set_content("""
                <p>お問い合わせはこちらのフォームから</p>
                <iframe srcdoc="&lt;form&gt;&lt;input name=name&gt;&lt;textarea&gt;&lt;/textarea&gt;
                    &lt;button type=submit&gt;送信&lt;/button&gt;&lt;/form&gt;"></iframe>""")
            page.wait_for_timeout(300)
            iframe_scopes = _form_scopes(page)
            print(f"  {'✓' if len(iframe_scopes) == 1 and iframe_scopes[0] is not page else '✗'} "
                  f"メインフレームに入力欄が無くてもiframe内のフォームを見つける(検出数={len(iframe_scopes)})")
            page.set_content("""
                <form><input name="a"><textarea></textarea></form>
                <iframe src="https://www.google.com/recaptcha/api2/anchor?k=x"></iframe>""")
            page.wait_for_timeout(300)
            main_first = _form_scopes(page)
            print(f"  {'✓' if main_first and main_first[0] is page else '✗'} "
                  f"メインフレームにフォームがあればそちらを優先する")

            print("\n── 送信ボタンの検出(submit_button_not_found 271件への対策) ──")
            btn_cases = [
                ("確認画面へ(Contact Form 7)",
                 '<form><input name="a"><button type="button" class="cf7-fake-confirm">確認画面へ</button></form>',
                 "確認画面へ"),
                ("次へ(旧実装は「次へ進む」しか見ていなかった)",
                 '<form><input name="a"><button type="button">次へ</button></form>', "次へ"),
                ('input[type=submit] value="確認"',
                 '<form><input name="a"><input type="submit" value="確認"></form>', "確認"),
                ("画像の送信ボタン",
                 '<form><input name="a"><input type="image" alt="送信する" '
                 'src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"></form>',
                 "送信する"),
                ("字間を空けた表記(送 信)",
                 '<form><input name="a"><button type="button">送 信</button></form>', "送 信"),
                ("先頭に非表示のsubmitがあっても本物を見つける",
                 '<form style="display:none"><input type="submit" value="検索"></form>'
                 '<form><input name="a"><input type="submit" value="送信する"></form>', "送信する"),
            ]
            for label, html, expect in btn_cases:
                page.set_content(html)
                b2 = _find_button(page, _SUBMIT_TEXT_RE)
                got = ""
                if b2:
                    got = b2.evaluate(
                        "e => (e.innerText || e.getAttribute('value') || e.getAttribute('alt') || '').trim()")
                ok_btn = got.replace(" ", "").replace("\u3000", "") == expect.replace(" ", "")
                print(f"  {'✓' if ok_btn else '✗'} {label}: 検出={got!r}")

            page.set_content("""
                <form id="search"><input type="text" name="q"><input type="submit" value="検索"></form>
                <form id="contact"><input name="name"><textarea></textarea>
                  <button type="submit">送信する</button></form>""")
            contact_form = page.query_selector("#contact")
            in_form = _find_button(page, _SUBMIT_TEXT_RE, form_el=contact_form)
            got_in = in_form.evaluate("e => (e.innerText || e.getAttribute('value') || '').trim()") if in_form else ""
            print(f"  {'✓' if got_in == '送信する' else '✗'} 対象フォームの中を優先し、サイト内検索の"
                  f"「検索」ボタンを押さない: 検出={got_in!r}")
            page.set_content('<form><input type="text" name="q"><input type="submit" value="検索"></form>')
            print(f"  {'✓' if _find_button(page, _SUBMIT_TEXT_RE) is None else '✗'} "
                  f"検索ボタンしか無いページでは送信ボタン無しと判定する")

            print("\n── 押せる送信ボタンが無いフォームを直接送信する(最後の手段) ──")
            page.set_content("""
                <form id="f" onsubmit="window.__submitted = 1; return false;">
                  <input name="a" value="x">
                  <input type="submit" value="確認" style="display:none">
                </form>""")
            hidden_form = page.query_selector("#f")
            no_btn = _find_button(page, _SUBMIT_TEXT_RE, form_el=hidden_form) is None
            sent = _submit_form_directly(page, hidden_form)
            fired = page.evaluate("() => window.__submitted === 1")
            print(f"  {'✓' if no_btn else '✗'} CSSで隠された送信ボタンはクリック対象にならない")
            print(f"  {'✓' if sent and fired else '✗'} それでもフォーム自体は送信できる(submit={sent})")
            page.set_content('<form id="g"><input type="text" name="q"></form>')
            no_submit_form = page.query_selector("#g")
            print(f"  {'✓' if not _submit_form_directly(page, no_submit_form) else '✗'} "
                  f"送信ボタンを持たないフォーム(検索窓等)は送信しない")

            print("\n── 探索の打ち切り(フォームが無い会社に時間をかけすぎない) ──")
            # 問い合わせリンクだけが延々と繋がっていてフォームに辿り着かないページを作り、
            # MAX_CRAWL_PAGES回まわる前に時間で打ち切られることを確認する
            _orig_budget = FORM_DISCOVER_BUDGET_MS
            _orig_render = FORM_RENDER_WAIT_MS
            try:
                sys.modules[__name__].FORM_DISCOVER_BUDGET_MS = 300
                sys.modules[__name__].FORM_RENDER_WAIT_MS = 200
                # 毎回ちがう問い合わせリンクだけを返す(=フォームに永久に辿り着かない)
                # ページを擬似的に用意し、MAX_CRAWL_PAGES回まわりきる前に時間で
                # 打ち切られることを見る。set_contentだと遷移自体が失敗して
                # 「遷移に失敗」で抜けてしまい、上限の判定を通らない
                hop = {"n": 0}

                def _endless(route):
                    hop["n"] += 1
                    route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                  body=f'<a href="/c{hop["n"]}/contact/">お問い合わせ</a>')

                page.route("**/*", _endless)
                try:
                    page.goto("https://example.test/", wait_until="domcontentloaded")
                    t_start = time.monotonic()
                    _, budget_err = _resolve_contact_page(page, "https://example.test/")
                    elapsed = time.monotonic() - t_start
                finally:
                    page.unroute("**/*")
                ok_budget = (budget_err == "問い合わせページを探す時間の上限に達した"
                             and hop["n"] < MAX_CRAWL_PAGES + 2 and elapsed < 10)
                print(f"  {'✓' if ok_budget else '✗'} 上限を過ぎたら探索を打ち切る"
                      f"({elapsed:.1f}秒 / 開いたページ{hop['n']}枚, 理由={budget_err!r})")
            finally:
                sys.modules[__name__].FORM_DISCOVER_BUDGET_MS = _orig_budget
                sys.modules[__name__].FORM_RENDER_WAIT_MS = _orig_render

            print("\n── ブラウザ使い回し(T107。1社ごとの起動をやめて所要時間を削る) ──")
            # このテストは既にsync_playwrightを1つ起動済みなので、同じドライバから
            # 別ブラウザを1つ作り、stop()だけ何もしないダミーを挟んで使い回しの挙動を見る
            # (close_thread_browser()でテスト本体のドライバまで止めてしまわないため)
            class _NoStop:
                def stop(self):
                    pass
            reuse_browser = _launch_browser(pw_ctx, True)
            _BROWSER_TLS.state = {"pw": _NoStop(), "browser": reuse_browser, "uses": 0}
            st1, owned1 = _acquire_browser(True)
            st2, owned2 = _acquire_browser(True)
            same = st1 is st2 and st1["browser"] is reuse_browser and not owned1 and not owned2
            print(f"  {'✓' if same and st2['uses'] == 2 else '✗'} 2社目は同じブラウザを使い回す(起動し直さない。uses={st2['uses']})")
            ctx_ok = False
            try:
                c = reuse_browser.new_context()
                c.new_page().set_content("<p>ok</p>")
                c.close()
                ctx_ok = True
            except Exception as ctx_e:  # noqa: BLE001
                print(f"    context error: {ctx_e}")
            print(f"  {'✓' if ctx_ok else '✗'} 使い回したブラウザから会社ごとのコンテキストを作れる(Cookieは毎回まっさら)")
            close_thread_browser()
            cleared = getattr(_BROWSER_TLS, "state", None) is None
            closed = not reuse_browser.is_connected()
            print(f"  {'✓' if cleared else '✗'} 閉じた後はスレッドに残らない")
            print(f"  {'✓' if closed else '✗'} close_thread_browser()でChromiumが実際に終了する(送信後に残さない)")
            print(f"  {'✓' if SETTLE_TIMEOUT_MS <= 8000 and POST_SUBMIT_WAIT_MS <= 2000 else '✗'} "
                  f"送信後の待ちが短縮されている(settle={SETTLE_TIMEOUT_MS}ms, 追加待ち={POST_SUBMIT_WAIT_MS}ms)")

            print("\n── フォーム側の入力検証エラーを成功と誤判定しない(2026-09-19、実インシデント3件) ──")
            for txt in ("入力内容に問題があります。確認して再度お試しください。",
                        "入力にエラーがあります。下記をご確認の上「戻る」ボタンにて修正をお願い致します。【お問い合わせ種別】が未選択です。",
                        "ご連絡先が未入力です。お問い合わせ項目がチェックされていません。"):
                hit = _detect_submission_error(txt)
                print(f"  {'✓' if hit else '✗'} エラー文言を検知: {txt[:22]}… → {hit!r}")
            ok_np = _detect_submission_error("お問い合わせ種別\n選択してください\nお問い合わせいただきありがとうございます。") is None
            print(f"  {'✓' if ok_np else '✗'} プレースホルダー「選択してください」だけの完了ページはエラー扱いにしない")
            page.set_content("""
                <form>
                  <label for="tel">ご連絡先</label><input id="tel" name="contact" placeholder="例：012-345-6789">
                  <p>お問い合わせ項目</p>
                  <label><input type="radio" name="item" value="kaitai">解体工事</label>
                  <label><input type="radio" name="item" value="ashiba">足場工事</label>
                  <label><input type="radio" name="item" value="other">その他</label>
                </form>""")
            tel_kind = _classify_field(page, page.query_selector("#tel"))
            print(f"  {'✓' if tel_kind == 'phone' else '✗'} 「ご連絡先」欄を電話番号と認識する: {tel_kind}")
            n_r2 = _check_required_radios(page)
            picked = page.evaluate("() => (document.querySelector('input[type=radio]:checked') || {}).value")
            print(f"  {'✓' if n_r2 == 1 and picked == 'other' else '✗'} 必須指定の無い未選択ラジオ群も「その他」を選ぶ: {picked}")

            print("\n── 必須欄の未入力を成功と誤判定しない(2026-09-19、実インシデントで発見) ──")
            page.set_content("""
                <form>
                  <label for="k">ふりがな</label><input id="k" name="kana" required>
                  <label for="e">メールアドレス</label><input id="e" type="email" required value="a@example.co.jp">
                  <label for="m">内容</label><textarea id="m" required>こんにちは。本文です。</textarea>
                  <input type="radio" name="kind" value="1" required id="r1"><label for="r1">お問い合わせ</label>
                  <input type="radio" name="kind" value="2" id="r2"><label for="r2">その他</label>
                  <button type="submit">送信する</button>
                </form>
                <p>お問い合わせいただきありがとうございます(テンプレート文言)</p>""")
            n_r = _check_required_radios(page)
            inv = _invalid_visible_fields(page)
            print(f"  {'✓' if n_r == 1 else '✗'} 必須ラジオ群が未選択なら先頭を選ぶ: {n_r}")
            print(f"  {'✓' if inv == ['ふりがな'] else '✗'} 未入力の必須欄(ふりがな)を検出し、埋めた欄は含めない: {inv}")
            keeps = _form_keeps_our_values(page, {"email": "a@example.co.jp", "message": "こんにちは。本文です。"})
            print(f"  {'✓' if keeps else '✗'} 入力した値がフォームに残っていることを検知できる")
            page.fill("#k", "ふりがな")
            inv2 = _invalid_visible_fields(page)
            print(f"  {'✓' if inv2 == [] else '✗'} 埋めれば無効な欄は無くなる: {inv2}")
            page.fill("#e", "")
            page.fill("#m", "")
            keeps2 = _form_keeps_our_values(page, {"email": "a@example.co.jp", "message": "こんにちは。本文です。"})
            print(f"  {'✓' if not keeps2 else '✗'} 送信成功後にリセットされたフォーム(値が消えた)は失敗にしない")

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
