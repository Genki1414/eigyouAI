"""
senders.py — 送信アダプタ層
チャネル（FAX / メール / SMS / 郵送）ごとに事業者は違うが、呼び出し側から見た形は同じにする。
本番で事業者を差し替えても campaign.py / followup.py を触らなくて済むようにするのが目的。

設計の要点:
  - send() は必ず SendResult を返す。例外は投げず、失敗も結果として返す（一括送信が止まらない）
  - 恒久エラー(宛先不明・番号不正)は permanent=True で返し、呼び出し側が配信停止に入れる
  - 冪等キーを必ず受け取る。同じキーの再送はアダプタ層で弾く
  - レート制御はアダプタが自分で持つ（事業者ごとに上限が違うため）
  - 送信者情報の付与はここで強制する（特定電子メール法。文面生成側の実装漏れを防ぐ）

本番実装で差すもの:
  MailSender  → SendGrid / Amazon SES
  FaxSender   → 秒速FAX / メッセージプラス
  SmsSender   → Twilio / KDDI Message Cast
  PostSender  → ハガキ・封書の印刷発送代行API

  python3 senders.py test    # モックで一通り動かす
"""
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import resilience as R


@dataclass
class SendResult:
    ok: bool
    provider_id: Optional[str] = None      # 事業者側のメッセージID（追跡に使う）
    error: Optional[str] = None
    permanent: bool = False                # True=再送しても無駄（宛先不明など）
    cost_yen: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class Recipient:
    company_id: int
    name: str
    email: Optional[str] = None
    fax: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    contact_url: Optional[str] = None


@dataclass
class Sender:
    """テナントの送信者情報。特定電子メール法の表示義務を満たすために必須。"""
    name: str
    email: str
    address: str
    optout_url: str


# ── 基底クラス ──────────────────────────────
class BaseSender:
    channel = "base"
    unit_cost_yen = 0
    rate_service = "default"

    def __init__(self, con, dry_run=True):
        self.con = con
        self.dry_run = dry_run
        self.rl = R.limiter_for(self.rate_service)
        self.idem = R.Idempotency(con)

    # 各アダプタが実装する
    def _deliver(self, to: Recipient, sender: Sender, subject, body) -> SendResult:
        raise NotImplementedError

    def validate(self, to: Recipient) -> Optional[str]:
        """宛先が使えるか。使えない理由を返す（Noneなら可）"""
        raise NotImplementedError

    def footer(self, sender: Sender) -> str:
        """全チャネル共通の送信者表示。ここで強制するので文面側の漏れが起きない。"""
        return (f"\n\n──────────\n{sender.name}\n{sender.address}\n"
                f"{sender.email}\n配信停止: {sender.optout_url}")

    def send(self, to: Recipient, sender: Sender, subject, body, idem_key) -> SendResult:
        # 1. 二重送信の防止
        if self.con.execute("SELECT 1 FROM idempotency WHERE key=?", (idem_key,)).fetchone():
            return SendResult(ok=True, provider_id="skipped", error="送信済み（冪等キー一致）")

        # 2. 宛先の検証
        why = self.validate(to)
        if why:
            return SendResult(ok=False, error=why, permanent=True)

        # 3. レート制御
        self.rl.acquire()

        # 4. 送信（一時エラーのみ再試行）
        full_body = body + self.footer(sender)
        try:
            res = R.retry(lambda: self._deliver(to, sender, subject, full_body),
                          attempts=4, job=f"{self.channel}:{to.name}")
        except Exception as e:  # noqa: BLE001
            return SendResult(ok=False, error=str(e)[:200],
                              permanent=isinstance(e, R.Fatal))

        # 5. 成功したら冪等キーを記録
        if res.ok:
            self.idem.record(idem_key, {"provider_id": res.provider_id,
                                        "at": datetime.now().isoformat(timespec="seconds")})
            res.cost_yen = self.unit_cost_yen
        return res


# ── メール ─────────────────────────────────
class MailSender(BaseSender):
    channel = "メール"
    unit_cost_yen = 1
    rate_service = "sendgrid"
    EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    def validate(self, to):
        if not to.email:
            return "メールアドレス未取得"
        if not self.EMAIL_RE.match(to.email):
            return f"メールアドレス形式不正: {to.email}"
        return None

    def _deliver(self, to, sender, subject, body):
        if self.dry_run:
            return SendResult(ok=True, provider_id=f"mock_mail_{uuid.uuid4().hex[:10]}")
        # 本番: SendGrid
        # import sendgrid
        # sg = sendgrid.SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
        # r = sg.send(Mail(from_email=sender.email, to_emails=to.email,
        #                  subject=subject, plain_text_content=body))
        # 401/403 は Fatal（キー不正なので再試行しても無駄）
        raise NotImplementedError("SENDGRID_API_KEY を設定して本実装に差し替える")


# ── FAX ────────────────────────────────────
class FaxSender(BaseSender):
    channel = "FAX"
    unit_cost_yen = 12
    rate_service = "fax_api"

    def validate(self, to):
        if not to.fax:
            return "FAX番号未取得"
        digits = re.sub(r"\D", "", to.fax)
        if not (9 <= len(digits) <= 11) or not digits.startswith("0"):
            return f"FAX番号形式不正: {to.fax}"
        return None

    def footer(self, sender):
        # FAXは用紙1枚に収める必要があるので簡潔に
        return (f"\n\n{sender.name} / {sender.email}\n"
                f"今後の送付が不要な場合は、本紙にその旨ご記入のうえご返信ください。")

    def _deliver(self, to, sender, subject, body):
        if self.dry_run:
            return SendResult(ok=True, provider_id=f"mock_fax_{uuid.uuid4().hex[:10]}")
        raise NotImplementedError("FAX事業者APIに差し替える（秒速FAX / メッセージプラス等）")


# ── SMS ────────────────────────────────────
class SmsSender(BaseSender):
    channel = "SMS"
    unit_cost_yen = 8
    rate_service = "sms"
    MAX_CHARS = 660

    def validate(self, to):
        if not to.phone:
            return "電話番号未取得"
        digits = re.sub(r"\D", "", to.phone)
        if not digits.startswith("0") or len(digits) not in (10, 11):
            return f"電話番号形式不正: {to.phone}"
        # 固定電話にSMSは届かない
        if not digits.startswith(("070", "080", "090")):
            return "携帯番号ではない（SMS不達）"
        return None

    def send(self, to, sender, subject, body, idem_key):
        if len(body) > self.MAX_CHARS:
            return SendResult(ok=False, error=f"本文が長すぎる({len(body)}字 > {self.MAX_CHARS})",
                              permanent=True)
        return super().send(to, sender, subject, body, idem_key)

    def footer(self, sender):
        return f"\n{sender.name}\n停止:{sender.optout_url}"

    def _deliver(self, to, sender, subject, body):
        if self.dry_run:
            return SendResult(ok=True, provider_id=f"mock_sms_{uuid.uuid4().hex[:10]}")
        raise NotImplementedError("SMS事業者APIに差し替える")


# ── 郵送DM ─────────────────────────────────
class PostSender(BaseSender):
    channel = "郵送DM"
    unit_cost_yen = 95
    rate_service = "default"

    def validate(self, to):
        if not to.address:
            return "住所未取得"
        if len(to.address) < 6:
            return f"住所が不完全: {to.address}"
        return None

    def _deliver(self, to, sender, subject, body):
        if self.dry_run:
            return SendResult(ok=True, provider_id=f"mock_post_{uuid.uuid4().hex[:10]}")
        raise NotImplementedError("印刷発送代行APIに差し替える")


# ── 問い合わせフォーム自動送信 ─────────────────
# サイトごとにフィールド構成が違うため、name/id/placeholder/labelの文言を
# ヒューリスティックで見て用途を推定する。完全な保証はできない前提の設計。
# reCAPTCHA等を検出したら自動突破はせず諦める(そのまま失敗として記録し、人手 or
# 他チャネルに任せる。permanent=Falseなので配信停止には入らない＝再挑戦の余地は残す)。
_FIELD_HINTS = {
    "email": ["メール", "eメール", "e-mail", "email"],
    "phone": ["電話", "tel", "phone"],
    "message": ["お問い合わせ内容", "ご質問", "ご相談内容", "メッセージ", "本文", "message", "内容", "詳細"],
    "company": ["会社名", "法人名", "貴社名", "御社名", "company"],
    "subject": ["件名", "題名", "subject"],
    "name": ["お名前", "氏名", "担当者", "ご担当者", "name"],
}
_CONSENT_HINTS = ["プライバシー", "個人情報", "利用規約", "同意"]
_SUCCESS_HINTS = ("ありがとうございます", "送信が完了", "受け付け", "受付ました", "thank you")


def _label_for(page, el):
    """input要素のラベル文言を推定する(label[for]優先、無ければ祖先/直前要素)。"""
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
            const prev = e.previousElementSibling;
            if (prev && prev.tagName === 'LABEL') return prev.innerText;
            return '';
        }""") or ""
    except Exception:  # noqa: BLE001
        return ""


def _classify_field(el, label_text):
    """input/textarea要素の用途を name/id/placeholder/autocomplete/label文言から推定する。"""
    text = " ".join(filter(None, [
        el.get_attribute("name") or "", el.get_attribute("id") or "",
        el.get_attribute("placeholder") or "", el.get_attribute("autocomplete") or "",
        label_text or ""])).lower()
    itype = (el.get_attribute("type") or "").lower()
    tag = (el.evaluate("e => e.tagName") or "").lower()
    if itype == "email" or any(h in text for h in _FIELD_HINTS["email"]):
        return "email"
    if itype == "tel" or any(h in text for h in _FIELD_HINTS["phone"]):
        return "phone"
    if tag == "textarea" or any(h in text for h in _FIELD_HINTS["message"]):
        return "message"
    if any(h in text for h in _FIELD_HINTS["company"]):
        return "company"
    if any(h in text for h in _FIELD_HINTS["subject"]):
        return "subject"
    if any(h in text for h in _FIELD_HINTS["name"]):
        return "name"
    return None


def _click_submit(page):
    """送信ボタンらしき要素を探してクリックする。見つかればTrue。
    入力→確認→送信の2段階フォームでは、確認画面でこれをもう一度呼べば送信まで進む。"""
    for sel in ("button[type=submit]", "input[type=submit]"):
        btn = page.query_selector(sel)
        if btn and btn.is_visible():
            btn.click()
            return True
    for el in page.query_selector_all("button, a"):
        if not el.is_visible():
            continue
        text = (el.inner_text() or "").strip()
        if re.search(r"送信|確認する|この内容で送信|submit", text, re.I):
            el.click()
            return True
    return False


def _submit_form(url, sender, subject, body):
    """Playwrightで問い合わせフォームへ実際に入力・送信する。dry_run=Falseの本番経路のみ。"""
    from playwright.sync_api import sync_playwright

    values = {"company": sender.name, "name": sender.name, "email": sender.email,
              "phone": "", "subject": subject or "", "message": body}
    try:
        with sync_playwright() as p:
            # --disable-http2: 一部サイトのHTTP/2実装とヘッドレスChromiumの相性問題で
            # ERR_HTTP2_PROTOCOL_ERRORになるケースがあるため、HTTP/1.1に固定する
            browser = p.chromium.launch(args=["--disable-http2"])
            try:
                page = browser.new_page()
                page.goto(url, timeout=45000, wait_until="domcontentloaded")

                if page.query_selector(
                        "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], .g-recaptcha"):
                    return SendResult(ok=False, error="CAPTCHA検出のため自動送信不可",
                                       permanent=False)

                fields = page.query_selector_all(
                    "input[type=text], input[type=email], input[type=tel], "
                    "input:not([type]), textarea")
                filled = 0
                for el in fields:
                    if not el.is_visible():
                        continue
                    kind = _classify_field(el, _label_for(page, el))
                    if kind and values.get(kind):
                        el.fill(values[kind])
                        filled += 1
                if filled == 0:
                    return SendResult(ok=False, error="入力欄を検出できず", permanent=False)

                for cb in page.query_selector_all("input[type=checkbox]"):
                    if not cb.is_visible() or cb.is_checked():
                        continue
                    if any(h in (_label_for(page, cb) or "") for h in _CONSENT_HINTS):
                        cb.check()

                if not _click_submit(page):
                    return SendResult(ok=False, error="送信ボタンを検出できず", permanent=False)
                page.wait_for_load_state("networkidle", timeout=15000)

                # 入力→確認→送信の2段階フォーム対応。確認画面が無ければ2回目は何も起きない
                if _click_submit(page):
                    page.wait_for_load_state("networkidle", timeout=15000)

                page_text = page.inner_text("body")
                if any(k in page_text for k in _SUCCESS_HINTS):
                    return SendResult(ok=True, provider_id=f"form_{uuid.uuid4().hex[:10]}",
                                       raw={"final_url": page.url})
                return SendResult(ok=False, error="送信完了の確認ができず(要目視確認)",
                                   permanent=False, raw={"final_url": page.url})
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001
        # ページ読込失敗等は一時的な可能性があるため再試行対象として投げる
        raise R.Retryable(f"フォーム送信中にエラー: {type(e).__name__}: {e}") from e


class FormSender(BaseSender):
    """問い合わせフォームへのPlaywright自動入力・送信。
    宛先ごとにフィールド構成が異なるため成功を保証できない。CAPTCHA・未検出系の
    失敗はpermanent=False(配信停止に入れない)で記録し、他チャネルや人手に委ねる。"""
    channel = "フォーム"
    unit_cost_yen = 0
    rate_service = "form_submit"
    URL_RE = re.compile(r"^https?://", re.I)

    def validate(self, to):
        if not to.contact_url:
            return "問い合わせページURL未取得"
        if not self.URL_RE.match(to.contact_url):
            return f"URL形式不正: {to.contact_url}"
        return None

    def footer(self, sender):
        # フォーム自体に会社名等の専用欄があるため、他チャネルより署名を簡潔にするが、
        # 送信者表示と配信停止手段は他チャネルと同様に必須で載せる
        return f"\n\n{sender.name} / {sender.email}\n今後のご連絡が不要な場合: {sender.optout_url}"

    def _deliver(self, to, sender, subject, body):
        if self.dry_run:
            return SendResult(ok=True, provider_id=f"mock_form_{uuid.uuid4().hex[:10]}")
        return _submit_form(to.contact_url, sender, subject, body)


REGISTRY = {s.channel: s for s in (MailSender, FaxSender, SmsSender, PostSender, FormSender)}


def get_sender(channel, con, dry_run=True):
    cls = REGISTRY.get(channel)
    if not cls:
        raise ValueError(f"未対応チャネル: {channel}")
    return cls(con, dry_run=dry_run)


# ── 一括送信 ────────────────────────────────
def send_campaign(con, campaign_id, step=1, dry_run=True, limit=None):
    """キャンペーンの未送信分を実際に送る。
    campaign.py simulate の本番版がこれ。接触ガードはここでも最終確認する。"""
    import db

    q = """SELECT t.id tid, t.channel, t.subject, t.body, t.company_id, t.step,
                  c.name, c.email, c.fax, c.phone, c.address, c.contact_url,
                  tn.name sname, tn.sender_email, tn.sender_address, tn.optout_url
           FROM touches t
           JOIN companies c ON c.id = t.company_id
           LEFT JOIN campaigns cp ON cp.id = t.campaign_id
           LEFT JOIN offers o ON o.id = 1
           LEFT JOIN tenants tn ON tn.id = o.tenant_id
           WHERE t.campaign_id=? AND t.step=? AND t.sent_at IS NULL
             AND t.body IS NOT NULL AND t.body != ''"""
    p = [campaign_id, step]
    if limit:
        q += " LIMIT ?"; p.append(limit)
    rows = con.execute(q, p).fetchall()

    if not rows:
        print("送信対象がありません（文面未生成、または全て送信済み）")
        return

    print(f"送信対象 {len(rows)}件 / {'DRY RUN（実送信しません）' if dry_run else '★本番送信★'}")
    stats = {"sent": 0, "failed": 0, "blocked": 0, "suppressed": 0}
    cost = 0

    for r in rows:
        # 送信直前の最終ガード（作成後に配信停止された可能性がある）
        allowed, why = db.can_contact(con, r["company_id"])
        if not allowed:
            stats["blocked"] += 1
            con.execute("UPDATE touches SET note=? WHERE id=?", (f"送信中止: {why}", r["tid"]))
            continue

        sender = Sender(
            name=r["sname"] or "AshiBase（足場ベース）",
            email=r["sender_email"] or "info@ashibase.jp",
            address=r["sender_address"] or "",
            optout_url=r["optout_url"] or "https://ashibase.jp/optout")
        to = Recipient(company_id=r["company_id"], name=r["name"], email=r["email"],
                       fax=r["fax"], phone=r["phone"], address=r["address"],
                       contact_url=r["contact_url"])

        adapter = get_sender(r["channel"], con, dry_run=dry_run)
        key = R.Idempotency.key("send", campaign_id, r["company_id"], step)
        res = adapter.send(to, sender, r["subject"], r["body"], key)

        if res.ok:
            con.execute("""UPDATE touches SET sent_at=?, delivered=1, note=?, unit_cost_yen=?
                           WHERE id=?""",
                        (datetime.now().isoformat(timespec="seconds"),
                         f"provider_id={res.provider_id}", res.cost_yen, r["tid"]))
            stats["sent"] += 1
            cost += res.cost_yen
        else:
            con.execute("UPDATE touches SET delivered=0, note=? WHERE id=?",
                        (f"送信失敗: {res.error}", r["tid"]))
            stats["failed"] += 1
            # 恒久エラーは配信停止に入れる（宛先不明への再送は無意味かつ有害）
            if res.permanent and "未取得" not in (res.error or ""):
                db.suppress(con, r["company_id"], "bounce_hard", source=adapter.channel,
                            note=res.error)
                stats["suppressed"] += 1
        con.commit()

    con.execute("UPDATE campaigns SET cost_yen = COALESCE(cost_yen,0) + ? WHERE id=?",
                (cost, campaign_id))
    con.commit()
    print(f"  送信 {stats['sent']} / 失敗 {stats['failed']} / "
          f"ガードで中止 {stats['blocked']} / 恒久エラーで配信停止 {stats['suppressed']}")
    print(f"  実費 {cost:,}円")
    return stats


if __name__ == "__main__":
    import sys
    import db

    con = db.connect(); db.migrate(con)

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("── 宛先検証 ──")
        cases = [
            (MailSender, Recipient(1, "テスト", email="a@b.co.jp"), None),
            (MailSender, Recipient(1, "テスト", email="こわれた"), "形式不正"),
            (MailSender, Recipient(1, "テスト"), "未取得"),
            (FaxSender, Recipient(1, "テスト", fax="03-1234-5678"), None),
            (FaxSender, Recipient(1, "テスト", fax="123"), "形式不正"),
            (SmsSender, Recipient(1, "テスト", phone="090-1234-5678"), None),
            (SmsSender, Recipient(1, "テスト", phone="03-1234-5678"), "携帯番号ではない"),
            (PostSender, Recipient(1, "テスト", address="東京都千代田区1-1-1"), None),
            (FormSender, Recipient(1, "テスト", contact_url="https://example.co.jp/contact/"), None),
            (FormSender, Recipient(1, "テスト", contact_url="example.co.jp/contact"), "URL形式不正"),
            (FormSender, Recipient(1, "テスト"), "未取得"),
        ]
        for cls, rcp, expect in cases:
            why = cls(con).validate(rcp)
            ok = (why is None) if expect is None else (expect in (why or ""))
            print(f"  {'✓' if ok else '✗'} {cls.channel:<6} {why or '送信可'}")

        print("\n── 送信者情報の自動付与 ──")
        s = Sender("AshiBase（足場ベース）", "info@ashibase.jp", "東京都...", "https://ashibase.jp/optout")
        for cls in (MailSender, FaxSender, SmsSender, FormSender):
            f = cls(con).footer(s)
            has = all(k in f for k in ("AshiBase",)) and ("optout" in f or "ご返信" in f)
            print(f"  {'✓' if has else '✗'} {cls.channel:<6} 送信者表示と停止手段あり")

        print("\n── 二重送信の防止 ──")
        con.execute("DELETE FROM idempotency WHERE key LIKE 'test:%'"); con.commit()
        m = MailSender(con, dry_run=True)
        rcp = Recipient(1, "テスト", email="a@b.co.jp")
        r1 = m.send(rcp, s, "件名", "本文", "test:1")
        r2 = m.send(rcp, s, "件名", "本文", "test:1")
        print(f"  1回目: {r1.provider_id}")
        print(f"  2回目: {r2.provider_id}  {'✓ 送信されない' if r2.provider_id=='skipped' else '✗ 二重送信'}")
        con.execute("DELETE FROM idempotency WHERE key LIKE 'test:%'"); con.commit()

        print("\n── フォーム自動送信(dry run) ──")
        fm = FormSender(con, dry_run=True)
        frcp = Recipient(1, "テスト", contact_url="https://example.co.jp/contact/")
        fres = fm.send(frcp, s, "件名", "本文", "test:form:1")
        print(f"  {'✓' if fres.ok else '✗'} dry runで成功応答 (provider_id={fres.provider_id})")

        print("\n── フォーム自動送信: フィールド検出ヒューリスティック ──")
        try:
            from playwright.sync_api import sync_playwright
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
                    </form>"""),
                ("placeholder頼みのフォーム", """
                    <form>
                      <input name="field1" placeholder="貴社名をご記入ください">
                      <input name="field2" placeholder="メールアドレス">
                      <textarea name="field3" placeholder="ご相談内容をご記入ください"></textarea>
                      <input type="submit" value="確認する">
                    </form>"""),
            ]
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                for label, html in samples:
                    page.set_content(html)
                    kinds = []
                    for el in page.query_selector_all("input, textarea"):
                        if (el.get_attribute("type") or "") == "checkbox":
                            continue
                        kinds.append(_classify_field(el, _label_for(page, el)))
                    has_message = "message" in kinds
                    has_email = "email" in kinds
                    print(f"  {'✓' if has_message and has_email else '✗'} {label}: "
                          f"検出結果={kinds}")
                browser.close()
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ Playwright未使用のためスキップ ({type(e).__name__}: {e})")
    else:
        cid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
        step = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        send_campaign(con, cid, step, dry_run=True)
