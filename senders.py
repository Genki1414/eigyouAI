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
import time as _time
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
    """テナントの送信者情報。特定電子メール法の表示義務を満たすために必須。
    last_name〜phoneは任意(姓・名・フリガナ・郵便番号・住所(都道府県/市区町村/
    丁目番地/建物名)・電話番号が別欄の問い合わせフォーム向け。MIKOMERU同等)。
    未設定ならFormSender側で妥当な既定値へフォールバックする(姓欄には会社名、
    名欄・フリガナ欄は空、住所系はaddressへ丸ごとフォールバック、というように)。"""
    name: str
    email: str
    address: str
    optout_url: str
    last_name: str = ""
    first_name: str = ""
    last_name_kana: str = ""
    first_name_kana: str = ""
    postal_code: str = ""
    prefecture: str = ""
    city: str = ""
    block: str = ""
    building: str = ""
    phone: str = ""


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
        # 1. 宛先の検証(無効な宛先はそもそも「試行」として占有しない)
        why = self.validate(to)
        if why:
            return SendResult(ok=False, error=why, permanent=True)

        # 2. 二重送信の防止。SELECTしてから後でINSERTする2段階のチェックだと、
        #    同じキーへの2つの同時リクエスト(例: list_builder.htmlのボタン連打、
        #    2人の担当者が同じリストを同時に送信)が両方とも「未送信」と判定してしまい、
        #    同じ会社へ二重に実送信してしまう恐れがある(SUCCESS確定前後の競合)。
        #    idempotency.keyはPRIMARY KEYなので、INSERT OR IGNOREの成否だけで
        #    原子的に排他制御できる(SQLiteが書き込みをシリアライズするため安全)。
        claimed = self.con.execute(
            "INSERT OR IGNORE INTO idempotency (key, created_at) VALUES (?,?)",
            (idem_key, datetime.now().isoformat(timespec="seconds"))).rowcount
        self.con.commit()
        if not claimed:
            return SendResult(ok=True, provider_id="skipped", error="送信済み（冪等キー一致）")

        # 3. レート制御
        self.rl.acquire()

        # 4. 送信（一時エラーのみ再試行）
        full_body = body + self.footer(sender)
        try:
            res = R.retry(lambda: self._deliver(to, sender, subject, full_body),
                          attempts=4, job=f"{self.channel}:{to.name}")
        except Exception as e:  # noqa: BLE001
            # 占有を解放する(失敗は再試行できないと詰むため。占有したまま
            # 失敗で終わると、以後ずっと「送信済み」扱いになってしまう)
            self.con.execute("DELETE FROM idempotency WHERE key=?", (idem_key,))
            self.con.commit()
            return SendResult(ok=False, error=str(e)[:200],
                              permanent=isinstance(e, R.Fatal))

        # 5. 成功したら占有を確定記録として残す。失敗は占有を解放して再試行を許す
        if res.ok:
            self.con.execute("UPDATE idempotency SET result=? WHERE key=?",
                             (json.dumps({"provider_id": res.provider_id,
                                          "at": datetime.now().isoformat(timespec="seconds")},
                                         ensure_ascii=False), idem_key))
            self.con.commit()
            res.cost_yen = self.unit_cost_yen
        else:
            self.con.execute("DELETE FROM idempotency WHERE key=?", (idem_key,))
            self.con.commit()
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
                    list_id=None, started_at=None, keep_debug_fields=False,
                    retry_count=0, execution_seconds=None):
    """form_send_logへ1試行分を記録する。個人情報配慮のため入力内容そのものは残さない。
    keep_debug_fields=Trueの間だけfinal_url/page_title/page_text_snippetを保存する
    (検証中のみ想定。page_text_snippetは成功判定できなかった原因調査用)。

    retry_count・execution_secondsは1送信あたりの原価把握のための最小限の計測。
    厳密な原価計算ではなく事業判断に使える概算値を残すのが目的(config.py参照)。
    AI課金はこの経路では発生しない(フォーム自動送信はAIを使わない)ため0のまま。"""
    import config as C
    server_cost = C.estimate_server_cost_yen(execution_seconds)
    con.execute("""INSERT INTO form_send_log
        (company_id, tenant_id, offer_id, list_id, target_url, contact_url, started_at, finished_at,
         status, reason_code, detected_fields, filled_fields, submit_attempted,
         success_evidence, error_message, retryable, playwright_run_id, final_url, page_title,
         page_text_snippet, retry_count, execution_seconds, ai_tokens_input, ai_tokens_output,
         ai_cost_yen, external_api_cost_yen, estimated_server_cost_yen, total_estimated_cost_yen,
         screenshot_before_path, screenshot_after_path)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (company_id, tenant_id, offer_id, list_id, target_url, result.contact_url_used,
         started_at or datetime.now().isoformat(timespec="seconds"),
         datetime.now().isoformat(timespec="seconds"),
         result.status, result.reason_code,
         json.dumps(result.detected_fields, ensure_ascii=False),
         json.dumps(result.filled_fields, ensure_ascii=False),
         1 if result.submit_attempted else 0, result.success_evidence, result.error_message,
         1 if result.status == "FAILED_RETRYABLE" else 0, result.run_id,
         result.final_url if keep_debug_fields else None,
         result.page_title if keep_debug_fields else None,
         result.page_text_snippet if keep_debug_fields else None,
         retry_count, execution_seconds, 0, 0, 0.0, 0.0, server_cost, server_cost,
         result.screenshot_before_path, result.screenshot_after_path))
    con.commit()


class FormSender(BaseSender):
    """問い合わせフォームへの自動入力・送信。ブラウザ操作はform_navigator.pyに委譲する。
    宛先ごとにフィールド構成が異なるため成功を保証できない。SKIP系(CAPTCHA・営業禁止等)は
    「会社がダメ」ではなく「このチャネルがダメ」なので配信停止には入れない(permanent=False)。"""
    channel = "フォーム"
    unit_cost_yen = 0
    rate_service = "form_submit"
    URL_RE = re.compile(r"^https?://", re.I)

    def __init__(self, con, dry_run=True, tenant_id=None, offer_id=None, list_id=None,
                 keep_debug_fields=False):
        super().__init__(con, dry_run=dry_run)
        self.tenant_id = tenant_id
        self.offer_id = offer_id
        self.list_id = list_id
        self.keep_debug_fields = keep_debug_fields
        self._run_count = 0  # このインスタンス(=1回の実行)での試行数
        self._attempt_count = 0  # このインスタンス(=1社への1回のsend())での試行回数。
                                  # R.retry()が_deliver()を再試行するたびに増える。
                                  # retry_countとしてform_send_logに残す(原価計測用)

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

        self._attempt_count += 1
        quota_ok, quota_reason = self._check_quota()
        if not quota_ok:
            # 上限に達した場合はPlaywrightを一切起動しない(相手サイトへのアクセス自体が
            # 発生しないので form_send_log には残さない)。permanent=Falseなので次回以降は
            # 通常通り試行できる
            return SendResult(ok=False, error=f"送信ペーシング上限: {quota_reason}", permanent=False,
                               raw={"status": "SKIP_QUOTA_EXCEEDED", "reason_code": quota_reason})

        import form_navigator as FN
        import config as C
        started_at = datetime.now().isoformat(timespec="seconds")
        t0 = _time.monotonic()
        # 姓・名・フリガナ・郵便番号は任意設定。未設定の場合、姓欄には会社名を
        # 入れておく(空欄より許容されやすい)が、名欄・フリガナ欄は空のままにする
        # (以前は名欄にも会社名を複製し、フリガナ欄には固定文字列"アシベース"を
        # 入れていたが、カスタムの送信者名を設定したテナントでは明らかに不自然な
        # 内容になってしまうため、確実な値が無い項目は空欄のままにする方針にした)
        furigana = f"{sender.last_name_kana}{sender.first_name_kana}".strip()
        # 住所は都道府県/市区町村/丁目番地/建物名が個別設定されていればそれを使い、
        # 単一の住所欄しかないフォーム向けにはそれらを連結した文字列をaddressへも入れる
        # (未設定ならsender.address(従来の単一自由記述欄)にフォールバックする)。
        structured_address = "".join(filter(None, [
            sender.prefecture, sender.city, sender.block, sender.building]))
        values = {"company": sender.name, "name": sender.name, "email": sender.email,
                  "phone": sender.phone or "",
                  "address": structured_address or (sender.address or ""),
                  "postal_code": sender.postal_code or "",
                  "prefecture": sender.prefecture or "", "city": sender.city or "",
                  "block": sender.block or "", "building": sender.building or "",
                  "last_name": sender.last_name or sender.name, "first_name": sender.first_name or "",
                  "subject": subject or "", "message": body, "furigana": furigana}
        result = FN.navigate_and_submit(to.contact_url, values,
                                         screenshot_dir=C.OUT_DIR / "form_screenshots")
        execution_seconds = _time.monotonic() - t0
        _log_form_send(self.con, to.company_id, result, to.contact_url,
                        tenant_id=self.tenant_id, offer_id=self.offer_id, list_id=self.list_id,
                        started_at=started_at, keep_debug_fields=self.keep_debug_fields,
                        retry_count=self._attempt_count - 1, execution_seconds=execution_seconds)

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


def get_sender(channel, con, dry_run=True, tenant_id=None, offer_id=None, list_id=None):
    cls = REGISTRY.get(channel)
    if not cls:
        raise ValueError(f"未対応チャネル: {channel}")
    if cls is FormSender:
        return cls(con, dry_run=dry_run, tenant_id=tenant_id, offer_id=offer_id, list_id=list_id)
    return cls(con, dry_run=dry_run)


MERGE_TAG_HELP = "##TO_COMPANY_NAME##(送信先の会社名) / ##FROM_FAMILY_NAME##(送信元の姓)"


def render_merge_tags(text, to, sender):
    """MIKOMERU同等のマージタグを置換する(送信文章に埋め込むと会社ごとに自動で
    差し込まれる)。未対応のタグはそのまま残す(誤字で本文が壊れて見えるほうが、
    黙って消えるより気づきやすい)。"""
    if not text:
        return text
    return (text.replace("##TO_COMPANY_NAME##", to.name or "")
                .replace("##FROM_FAMILY_NAME##", sender.last_name or sender.name or ""))


# 本文中のURLを検出する。日本語文章はURLの直後にスペースを挟まず句読点や
# 閉じ括弧が続くことが多いため、それらは掴まないようにストップ文字へ含める
# (完全ではないが、実運用上のほとんどのケースをカバーする)。
_URL_RE = re.compile(r'https?://[^\s<>"　。、）】』」]+')


def rewrite_tracked_links(con, touch_id, body, base_url):
    """MIKOMERUの「URLアクセスの記録」相当。本文中のURLをクリック計測用の
    リダイレクトリンクに置き換える(同じURLが複数回出てきてもトークンは1個だけ
    発行し、全出現箇所を置き換える)。呼び出し側でdry_run時は呼ばないこと
    (実際に届かない文面のためにトークンを発行し続けるとDBが無駄に増える)。"""
    import db
    if not body:
        return body
    for url in set(_URL_RE.findall(body)):
        token = db.create_click_token(con, touch_id, url)
        tracked = f"{base_url.rstrip('/')}/track/click/{token}"
        body = body.replace(url, tracked)
    return body


# ── 一括送信 ────────────────────────────────
def send_campaign(con, campaign_id, step=1, dry_run=True, limit=None, track_clicks=False):
    """キャンペーンの未送信分を実際に送る。
    campaign.py simulate の本番版がこれ。接触ガードはここでも最終確認する。
    track_clicks=Trueなら、本文中のURLをクリック計測リンクへ置き換える
    (MIKOMERUの「URLアクセスの記録」相当。dry_run時は置き換えない=トークンを
    無駄に発行しない)。この設定はcampaigns/touchesへは保存しない——両者は
    同じリストへの再送で使い回されるため、そこに保存すると別の送信操作の
    設定が漏れて残ってしまう(呼び出しごとに都度指定する設計)。"""
    import db

    q = """SELECT t.id tid, t.channel, t.subject, t.body, t.company_id, t.step,
                  c.name, c.email, c.fax, c.phone, c.address, c.contact_url,
                  o.id offer_id, tn.id tenant_id, tl.id list_id,
                  tn.sender_name sname, tn.sender_email, tn.sender_address, tn.optout_url,
                  tn.sender_last_name, tn.sender_first_name, tn.sender_last_name_kana,
                  tn.sender_first_name_kana, tn.sender_postal_code, tn.sender_prefecture,
                  tn.sender_city, tn.sender_block, tn.sender_building, tn.sender_phone
           FROM touches t
           JOIN companies c ON c.id = t.company_id
           LEFT JOIN campaigns cp ON cp.id = t.campaign_id
           LEFT JOIN offers o ON o.id = COALESCE(cp.offer_id, 1)
           LEFT JOIN tenants tn ON tn.id = o.tenant_id
           LEFT JOIN target_lists tl ON tl.campaign_id = t.campaign_id
           WHERE t.campaign_id=? AND t.step=?
             AND (t.sent_at IS NULL OR instr(t.note, 'provider_id=mock_') > 0)
             AND t.body IS NOT NULL AND t.body != ''"""
    p = [campaign_id, step]
    if limit:
        q += " LIMIT ?"; p.append(limit)
    rows = con.execute(q, p).fetchall()

    stats = {"sent": 0, "failed": 0, "blocked": 0, "suppressed": 0, "stopped": 0}
    if not rows:
        print("送信対象がありません（文面未生成、または全て送信済み）")
        return stats

    print(f"送信対象 {len(rows)}件 / {'DRY RUN（実送信しません）' if dry_run else '★本番送信★'}")
    cost = 0

    for r in rows:
        # 送信直前の最終ガード（作成後に配信停止された可能性がある。テナント別の
        # 送信除外設定(tenant_exclusions)もここで一緒に確認する）
        allowed, why = db.can_contact(con, r["company_id"], tenant_id=r["tenant_id"])
        if not allowed:
            stats["blocked"] += 1
            con.execute("UPDATE touches SET note=? WHERE id=?", (f"送信中止: {why}", r["tid"]))
            continue

        # Kill Switch: 異常検知時に管理者が停止していれば実送信はここで止める
        # (dry_runは実サイトへ触れないので対象外。kill_switch_cli.py参照)。
        # ここが全送信経路(手動送信/cron/Stock Factory運用API)の唯一の合流点。
        if not dry_run:
            stopped, stop_reason = db.kill_switch_status(con, tenant_id=r["tenant_id"])
            if stopped:
                stats["stopped"] += 1
                con.execute("UPDATE touches SET note=? WHERE id=?",
                            (f"送信中止: Kill Switch — {stop_reason}", r["tid"]))
                continue

        sender = Sender(
            name=r["sname"] or "AshiBase（足場ベース）",
            email=r["sender_email"] or "info@ashibase.jp",
            address=r["sender_address"] or "",
            optout_url=r["optout_url"] or "https://ashibase.jp/optout",
            last_name=r["sender_last_name"] or "", first_name=r["sender_first_name"] or "",
            last_name_kana=r["sender_last_name_kana"] or "",
            first_name_kana=r["sender_first_name_kana"] or "",
            postal_code=r["sender_postal_code"] or "",
            prefecture=r["sender_prefecture"] or "", city=r["sender_city"] or "",
            block=r["sender_block"] or "", building=r["sender_building"] or "",
            phone=r["sender_phone"] or "")
        to = Recipient(company_id=r["company_id"], name=r["name"], email=r["email"],
                       fax=r["fax"], phone=r["phone"], address=r["address"],
                       contact_url=r["contact_url"])

        adapter = get_sender(r["channel"], con, dry_run=dry_run,
                              tenant_id=r["tenant_id"], offer_id=r["offer_id"], list_id=r["list_id"])
        # ドライランと本番送信は別の冪等キー空間を使う。同じキーだとドライランが
        # 冪等キーを占有してしまい、その後の本番送信が「送信済み(冪等キー一致)」
        # として何もせず素通りしてしまう(実サイトに一度も送らないまま「完了」扱いになる)。
        key = R.Idempotency.key("send" if not dry_run else "send:dryrun",
                                 campaign_id, r["company_id"], step)
        subject = render_merge_tags(r["subject"], to, sender)
        body = render_merge_tags(r["body"], to, sender)
        if track_clicks and not dry_run:
            import config as C
            body = rewrite_tracked_links(con, r["tid"], body, C.TRACK_BASE_URL)
        res = adapter.send(to, sender, subject, body, key)

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
          f"ガードで中止 {stats['blocked']} / 恒久エラーで配信停止 {stats['suppressed']} / "
          f"Kill Switchで中止 {stats['stopped']}")
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

        print("\n── 二重送信の防止(同時リクエストでの競合) ──")
        # ボタン連打や2人の担当者による同時送信を想定し、同じ冪等キーへ複数スレッドから
        # 本物のHTTPリクエストのように「別コネクション」で同時にsend()を呼ぶ。
        # _deliver()にわずかな遅延を入れて競合が起きやすい状況を作る(dry_run=trueなので
        # 実サイトへは触れない)。修正前は複数スレッドが揃って「未送信」と判定してしまい
        # 実送信扱いが複数件になり得た(SELECTしてから後でINSERTする2段階チェックのため)
        import threading
        import time as _time

        class _SlowMailSender(MailSender):
            def _deliver(self, to, sender, subject, body):
                _time.sleep(0.05)
                return super()._deliver(to, sender, subject, body)

        con.execute("DELETE FROM idempotency WHERE key LIKE 'test:race:%'"); con.commit()
        race_results = []

        def _race_worker():
            con_t = db.connect()  # 別スレッド=別HTTPリクエストを模すため別コネクション
            sm = _SlowMailSender(con_t, dry_run=True)
            rcp2 = Recipient(1, "テスト", email="race@test.co.jp")
            race_results.append(sm.send(rcp2, s, "件名", "本文", "test:race:1"))
            con_t.close()

        threads = [threading.Thread(target=_race_worker) for _ in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        sent_count = sum(1 for r in race_results if r.provider_id != "skipped")
        print(f"  5スレッド同時送信 → 実送信扱い{sent_count}件 / スキップ{5 - sent_count}件")
        print(f"  {'✓' if sent_count == 1 else '✗'} 5スレッド同時でも実際に送るのは1回だけ")
        con.execute("DELETE FROM idempotency WHERE key LIKE 'test:race:%'"); con.commit()

        print("\n── 原価計測(form_send_logへの記録) ──")
        # 実際にPlaywrightを起動せず、_log_form_send()だけを直接呼んで
        # 原価計測(execution_seconds→estimated_server_cost_yen、retry_countの
        # 記録)が正しく効くことを確認する。
        import form_navigator as FN
        con.execute("DELETE FROM form_send_log WHERE company_id=999999"); con.commit()
        fake_result = FN.NavigationResult(status="SUCCESS", reason_code="success_text_matched",
                                          contact_url_used="https://example.co.jp/contact/",
                                          submit_attempted=True, success_evidence="ok")
        _log_form_send(con, 999999, fake_result, "https://example.co.jp/contact/",
                       tenant_id=1, list_id=None, retry_count=2, execution_seconds=30.0)
        logged = con.execute("""SELECT retry_count, execution_seconds, estimated_server_cost_yen,
            total_estimated_cost_yen FROM form_send_log WHERE company_id=999999
            ORDER BY id DESC LIMIT 1""").fetchone()
        print(f"  retry_count={logged['retry_count']} execution_seconds={logged['execution_seconds']} "
              f"estimated_server_cost_yen={logged['estimated_server_cost_yen']:.4f}円")
        ok_cost = (logged["retry_count"] == 2 and logged["execution_seconds"] == 30.0
                   and logged["estimated_server_cost_yen"] > 0
                   and logged["total_estimated_cost_yen"] == logged["estimated_server_cost_yen"])
        print(f"  {'✓' if ok_cost else '✗'} retry_count・execution_seconds・推定原価が記録される")
        con.execute("DELETE FROM form_send_log WHERE company_id=999999"); con.commit()

        print("\n── フォーム自動送信(dry run) ──")
        fm = FormSender(con, dry_run=True)
        frcp = Recipient(1, "テスト", contact_url="https://example.co.jp/contact/")
        fres = fm.send(frcp, s, "件名", "本文", "test:form:1")
        print(f"  {'✓' if fres.ok else '✗'} dry runで成功応答 (provider_id={fres.provider_id})")
        print("  (フィールド検出ヒューリスティックの検証は python3 form_navigator.py test を参照)")

        print("\n── ドライラン後の本番送信(冪等キー分離) ──")
        # 過去のバグ: ドライランと本番送信が同じsent_at判定・同じ冪等キーを
        # 共有していたため、ドライランで一度「成功」させたリストを後で本番送信すると
        # 「送信対象がありません」で黙って何も送らなかった(実サイトへ一度も
        # 送っていないのに完了扱いになる、という一番怖いパターン)。
        con.execute("DELETE FROM companies WHERE id=999998")
        con.execute("""INSERT INTO companies (id, name, contact_url)
            VALUES (999998, 'テスト_ドライラン後本番', 'https://example.co.jp/contact/')""")
        cur = con.execute("INSERT INTO campaigns (name, started_at, target_rule) VALUES (?,?,?)",
                          ("test-dryrun-then-real", datetime.now().isoformat(timespec="seconds"), "ALL"))
        dr_cid = cur.lastrowid
        con.execute("""INSERT INTO touches (campaign_id, company_id, channel, variant, step,
            subject, body) VALUES (?,999998,'フォーム','A',1,'件名','本文')""", (dr_cid,))
        con.execute("DELETE FROM idempotency WHERE key LIKE '%:999998:1'")
        con.commit()

        orig_ks, orig_ks_reason = db.kill_switch_status(con)
        db.set_global_kill_switch(con, False, updated_by="test")
        try:
            stats1 = send_campaign(con, dr_cid, step=1, dry_run=True)
            row1 = con.execute("SELECT sent_at, note FROM touches WHERE campaign_id=? AND company_id=999998",
                               (dr_cid,)).fetchone()
            ok_dr1 = (stats1 is not None and stats1["sent"] == 1
                      and row1["sent_at"] is not None and "mock" in (row1["note"] or ""))
            print(f"  1回目(ドライラン): sent={stats1['sent'] if stats1 else None} note={row1['note']}")
            print(f"  {'✓' if ok_dr1 else '✗'} ドライランはmock成功としてsent_at/noteが立つ")

            import form_navigator as FN
            orig_navigate = FN.navigate_and_submit
            FN.navigate_and_submit = lambda *a, **k: FN.NavigationResult(
                status="SUCCESS", reason_code="success_text_matched", submit_attempted=True)
            try:
                stats2 = send_campaign(con, dr_cid, step=1, dry_run=False)
            finally:
                FN.navigate_and_submit = orig_navigate
            row2 = con.execute("SELECT sent_at, note FROM touches WHERE campaign_id=? AND company_id=999998",
                               (dr_cid,)).fetchone()
            ok_dr2 = (stats2 is not None and stats2["sent"] == 1
                      and "mock" not in (row2["note"] or "") and "provider_id=form_" in (row2["note"] or ""))
            print(f"  2回目(本番): sent={stats2['sent'] if stats2 else None} note={row2['note']}")
            print(f"  {'✓' if ok_dr2 else '✗'} ドライラン後でも本番送信が実際に実行される(黙ってスキップされない)")
        finally:
            db.set_global_kill_switch(con, orig_ks, reason=orig_ks_reason, updated_by="test-restore")
            con.execute("DELETE FROM touches WHERE campaign_id=?", (dr_cid,))
            con.execute("DELETE FROM campaigns WHERE id=?", (dr_cid,))
            con.execute("DELETE FROM companies WHERE id=999998")
            con.execute("DELETE FROM idempotency WHERE key LIKE '%:999998:1'")
            # form_send_logも消しておく(直後の「フォーム送信ペーシング上限」テストは
            # 直近1時間・24時間の件数を実際にDBから数えるため、消さずに残すと
            # 繰り返しテスト実行した時に上限へ引っかかって誤って失敗する)
            con.execute("DELETE FROM form_send_log WHERE company_id=999998")
            con.commit()

        print("\n── 送信元の姓・名・フリガナ・郵便番号(未設定/設定済みの両方) ──")
        # 過去のバグ: 姓欄・名欄の両方に会社名(sender.name)をそのまま複製し、
        # フリガナ欄は常に固定文字列"アシベース"を入れていた。カスタムの送信者名を
        # 設定したテナント(例: "東北三上機材株式会社")では明らかに不自然な内容に
        # なってしまう。未設定なら姓欄=会社名/名欄・フリガナ欄=空、設定済みなら
        # その値をそのまま使う、という2パターンを確認する。
        import offers as OF
        con.execute("DELETE FROM companies WHERE id=999997")
        con.execute("""INSERT INTO companies (id, name, contact_url)
            VALUES (999997, 'テスト_送信元情報確認', 'https://example.co.jp/contact/')""")
        con.commit()

        def _capture_values(tenant_id, offer_name, subject="件名", body="本文", track_clicks=False):
            offer_id = con.execute("SELECT id FROM offers WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
            cur = con.execute("""INSERT INTO campaigns (name, started_at, target_rule, offer_id)
                VALUES (?,?,?,?)""",
                (offer_name, datetime.now().isoformat(timespec="seconds"), "ALL", offer_id))
            cid = cur.lastrowid
            con.execute("""INSERT INTO touches (campaign_id, company_id, channel, variant, step,
                subject, body) VALUES (?,999997,'フォーム','A',1,?,?)""", (cid, subject, body))
            con.commit()
            captured = {}
            import form_navigator as FN
            orig_navigate = FN.navigate_and_submit
            FN.navigate_and_submit = lambda url, values, **k: (
                captured.update(values),
                FN.NavigationResult(status="SUCCESS", reason_code="success_text_matched",
                                    submit_attempted=True))[-1]
            try:
                send_campaign(con, cid, step=1, dry_run=False, track_clicks=track_clicks)
                if track_clicks:
                    # trackingリンクをクリックした体で解決し、その結果もcapturedへ
                    # 詰めて返す(呼び出し側がtouch_idを知らなくても確認できるように)。
                    m = re.search(r"/track/click/([A-Za-z0-9_-]+)", captured.get("message") or "")
                    if m:
                        captured["_click_target"] = db.resolve_click_token(con, m.group(1))
                        row = con.execute("""SELECT email_click_count, email_clicked_at
                            FROM touches WHERE campaign_id=?""", (cid,)).fetchone()
                        captured["_click_count"] = row["email_click_count"] if row else None
                        captured["_clicked_at"] = row["email_clicked_at"] if row else None
            finally:
                FN.navigate_and_submit = orig_navigate
                # touchesを消す前にemail_tracking_tokens(FK制約)を先に消す
                # (track_clicks=Trueのテストがトークンを発行した場合に備える)
                con.execute("""DELETE FROM email_tracking_tokens WHERE touch_id IN
                    (SELECT id FROM touches WHERE campaign_id=?)""", (cid,))
                con.execute("DELETE FROM touches WHERE campaign_id=?", (cid,))
                con.execute("DELETE FROM campaigns WHERE id=?", (cid,))
                con.commit()
            return captured

        orig_ks2, orig_ks2_reason = db.kill_switch_status(con)
        db.set_global_kill_switch(con, False, updated_by="test")
        try:
            tid_default, _ = OF.add_tenant(con, "test-sender-default", "default@example.co.jp",
                                            sender_name="東北三上機材株式会社")
            v_default = _capture_values(tid_default, "test-sender-default-campaign")
            ok_default = (v_default.get("last_name") == "東北三上機材株式会社"
                          and v_default.get("first_name") == "" and v_default.get("furigana") == ""
                          and v_default.get("postal_code") == "")
            print(f"  未設定時: last_name={v_default.get('last_name')!r} "
                  f"first_name={v_default.get('first_name')!r} furigana={v_default.get('furigana')!r}")
            print(f"  {'✓' if ok_default else '✗'} 未設定なら姓欄=会社名/名欄・フリガナ欄・郵便番号は空"
                  "(以前は名欄にも会社名を複製しフリガナは固定文字列だった)")

            tid_custom, _ = OF.add_tenant(con, "test-sender-custom", "custom@example.co.jp",
                                           sender_name="東北三上機材株式会社")
            con.execute("""UPDATE tenants SET sender_last_name=?, sender_first_name=?,
                sender_last_name_kana=?, sender_first_name_kana=?, sender_postal_code=?
                WHERE id=?""", ("中川", "太郎", "ナカガワ", "タロウ", "980-0021", tid_custom))
            con.commit()
            v_custom = _capture_values(tid_custom, "test-sender-custom-campaign")
            ok_custom = (v_custom.get("last_name") == "中川" and v_custom.get("first_name") == "太郎"
                         and v_custom.get("furigana") == "ナカガワタロウ"
                         and v_custom.get("postal_code") == "980-0021")
            print(f"  設定済み時: last_name={v_custom.get('last_name')!r} "
                  f"first_name={v_custom.get('first_name')!r} furigana={v_custom.get('furigana')!r} "
                  f"postal_code={v_custom.get('postal_code')!r}")
            print(f"  {'✓' if ok_custom else '✗'} 設定済みならその値がそのまま使われる")
        finally:
            db.set_global_kill_switch(con, orig_ks2, reason=orig_ks2_reason, updated_by="test-restore")
            con.execute("DELETE FROM companies WHERE id=999997")
            con.execute("DELETE FROM form_send_log WHERE company_id=999997")
            for tname in ("test-sender-default", "test-sender-custom"):
                for row in con.execute("SELECT id FROM tenants WHERE name=?", (tname,)).fetchall():
                    con.execute("DELETE FROM offers WHERE tenant_id=?", (row["id"],))
                    con.execute("DELETE FROM tenants WHERE id=?", (row["id"],))
            con.commit()

        print("\n── マージタグ(##TO_COMPANY_NAME##/##FROM_FAMILY_NAME##)・住所の構造化 ──")
        con.execute("""DELETE FROM companies WHERE id=999997""")
        con.execute("""INSERT INTO companies (id, name, contact_url)
            VALUES (999997, 'マージタグ確認先株式会社', 'https://example.co.jp/contact/')""")
        con.commit()
        orig_ks3, orig_ks3_reason = db.kill_switch_status(con)
        db.set_global_kill_switch(con, False, updated_by="test")
        try:
            tid_mt, _ = OF.add_tenant(con, "test-merge-tags", "mt@example.co.jp",
                                       sender_name="マージタグ送信元")
            con.execute("""UPDATE tenants SET sender_last_name=?, sender_prefecture=?,
                sender_city=?, sender_block=?, sender_building=?, sender_phone=?
                WHERE id=?""", ("山田", "東京都", "千代田区丸の内", "1-1-1", "3F", "03-1234-5678",
                                 tid_mt))
            con.commit()
            v_mt = _capture_values(tid_mt, "test-merge-tags-campaign",
                                    subject="##TO_COMPANY_NAME##様へのご案内",
                                    body="##TO_COMPANY_NAME##様\n\nいつも##FROM_FAMILY_NAME##がお世話になっております。")
            ok_subject = v_mt.get("subject") == "マージタグ確認先株式会社様へのご案内"
            ok_body = "マージタグ確認先株式会社様" in (v_mt.get("message") or "") \
                and "いつも山田がお世話になっております" in (v_mt.get("message") or "")
            print(f"  件名: {v_mt.get('subject')!r}")
            print(f"  {'✓' if ok_subject else '✗'} ##TO_COMPANY_NAME##が件名で送信先の会社名に置換される")
            print(f"  {'✓' if ok_body else '✗'} ##TO_COMPANY_NAME##/##FROM_FAMILY_NAME##が本文で置換される")
            ok_addr = (v_mt.get("prefecture") == "東京都" and v_mt.get("city") == "千代田区丸の内"
                       and v_mt.get("block") == "1-1-1" and v_mt.get("building") == "3F"
                       and v_mt.get("phone") == "03-1234-5678"
                       and v_mt.get("address") == "東京都千代田区丸の内1-1-13F")
            print(f"  住所: prefecture={v_mt.get('prefecture')!r} city={v_mt.get('city')!r} "
                  f"block={v_mt.get('block')!r} building={v_mt.get('building')!r} "
                  f"address(連結)={v_mt.get('address')!r} phone={v_mt.get('phone')!r}")
            print(f"  {'✓' if ok_addr else '✗'} 構造化住所の各項目が個別に入り、単一住所欄向けの"
                  "連結文字列とphoneも入る")
        finally:
            db.set_global_kill_switch(con, orig_ks3, reason=orig_ks3_reason, updated_by="test-restore")
            con.execute("DELETE FROM companies WHERE id=999997")
            con.execute("DELETE FROM form_send_log WHERE company_id=999997")
            for row in con.execute("SELECT id FROM tenants WHERE name='test-merge-tags'").fetchall():
                con.execute("DELETE FROM offers WHERE tenant_id=?", (row["id"],))
                con.execute("DELETE FROM tenants WHERE id=?", (row["id"],))
            con.commit()

        print("\n── URLアクセスの記録(MIKOMERUの「URLアクセスの記録」相当) ──")
        con.execute("DELETE FROM companies WHERE id=999997")
        con.execute("""INSERT INTO companies (id, name, contact_url)
            VALUES (999997, 'クリック計測確認先株式会社', 'https://example.co.jp/contact/')""")
        con.commit()
        orig_ks4, orig_ks4_reason = db.kill_switch_status(con)
        db.set_global_kill_switch(con, False, updated_by="test")
        try:
            tid_tc, _ = OF.add_tenant(con, "test-track-clicks", "tc@example.co.jp",
                                       sender_name="計測送信元")
            v_tc = _capture_values(tid_tc, "test-track-clicks-campaign",
                                    body="詳しくはこちらをご覧ください https://example.co.jp/service です",
                                    track_clicks=True)
            msg = v_tc.get("message") or ""
            ok_rewritten = ("/track/click/" in msg) and ("https://example.co.jp/service" not in msg)
            print(f"  本文(置換後): {msg!r}")
            print(f"  {'✓' if ok_rewritten else '✗'} track_clicks=Trueだと本文中のURLがトラッキングリンクへ置換される")
            ok_resolve = v_tc.get("_click_target") == "https://example.co.jp/service"
            print(f"  {'✓' if ok_resolve else '✗'} トラッキングリンクを解決すると元のURLに戻る")
            ok_count = v_tc.get("_click_count") == 1 and v_tc.get("_clicked_at") is not None
            print(f"  {'✓' if ok_count else '✗'} クリック解決でtouches.email_click_count/"
                  "email_clicked_atが記録される(count={} clicked_at={})".format(
                      v_tc.get("_click_count"), v_tc.get("_clicked_at")))

            v_notrack = _capture_values(tid_tc, "test-track-clicks-off-campaign",
                                         body="こちら https://example.co.jp/service2 です",
                                         track_clicks=False)
            ok_notrack = "https://example.co.jp/service2" in (v_notrack.get("message") or "") \
                and "/track/click/" not in (v_notrack.get("message") or "")
            print(f"  {'✓' if ok_notrack else '✗'} track_clicks=False(既定)なら本文のURLはそのまま")
        finally:
            db.set_global_kill_switch(con, orig_ks4, reason=orig_ks4_reason, updated_by="test-restore")
            con.execute("DELETE FROM companies WHERE id=999997")
            con.execute("DELETE FROM form_send_log WHERE company_id=999997")
            for row in con.execute("SELECT id FROM tenants WHERE name='test-track-clicks'").fetchall():
                con.execute("DELETE FROM offers WHERE tenant_id=?", (row["id"],))
                con.execute("DELETE FROM tenants WHERE id=?", (row["id"],))
            con.commit()

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
