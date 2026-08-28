"""
monitor.py — 異常時のメールアラート(T35)

「止まっていることに誰も気づかない」を防ぐための最小限の監視。新しい監視基盤を
作るのではなく、既存の判断ロジック(db.kill_switch_status/list_tenant_kill_switches、
suppress_cli.pyのcheck相当の遵守監査、form_send_logの直近失敗率)を1箇所から
まとめて呼び出し、異常があればメールで知らせるだけにとどめる。

同じ異常を検知するたびに毎回メールを送ると、本当に重要な変化(新たに起きた
異常)を見逃す(アラート疲れ)。ALERT_COOLDOWN_MINUTES以内に同じ異常(alert_key)を
再検知してもメールは送らず、標準出力にだけ残す(cron.logで後から追える)。

使い方:
  python3 monitor.py check              # 1回だけ実行(通常はcronから)
  python3 monitor.py check --force      # クールダウンを無視して必ず送る(動作確認用)

crontab登録例(30分おき。deploy/crontabに追加済み):
  */30 * * * * cd /app && python3 monitor.py check >> /app/out/cron.log 2>&1

終了コード: 0=異常なし / 1=警告のみ / 2=緊急あり(cron.logの監視や
外形監視ツールでの二次検知にも使えるようにするため)。
"""
import argparse
import os
from datetime import datetime, timedelta

import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_state (
  alert_key TEXT PRIMARY KEY,
  last_sent_at TEXT NOT NULL
);
"""

ALERT_COOLDOWN_MINUTES = 60  # 同じ異常について、これより短い間隔では再送しない

# 直近フォーム送信の失敗率チェック(対象サイトの仕様変更・IPブロック等の早期検知)
FAILURE_WINDOW_HOURS = 1
FAILURE_MIN_SAMPLE = 5        # これ未満の試行数では判定しない(1〜2件の失敗で誤報しないため)
FAILURE_RATE_THRESHOLD = 0.5


def collect_alerts(con):
    """(alert_key, level, title, detail) のタプルのリストを返す。
    levelは'critical'(即対応が要る)/'warning'(様子見でよい)の2段階。"""
    alerts = []

    stopped, reason = db.kill_switch_status(con)
    if stopped:
        alerts.append(("kill_switch_global", "critical",
                        "全体Kill Switchが停止中です",
                        f"理由: {reason}\n意図的な停止でなければ、kill_switch_cli.py resume "
                        "で解除してください。"))

    tenant_stops = db.list_tenant_kill_switches(con)
    if tenant_stops:
        names = "、".join(f"{t['tenant_name'] or t['tenant_id']}（{t['reason'] or '理由未記録'}）"
                          for t in tenant_stops)
        alerts.append(("kill_switch_tenant", "warning",
                        f"テナント別Kill Switchが{len(tenant_stops)}件停止中です",
                        names))

    bad = con.execute("""SELECT COUNT(*) c FROM suppression s
        JOIN touches t ON t.company_id = s.company_id
        WHERE t.sent_at IS NOT NULL AND t.sent_at > s.created_at""").fetchone()["c"]
    if bad:
        alerts.append(("suppression_violation", "critical",
                        f"配信停止後に送信された記録が{bad}件あります",
                        "suppress_cli.py check で詳細を確認し、原因を特定してください"
                        "(特定電子メール法上のリスクです)。"))

    pending = con.execute("""SELECT COUNT(*) c FROM touches t
        JOIN suppression s ON s.company_id = t.company_id
        WHERE t.sent_at IS NULL""").fetchone()["c"]
    if pending:
        alerts.append(("suppression_pending", "warning",
                        f"配信停止対象に未送信の予定が{pending}件残っています",
                        "suppress_cli.py check で詳細を確認し、取り消してください。"))

    since = (datetime.now() - timedelta(hours=FAILURE_WINDOW_HOURS)).isoformat(timespec="seconds")
    row = con.execute("""SELECT COUNT(*) attempts,
            SUM(CASE WHEN status IN ('FAILED_RETRYABLE','FAILED_UNSUPPORTED') THEN 1 ELSE 0 END) failed
        FROM form_send_log WHERE started_at >= ?""", (since,)).fetchone()
    if row["attempts"] and row["attempts"] >= FAILURE_MIN_SAMPLE:
        rate = (row["failed"] or 0) / row["attempts"]
        if rate > FAILURE_RATE_THRESHOLD:
            alerts.append(("form_send_failure_rate", "warning",
                           f"直近{FAILURE_WINDOW_HOURS}時間のフォーム送信失敗率が"
                           f"{rate * 100:.0f}%です({row['failed']}/{row['attempts']}件)",
                           "対象サイトの仕様変更・ネットワーク障害・送信元IPのブロック等の"
                           "可能性があります。out/form_screenshots/の直近失敗分を確認してください。"))

    import backup as _backup
    import config as _config
    last_at, last_path = _backup.last_success()
    if last_at is None:
        alerts.append(("backup_stale", "critical",
                       "バックアップが一度も成功していません(記録がありません)",
                       "python3 backup.py run を手動実行し、エラーがあれば対処してください。"))
    elif datetime.now() - last_at > timedelta(hours=_config.BACKUP_STALE_HOURS):
        hours_ago = (datetime.now() - last_at).total_seconds() / 3600
        alerts.append(("backup_stale", "critical",
                       f"直近{_config.BACKUP_STALE_HOURS}時間以内にバックアップが成功していません"
                       f"(最終成功: {hours_ago:.0f}時間前)",
                       f"最後の成功: {last_path}\ncron.logでbackup.py runの失敗理由を確認してください。"))

    offsite_configured, offsite_at = _backup.last_offsite_success()
    if offsite_configured:
        if offsite_at is None:
            alerts.append(("backup_offsite_stale", "warning",
                           "オフサイト複製(BACKUP_OFFSITE_TARGET)が設定されていますが、"
                           "一度も成功していません",
                           "cron.logでbackup.py runのrsyncエラーを確認してください"
                           "(ローカルのバックアップ自体は別途成功しています)。"))
        elif datetime.now() - offsite_at > timedelta(hours=_config.BACKUP_OFFSITE_STALE_HOURS):
            hours_ago = (datetime.now() - offsite_at).total_seconds() / 3600
            alerts.append(("backup_offsite_stale", "warning",
                           f"直近{_config.BACKUP_OFFSITE_STALE_HOURS}時間以内にオフサイト複製が"
                           f"成功していません(最終成功: {hours_ago:.0f}時間前)",
                           "cron.logでbackup.py runのrsyncエラーを確認してください"
                           "(ローカルのバックアップ自体は別途成功しています)。"))

    return alerts


def _due(con, key, force):
    if force:
        return True
    row = con.execute("SELECT last_sent_at FROM alert_state WHERE alert_key=?", (key,)).fetchone()
    if not row:
        return True
    return datetime.now() - datetime.fromisoformat(row["last_sent_at"]) \
        >= timedelta(minutes=ALERT_COOLDOWN_MINUTES)


def _mark_sent(con, key):
    now = datetime.now().isoformat(timespec="seconds")
    con.execute("""INSERT INTO alert_state (alert_key, last_sent_at) VALUES (?,?)
        ON CONFLICT(alert_key) DO UPDATE SET last_sent_at=excluded.last_sent_at""", (key, now))
    con.commit()


def _send_alert_email(con, to_email, due_alerts):
    """アラートメールを1通にまとめて送る。呼び出し元に成否をそのまま返す
    (T33/T34の_send_..._email系と違い、こちらは戻り値で成否を返す設計。
    メール送信に失敗した場合はクールダウンを進めず、次回すぐ再送を試みたいため)。"""
    import senders
    icon = "🔴" if any(level == "critical" for _, level, _, _ in due_alerts) else "🟡"
    subject = f"【ヒラケル監視】{icon} 異常を検知しました({len(due_alerts)}件)"
    lines = []
    for _, level, title, detail in due_alerts:
        badge = "[緊急]" if level == "critical" else "[注意]"
        lines.append(f"{badge} {title}\n{detail}\n")
    body = "\n".join(lines)
    default_sender = senders.Sender(name="ヒラケル", email="info@hirakeru.jp",
                                     address="", optout_url="https://hirakeru.jp/optout")
    mailer = senders.MailSender(con, dry_run=False)
    mailer._deliver(senders.Recipient(company_id=0, name="運用担当", email=to_email),
                    default_sender, subject, body)


def run_check(con, force=False):
    con.executescript(SCHEMA)
    con.commit()
    alerts = collect_alerts(con)
    if not alerts:
        print("── ヒラケル監視(T35) ── 異常なし")
        return 0

    print(f"── ヒラケル監視(T35) ── {len(alerts)}件の異常を検知")
    due_alerts = []
    for key, level, title, detail in alerts:
        badge = "🔴" if level == "critical" else "🟡"
        due = _due(con, key, force)
        note = "" if due else "(クールダウン中のためメール送信はスキップ)"
        print(f"  {badge} {title}{note}")
        if due:
            due_alerts.append((key, level, title, detail))

    if due_alerts:
        to_email = os.environ.get("OPS_ALERT_EMAIL")
        if not to_email:
            print("  ⚠ OPS_ALERT_EMAILが未設定のため、メール送信はスキップします")
        else:
            try:
                _send_alert_email(con, to_email, due_alerts)
                print(f"  → {to_email} 宛にアラートメールを送信しました")
                for key, *_ in due_alerts:
                    _mark_sent(con, key)
            except NotImplementedError:
                print("  ⚠ メール送信基盤(SENDGRID_API_KEY)が未設定のため送信できません"
                      "(クールダウンは進めず、次回すぐ再試行します)")
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠ アラートメールの送信に失敗しました: {e}"
                      "(クールダウンは進めず、次回すぐ再試行します)")

    return 2 if any(level == "critical" for _, level, _, _ in alerts) else 1


def test():
    """python3 monitor.py test。既存の運用状態(Kill Switch等)は必ずテスト前の
    値へ復元する(api.py testのKill Switchセクションと同じ方針)。"""
    import senders as _senders
    from datetime import datetime as _dt, timedelta as _td

    con = db.connect(); db.migrate(con)
    con.executescript(SCHEMA); con.commit()
    passed, failed = [0], [0]

    def t(desc, cond, extra=""):
        if cond:
            passed[0] += 1
            print(f"  ✓ {desc}")
        else:
            failed[0] += 1
            print(f"  ✗ {desc} {extra}")

    def keys(alerts):
        return {a[0] for a in alerts}

    g0 = con.execute("SELECT stopped, reason FROM kill_switch WHERE id=1").fetchone()
    orig_stopped = bool(g0["stopped"]) if g0 else True
    orig_reason = g0["reason"] if g0 else None

    print("── collect_alerts(): Kill Switch ──")
    db.set_global_kill_switch(con, False, updated_by="monitor-test")
    db.set_tenant_kill_switch(con, 999997, False)
    t("両方オフならkill_switch系のアラートは出ない",
      not ({"kill_switch_global", "kill_switch_tenant"} & keys(collect_alerts(con))))

    db.set_global_kill_switch(con, True, reason="monitor-test-reason", updated_by="monitor-test")
    g_alert = next((a for a in collect_alerts(con) if a[0] == "kill_switch_global"), None)
    t("全体Kill Switch停止中はcriticalアラートが出る",
      g_alert is not None and g_alert[1] == "critical" and "monitor-test-reason" in g_alert[3])
    db.set_global_kill_switch(con, False, updated_by="monitor-test")

    db.set_tenant_kill_switch(con, 999997, True, reason="monitor-test-tenant")
    t_alert = next((a for a in collect_alerts(con) if a[0] == "kill_switch_tenant"), None)
    t("テナント別Kill Switch停止中はwarningアラートが出る",
      t_alert is not None and t_alert[1] == "warning" and "monitor-test-tenant" in t_alert[3])
    db.set_tenant_kill_switch(con, 999997, False)
    t("解除後はkill_switch_tenantアラートが消える",
      "kill_switch_tenant" not in keys(collect_alerts(con)))

    print("── collect_alerts(): 配信停止遵守 ──")
    now = _dt.now()
    mcid = con.execute("INSERT INTO companies (name, pref) VALUES (?,?)",
                       ("監視テスト株式会社", "東京都")).lastrowid
    con.execute("INSERT INTO suppression (company_id, reason, created_at) VALUES (?,?,?)",
                (mcid, "manual", (now - _td(days=1)).isoformat(timespec="seconds")))
    mcamp = con.execute("INSERT INTO campaigns (name, started_at, target_rule) VALUES (?,?,?)",
                        ("monitor-test", now.isoformat(timespec="seconds"), "ALL")).lastrowid
    con.execute("""INSERT INTO touches (campaign_id, company_id, channel, variant, step, body,
                   unit_cost_yen, sent_at) VALUES (?,?,?,?,?,?,?,?)""",
                (mcamp, mcid, "メール", "A", 1, "本文", 1, now.isoformat(timespec="seconds")))
    con.commit()
    v_alert = next((a for a in collect_alerts(con) if a[0] == "suppression_violation"), None)
    t("停止後送信があるとcriticalアラートが出る", v_alert is not None and v_alert[1] == "critical")
    con.execute("DELETE FROM touches WHERE campaign_id=?", (mcamp,))
    con.execute("DELETE FROM campaigns WHERE id=?", (mcamp,))
    con.execute("DELETE FROM suppression WHERE company_id=?", (mcid,))
    con.commit()
    t("片付け後はsuppression_violationアラートが(この会社の分は)消える",
      "suppression_violation" not in keys(collect_alerts(con)))

    con.execute("INSERT INTO suppression (company_id, reason, created_at) VALUES (?,?,?)",
                (mcid, "manual", now.isoformat(timespec="seconds")))
    mcamp2 = con.execute("INSERT INTO campaigns (name, started_at, target_rule) VALUES (?,?,?)",
                         ("monitor-test2", now.isoformat(timespec="seconds"), "ALL")).lastrowid
    con.execute("""INSERT INTO touches (campaign_id, company_id, channel, variant, step, body,
                   unit_cost_yen, sent_at) VALUES (?,?,?,?,?,?,?,NULL)""",
                (mcamp2, mcid, "メール", "A", 1, "本文", 1))
    con.commit()
    p_alert = next((a for a in collect_alerts(con) if a[0] == "suppression_pending"), None)
    t("停止対象への未送信予定があるとwarningアラートが出る",
      p_alert is not None and p_alert[1] == "warning")
    con.execute("DELETE FROM touches WHERE campaign_id=?", (mcamp2,))
    con.execute("DELETE FROM campaigns WHERE id=?", (mcamp2,))
    con.execute("DELETE FROM suppression WHERE company_id=?", (mcid,))
    con.execute("DELETE FROM companies WHERE id=?", (mcid,))
    con.commit()

    print("── collect_alerts(): フォーム送信失敗率 ──")
    ts = now.isoformat(timespec="seconds")

    def log_rows(n_failed, n_success):
        for _ in range(n_failed):
            con.execute("""INSERT INTO form_send_log (company_id, status, started_at)
                VALUES (0, 'FAILED_RETRYABLE', ?)""", (ts,))
        for _ in range(n_success):
            con.execute("""INSERT INTO form_send_log (company_id, status, started_at)
                VALUES (0, 'SUCCESS', ?)""", (ts,))
        con.commit()

    def clear_rows():
        con.execute("DELETE FROM form_send_log WHERE started_at=? AND company_id=0", (ts,))
        con.commit()

    log_rows(2, 1)
    t("試行数が最低サンプル数未満なら失敗率が高くても判定しない",
      "form_send_failure_rate" not in keys(collect_alerts(con)))
    clear_rows()

    log_rows(4, 2)
    f_alert = next((a for a in collect_alerts(con) if a[0] == "form_send_failure_rate"), None)
    t("サンプル十分・失敗率50%超でwarningアラートが出る",
      f_alert is not None and f_alert[1] == "warning")
    clear_rows()

    log_rows(1, 5)
    t("失敗率が閾値以下ならアラートは出ない",
      "form_send_failure_rate" not in keys(collect_alerts(con)))
    clear_rows()

    print("── collect_alerts(): バックアップ ──")
    import backup as _backup
    import config as _config
    import tempfile
    from pathlib import Path as _Path

    orig_backup_dir = _config.BACKUP_DIR
    _config.BACKUP_DIR = _Path(tempfile.mkdtemp(prefix="ashibase_monitor_test_"))
    try:
        t("バックアップの記録が無ければcriticalアラートが出る",
          "backup_stale" in keys(collect_alerts(con)))

        (_config.BACKUP_DIR / "last_success.json").write_text(
            f'{{"at": "{now.isoformat(timespec="seconds")}", "path": "dummy", "size_bytes": 1}}')
        t("直近にバックアップが成功していればアラートは出ない",
          "backup_stale" not in keys(collect_alerts(con)))

        stale_at = now - _td(hours=_config.BACKUP_STALE_HOURS + 1)
        (_config.BACKUP_DIR / "last_success.json").write_text(
            f'{{"at": "{stale_at.isoformat(timespec="seconds")}", "path": "dummy", "size_bytes": 1}}')
        b_alert = next((a for a in collect_alerts(con) if a[0] == "backup_stale"), None)
        t(f"最終成功が{_config.BACKUP_STALE_HOURS}時間より前ならcriticalアラートが出る",
          b_alert is not None and b_alert[1] == "critical")

        print("── collect_alerts(): オフサイト複製 ──")
        (_config.BACKUP_DIR / "last_success.json").write_text(
            f'{{"at": "{now.isoformat(timespec="seconds")}", "path": "dummy", "size_bytes": 1,'
            f'"offsite_configured": false}}')
        t("BACKUP_OFFSITE_TARGET未設定ならbackup_offsite_staleは出ない",
          "backup_offsite_stale" not in keys(collect_alerts(con)))

        (_config.BACKUP_DIR / "last_success.json").write_text(
            f'{{"at": "{now.isoformat(timespec="seconds")}", "path": "dummy", "size_bytes": 1,'
            f'"offsite_configured": true, "offsite_at": null}}')
        o_alert = next((a for a in collect_alerts(con) if a[0] == "backup_offsite_stale"), None)
        t("設定はあるのに一度も成功していなければwarningアラートが出る",
          o_alert is not None and o_alert[1] == "warning")

        (_config.BACKUP_DIR / "last_success.json").write_text(
            f'{{"at": "{now.isoformat(timespec="seconds")}", "path": "dummy", "size_bytes": 1,'
            f'"offsite_configured": true, "offsite_at": "{now.isoformat(timespec="seconds")}"}}')
        t("直近にオフサイト複製が成功していればアラートは出ない",
          "backup_offsite_stale" not in keys(collect_alerts(con)))

        offsite_stale_at = now - _td(hours=_config.BACKUP_OFFSITE_STALE_HOURS + 1)
        (_config.BACKUP_DIR / "last_success.json").write_text(
            f'{{"at": "{now.isoformat(timespec="seconds")}", "path": "dummy", "size_bytes": 1,'
            f'"offsite_configured": true, "offsite_at": "{offsite_stale_at.isoformat(timespec="seconds")}"}}')
        o_alert2 = next((a for a in collect_alerts(con) if a[0] == "backup_offsite_stale"), None)
        t(f"最終成功が{_config.BACKUP_OFFSITE_STALE_HOURS}時間より前ならwarningアラートが出る",
          o_alert2 is not None and o_alert2[1] == "warning")
    finally:
        import shutil as _shutil
        _shutil.rmtree(_config.BACKUP_DIR, ignore_errors=True)
        _config.BACKUP_DIR = orig_backup_dir

    print("── クールダウン(_due/_mark_sent) ──")
    con.execute("DELETE FROM alert_state WHERE alert_key='monitor-test-key'")
    con.commit()
    t("送信履歴が無いキーは常にdue", _due(con, "monitor-test-key", force=False))
    _mark_sent(con, "monitor-test-key")
    t("送信直後は同じキーはクールダウン中でdueではない",
      _due(con, "monitor-test-key", force=False) is False)
    t("forceを指定すればクールダウン中でも常にdue",
      _due(con, "monitor-test-key", force=True) is True)
    old = (now - _td(minutes=ALERT_COOLDOWN_MINUTES + 1)).isoformat(timespec="seconds")
    con.execute("UPDATE alert_state SET last_sent_at=? WHERE alert_key='monitor-test-key'", (old,))
    con.commit()
    t("クールダウン時間を過ぎれば再びdueになる", _due(con, "monitor-test-key", force=False))
    con.execute("DELETE FROM alert_state WHERE alert_key='monitor-test-key'")
    con.commit()

    print("── _send_alert_email() / run_check() ──")
    orig_deliver = _senders.MailSender._deliver
    orig_ops_email = os.environ.get("OPS_ALERT_EMAIL")
    os.environ["OPS_ALERT_EMAIL"] = "ops-test@example.co.jp"
    captured = []

    def fake_deliver_ok(self, to, sender, subject, body):
        captured.append((to.email, subject, body))
        return _senders.SendResult(ok=True, provider_id="fake-msg")

    def fake_deliver_fail(self, to, sender, subject, body):
        raise NotImplementedError("test-stub")

    try:
        db.set_global_kill_switch(con, True, reason="monitor-test-email", updated_by="monitor-test")
        con.execute("DELETE FROM alert_state WHERE alert_key='kill_switch_global'")
        con.commit()

        _senders.MailSender._deliver = fake_deliver_ok
        rc = run_check(con)
        t("異常検知時はrun_check()がexit code 2(critical)を返す", rc == 2)
        t("メール送信が成功するとメール本文に検知内容が含まれる",
          len(captured) == 1 and "monitor-test-email" in captured[0][2])
        row = con.execute("SELECT last_sent_at FROM alert_state WHERE alert_key='kill_switch_global'")\
            .fetchone()
        t("メール送信成功時はalert_stateへ記録される(クールダウン用)", row is not None)

        captured.clear()
        rc2 = run_check(con)
        t("クールダウン中の2回目はメールを再送しない", len(captured) == 0)
        t("クールダウン中でも異常自体は継続していればexit codeは2のまま", rc2 == 2)

        con.execute("DELETE FROM alert_state WHERE alert_key='kill_switch_global'")
        con.commit()
        _senders.MailSender._deliver = fake_deliver_fail
        rc3 = run_check(con)
        t("メール送信失敗時もrun_check()自体はエラーにならずexit codeを返す", rc3 == 2)
        row2 = con.execute("SELECT last_sent_at FROM alert_state WHERE alert_key='kill_switch_global'")\
            .fetchone()
        t("メール送信に失敗した場合はalert_stateへ記録しない(次回すぐ再試行できるように)",
          row2 is None)
    finally:
        _senders.MailSender._deliver = orig_deliver
        if orig_ops_email is None:
            os.environ.pop("OPS_ALERT_EMAIL", None)
        else:
            os.environ["OPS_ALERT_EMAIL"] = orig_ops_email
        con.execute("DELETE FROM alert_state WHERE alert_key='kill_switch_global'")
        db.set_global_kill_switch(con, orig_stopped, reason=orig_reason, updated_by="monitor-test-restore")
        con.commit()

    print(f"\n  成功 {passed[0]} / 失敗 {failed[0]}")
    return failed[0] == 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    check_p = sub.add_parser("check")
    check_p.add_argument("--force", action="store_true",
                          help="クールダウンを無視して必ずメール送信する(動作確認用)")
    sub.add_parser("test")
    args = ap.parse_args()

    if args.cmd == "test":
        raise SystemExit(0 if test() else 1)

    con = db.connect(); db.migrate(con)
    if args.cmd == "check":
        raise SystemExit(run_check(con, force=args.force))
