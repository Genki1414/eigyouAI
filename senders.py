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
from datetime import datetime, timedelta
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
# 実際のブラウザ操作(探索・検出・入力・送信・判定)はform_navigator.pyが専任で担当する。
# ここ(FormSender)は対象決定・接触ガード・履歴管理という送信アダプタ本来の責務のみを持ち、
# Playwrightの詳細には立ち入らない。
_SKIP_STATUSES = {"SKIP_CAPTCHA", "SKIP_NO_SOLICIT", "SKIP_RECRUIT_ONLY", "SKIP_SUPPORT_ONLY",
                   "SKIP_BOT_CHALLENGE"}


def _log_form_send(con, company_id, result, target_url, tenant_id=None, offer_id=None,
                    started_at=None, keep_debug_fields=False):
    """form_send_logへ1試行分を記録する。個人情報配慮のため入力内容そのものは残さない。
    keep_debug_fields=Trueの間だけfinal_url/page_title/page_text_snippetを保存する
    (検証中のみ想定。page_text_snippetは成功判定できなかった原因調査用)。"""
    con.execute("""INSERT INTO form_send_log
        (company_id, tenant_id, offer_id, target_url, contact_url, started_at, finished_at,
         status, reason_code, detected_fields, filled_fields, submit_attempted,
         success_evidence, error_message, retryable, playwright_run_id, final_url, page_title,
         page_text_snippet)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (company_id, tenant_id, offer_id, target_url, result.contact_url_used,
         started_at or datetime.now().isoformat(timespec="seconds"),
         datetime.now().isoformat(timespec="seconds"),
         result.status, result.reason_code,
         json.dumps(result.detected_fields, ensure_ascii=False),
         json.dumps(result.filled_fields, ensure_ascii=False),
         1 if result.submit_attempted else 0, result.success_evidence, result.error_message,
         1 if result.status == "FAILED_RETRYABLE" else 0, result.run_id,
         result.final_url if keep_debug_fields else None,
         result.page_title if keep_debug_fields else None,
         result.page_text_snippet if keep_debug_fields else None))
    con.commit()


class FormSender(BaseSender):
    """問い合わせフォームへの自動入力・送信。ブラウザ操作はform_navigator.pyに委譲する。
    宛先ごとにフィールド構成が異なるため成功を保証できない。SKIP系(CAPTCHA・営業禁止等)は
    「会社がダメ」ではなく「このチャネルがダメ」なので配信停止には入れない(permanent=False)。"""
    channel = "フォーム"
    unit_cost_yen = 0
    rate_service = "form_submit"
    URL_RE = re.compile(r"^https?://", re.I)

    def __init__(self, con, dry_run=True, tenant_id=None, offer_id=None, keep_debug_fields=False):
        super().__init__(con, dry_run=dry_run)
        self.tenant_id = tenant_id
        self.offer_id = offer_id
        self.keep_debug_fields = keep_debug_fields
        self._run_count = 0  # このインスタンス(=1回の実行)での試行数

    def _check_quota(self):
        """cron/API呼び出し1回あたり・直近1時間・直近24時間・テナット別の上限を見る。
        件数は成否を問わず試行数でカウントする(失敗でも相手サイトへの負荷は発生するため)。
        上限に達したら(False, 理由)を返し、Playwrightは一切起動しない。"""
        import config as C

        self._run_count += 1
        if self._run_count > C.FORM_MAX_PER_RUN:
            return False, f"1回の実行あたりの上限({C.FORM_MAX_PER_RUN}件)に到達"

        now = datetime.now()
        hour_ago = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        day_ago = (now - timedelta(hours=24)).isoformat(timespec="seconds")

        n_hour = self.con.execute(
            "SELECT COUNT(*) FROM form_send_log WHERE started_at >= ?", (hour_ago,)).fetchone()[0]
        if n_hour >= C.FORM_MAX_PER_HOUR:
            return False, f"直近1時間の上限({C.FORM_MAX_PER_HOUR}件)に到達"

        n_day = self.con.execute(
            "SELECT COUNT(*) FROM form_send_log WHERE started_at >= ?", (day_ago,)).fetchone()[0]
        if n_day >= C.FORM_MAX_PER_DAY:
            return False, f"直近24時間の上限({C.FORM_MAX_PER_DAY}件)に到達"

        if self.tenant_id is not None:
            n_tenant = self.con.execute(
                "SELECT COUNT(*) FROM form_send_log WHERE started_at >= ? AND tenant_id=?",
                (day_ago, self.tenant_id)).fetchone()[0]
            if n_tenant >= C.FORM_MAX_PER_TENANT_PER_DAY:
                return False, f"テナント別・直近24時間の上限({C.FORM_MAX_PER_TENANT_PER_DAY}件)に到達"

        return True, None

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

        quota_ok, quota_reason = self._check_quota()
        if not quota_ok:
            # 上限に達した場合はPlaywrightを一切起動しない(相手サイトへのアクセス自体が
            # 発生しないので form_send_log には残さない)。permanent=Falseなので次回以降は
            # 通常通り試行できる
            return SendResult(ok=False, error=f"送信ペーシング上限: {quota_reason}", permanent=False,
                               raw={"status": "SKIP_QUOTA_EXCEEDED", "reason_code": quota_reason})

        import form_navigator as FN
        started_at = datetime.now().isoformat(timespec="seconds")
        values = {"company": sender.name, "name": sender.name, "email": sender.email,
                  "phone": "", "subject": subject or "", "message": body,
                  "furigana": "アシベース"}
        result = FN.navigate_and_submit(to.contact_url, values)
        _log_form_send(self.con, to.company_id, result, to.contact_url,
                        tenant_id=self.tenant_id, offer_id=self.offer_id,
                        started_at=started_at, keep_debug_fields=self.keep_debug_fields)

        if result.status == "SUCCESS":
            return SendResult(ok=True, provider_id=f"form_{result.run_id}",
                               raw={"final_url": result.final_url, "reason_code": result.reason_code})
        if result.status == "FAILED_RETRYABLE":
            # R.retry()に任せて呼び出し側の再試行ループに乗せる
            raise R.Retryable(result.error_message or result.reason_code or "一時的な失敗")
        # SKIP_* / FAILED_UNSUPPORTED はここで確定させる。いずれもpermanent=Falseとし、
        # 「会社を止める」のではなく「このチャネルでは通らなかった」事実だけ記録する。
        label = "対象外(SKIP)" if result.status in _SKIP_STATUSES else "未対応の構造"
        return SendResult(ok=False, error=f"{label}: {result.reason_code}", permanent=False,
                           raw={"status": result.status, "reason_code": result.reason_code})


REGISTRY = {s.channel: s for s in (MailSender, FaxSender, SmsSender, PostSender, FormSender)}


def get_sender(channel, con, dry_run=True, tenant_id=None, offer_id=None):
    cls = REGISTRY.get(channel)
    if not cls:
        raise ValueError(f"未対応チャネル: {channel}")
    if cls is FormSender:
        return cls(con, dry_run=dry_run, tenant_id=tenant_id, offer_id=offer_id)
    return cls(con, dry_run=dry_run)


# ── 一括送信 ────────────────────────────────
def send_campaign(con, campaign_id, step=1, dry_run=True, limit=None):
    """キャンペーンの未送信分を実際に送る。
    campaign.py simulate の本番版がこれ。接触ガードはここでも最終確認する。"""
    import db

    q = """SELECT t.id tid, t.channel, t.subject, t.body, t.company_id, t.step,
                  c.name, c.email, c.fax, c.phone, c.address, c.contact_url,
                  o.id offer_id, tn.id tenant_id,
                  tn.name sname, tn.sender_email, tn.sender_address, tn.optout_url
           FROM touches t
           JOIN companies c ON c.id = t.company_id
           LEFT JOIN campaigns cp ON cp.id = t.campaign_id
           LEFT JOIN offers o ON o.id = COALESCE(cp.offer_id, 1)
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

        adapter = get_sender(r["channel"], con, dry_run=dry_run,
                              tenant_id=r["tenant_id"], offer_id=r["offer_id"])
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
        print("  (フィールド検出ヒューリスティックの検証は python3 form_navigator.py test を参照)")

        print("\n── フォーム送信ペーシング上限 ──")
        import config as C
        orig_max_per_run = C.FORM_MAX_PER_RUN
        C.FORM_MAX_PER_RUN = 2
        try:
            fq = FormSender(con, dry_run=False)  # _check_quota()自体はPlaywrightを起動しない
            r1 = fq._check_quota()
            r2 = fq._check_quota()
            r3 = fq._check_quota()
            ok = r1[0] and r2[0] and not r3[0]
            print(f"  {'✓' if ok else '✗'} 1回目:{r1[0]} 2回目:{r2[0]} "
                  f"3回目:{r3[0]}(上限{C.FORM_MAX_PER_RUN}のため False が正しい)")
        finally:
            C.FORM_MAX_PER_RUN = orig_max_per_run
    else:
        cid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
        step = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        send_campaign(con, cid, step, dry_run=True)
