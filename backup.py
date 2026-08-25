"""
backup.py — SQLiteの安全なバックアップ+オフサイト複製(T36/T37)

生ファイルコピー(`cp out/companies.db ...`)は、WALモード運用中に書き込みと
重なると壊れたスナップショットを作りかねない(-wal/-shmファイルが未反映のまま
本体だけコピーされる恐れがある)。sqlite3標準の`Connection.backup()`は書き込み
と衝突しても一貫性のあるスナップショットを取れる、SQLite公式の安全な方法。

取得したバックアップは`PRAGMA integrity_check`で壊れていないことまで確認し、
成功時刻をマニフェスト(BACKUP_DIR/last_success.json)に記録する。この
マニフェストはmonitor.pyが読み、一定時間バックアップが成功していなければ
アラートメールを送る(T35のアラート基盤にそのまま乗せる。バックアップ専用の
通知経路を新たに作らない)。

オフサイト複製(T37): ローカルディスク上のバックアップだけでは、ディスク障害・
サーバー消失そのものには対応できない。BACKUP_OFFSITE_TARGET(rsyncの宛先。
例: Hetzner Storage Boxなら "u123456@u123456.your-storagebox.de:backups/")が
設定されていれば、ローカルバックアップ成功直後にrsyncで複製する。未設定なら
何もしない(SENDGRID_API_KEY等と同じ「未設定でも運用を止めない」方針)。

使い方:
  python3 backup.py run              # バックアップを1つ作成し、整合性確認+オフサイト複製する
  python3 backup.py list             # 既存バックアップの一覧
  python3 backup.py restore <path>   # 指定したバックアップから復元する(要確認プロンプト)
  python3 backup.py test             # 自己テスト

crontab登録例(毎日1時。deploy/crontabに設定済み):
  0 1 * * * cd /app && python3 backup.py run >> /app/out/cron.log 2>&1

Hetzner Storage Boxを使う場合の設定手順(.env.example参照):
  1. Hetzner ConsoleでStorage Boxを契約する(最安のBX11で足りる)
  2. サーバー上でSSH鍵を生成し(未生成なら `ssh-keygen -t ed25519`)、
     公開鍵をStorage Box管理画面の「SSH-Keys」に登録する
  3. .envに BACKUP_OFFSITE_TARGET=u123456@u123456.your-storagebox.de:backups/
     を設定する(uXXXXXXはStorage Box契約時に発行されるユーザー名)
  4. Storage Boxの管理画面で対象ディレクトリ(上の例ではbackups/)を作成しておく

現時点ではSQLiteのみ対応。DATABASE_URL設定時(Postgres)は、pg_dump等への
切替が必要(storage.pyのバックエンド切替点と同じ考え方。今回は対象外)。
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import config as C


def _manifest_path():
    return C.BACKUP_DIR / "last_success.json"


def run_backup():
    """バックアップを1件作成する。戻り値: (ok, path_or_None, message)。"""
    import storage
    if storage.backend() != "sqlite":
        return False, None, ("SQLite以外のバックエンドには未対応です"
                              "(Postgres移行時はpg_dump等に切り替えること)")

    C.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not Path(C.DB_PATH).exists():
        return False, None, f"DBファイルが見つかりません: {C.DB_PATH}"

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = C.BACKUP_DIR / f"companies_{stamp}.db"

    src_con = sqlite3.connect(str(C.DB_PATH))
    dst_con = sqlite3.connect(str(dest))
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()

    ok, detail = verify_backup(dest)
    if not ok:
        return False, dest, f"バックアップの整合性チェックに失敗しました: {detail}"

    offsite_ok, offsite_msg = sync_offsite(dest)
    _write_manifest(dest, offsite_ok, offsite_msg)
    _prune_old_backups()
    if offsite_ok is False:
        # オフサイト複製の失敗はローカルバックアップ自体の成否とは分けて扱う。
        # ローカルは既に安全に取得できているため、run_backup()全体はokのまま返す
        # (呼び出し元のcronはこれを見て0で終了する)。失敗はmessageで分かるようにし、
        # 実際の通知はmonitor.pyのbackup_offsite_failedアラート側に任せる。
        return True, dest, f"ローカルは成功。オフサイト複製に失敗: {offsite_msg}"
    return True, dest, "ok"


def sync_offsite(path):
    """rsyncでオフサイト先(Hetzner Storage Box等)へバックアップを複製する(T37)。
    BACKUP_OFFSITE_TARGET未設定時は(True, None)を返し「対象外」を表す
    (False=失敗、と区別するため)。戻り値: (ok: bool|None, message: str|None)。"""
    target = os.environ.get("BACKUP_OFFSITE_TARGET")
    if not target:
        return None, None
    port = os.environ.get("BACKUP_OFFSITE_SSH_PORT", "23")  # Storage Boxの既定SSHポート
    cmd = ["rsync", "-az", "-e", f"ssh -p {port} -o StrictHostKeyChecking=accept-new "
           "-o BatchMode=yes -o ConnectTimeout=15", str(path), target]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        return True, "ok"
    except subprocess.CalledProcessError as e:
        return False, f"rsync失敗(exit={e.returncode}): {e.stderr.decode(errors='ignore')[:300]}"
    except FileNotFoundError:
        return False, "rsyncコマンドが見つかりません(サーバーにインストールされているか確認してください)"
    except subprocess.TimeoutExpired:
        return False, "rsyncがタイムアウトしました(300秒)"


def verify_backup(path):
    """PRAGMA integrity_checkで壊れていないか確認する。戻り値: (ok, detail)。
    SQLiteのファイル形式ですらない場合はintegrity_check自体が例外を投げるため、
    それも「壊れている」の一種として扱う。"""
    con = sqlite3.connect(str(path))
    try:
        rows = con.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as e:
        return False, str(e)
    finally:
        con.close()
    detail = "; ".join(r[0] for r in rows)
    return (len(rows) == 1 and rows[0][0] == "ok"), detail


def _write_manifest(path, offsite_ok, offsite_msg):
    """offsite_atは「オフサイト複製が最後に成功した時刻」を保持し続ける
    (直近の実行が失敗しても、前回までの成功実績を上書きで消さないため。
    monitor.pyのbackup_offsite_stale判定はこの時刻の新しさだけを見る)。"""
    now_iso = datetime.now().isoformat(timespec="seconds")
    prev = {}
    p = _manifest_path()
    if p.exists():
        try:
            prev = json.loads(p.read_text())
        except (ValueError, json.JSONDecodeError):
            prev = {}
    manifest = {"at": now_iso, "path": str(path), "size_bytes": path.stat().st_size,
                "offsite_configured": offsite_ok is not None,
                "offsite_last_ok": offsite_ok,
                "offsite_last_message": offsite_msg,
                "offsite_at": now_iso if offsite_ok else prev.get("offsite_at")}
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


def _prune_old_backups():
    cutoff = datetime.now() - timedelta(days=C.BACKUP_RETENTION_DAYS)
    for f in C.BACKUP_DIR.glob("companies_*.db"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()


def last_success():
    """monitor.pyから使う。(at: datetime|None, path: str|None) を返す。
    マニフェストが無い/壊れている場合はNoneを返す(=バックアップの実績が
    確認できない、という扱いにする)。"""
    p = _manifest_path()
    if not p.exists():
        return None, None
    try:
        data = json.loads(p.read_text())
        return datetime.fromisoformat(data["at"]), data.get("path")
    except (ValueError, KeyError, json.JSONDecodeError):
        return None, None


def last_offsite_success():
    """monitor.pyから使う。(configured: bool, at: datetime|None) を返す。
    configured=FalseならBACKUP_OFFSITE_TARGET自体が未設定(=まだ導入していない
    だけなので、アラート対象にはしない)。configured=Trueでat=Noneなら
    「設定はあるのに一度も成功していない」という異常な状態を表す。"""
    p = _manifest_path()
    if not p.exists():
        return False, None
    try:
        data = json.loads(p.read_text())
        if not data.get("offsite_configured"):
            return False, None
        at = data.get("offsite_at")
        return True, (datetime.fromisoformat(at) if at else None)
    except (ValueError, KeyError, json.JSONDecodeError):
        return False, None


def list_backups():
    files = sorted(C.BACKUP_DIR.glob("companies_*.db"), reverse=True)
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        print(f"  {f.name}  {size_mb:.1f}MB  {datetime.fromtimestamp(f.stat().st_mtime)}")
    if not files:
        print("  バックアップはまだありません")
    return files


def restore(path):
    """指定したバックアップファイルで本番DBを上書きする。取り消せない操作なので
    必ず確認プロンプトを挟む(スクリプトから自動実行させない設計)。"""
    src = Path(path)
    if not src.exists():
        print(f"ファイルが見つかりません: {path}")
        return False
    ok, detail = verify_backup(src)
    if not ok:
        print(f"このバックアップは壊れています。復元を中止します: {detail}")
        return False
    print(f"⚠ {C.DB_PATH} を {src} の内容で上書きします。現在のDBは失われます。")
    answer = input("本当に実行しますか? 'yes' と入力してください: ")
    if answer.strip().lower() != "yes":
        print("中止しました")
        return False
    # 復元前の状態も一応退避しておく(誤操作からの二段階の保険)
    safety = C.BACKUP_DIR / f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    if Path(C.DB_PATH).exists():
        shutil.copy2(C.DB_PATH, safety)
        print(f"  復元前の状態を退避しました: {safety}")
    shutil.copy2(src, C.DB_PATH)
    print(f"✓ 復元しました: {src} → {C.DB_PATH}")
    return True


def test():
    import tempfile

    passed, failed = [0], [0]

    def t(desc, cond, extra=""):
        if cond:
            passed[0] += 1
            print(f"  ✓ {desc}")
        else:
            failed[0] += 1
            print(f"  ✗ {desc} {extra}")

    orig_db_path, orig_backup_dir = C.DB_PATH, C.BACKUP_DIR
    tmpdir = Path(tempfile.mkdtemp(prefix="ashibase_backup_test_"))
    try:
        C.DB_PATH = tmpdir / "companies.db"
        C.BACKUP_DIR = tmpdir / "backups"

        con = sqlite3.connect(str(C.DB_PATH))
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        con.execute("INSERT INTO t (v) VALUES ('hello')")
        con.commit(); con.close()

        ok, path, msg = run_backup()
        t("正常なDBのバックアップが成功する", ok and path is not None, msg)
        t("バックアップファイルが実際に作られる", path.exists())
        t("整合性チェックがokを返す", verify_backup(path)[0])

        at, p = last_success()
        t("last_success()がマニフェストの時刻を返す", at is not None and (datetime.now() - at).total_seconds() < 10)
        t("last_success()がバックアップのパスを返す", p == str(path))

        con2 = sqlite3.connect(str(path))
        row = con2.execute("SELECT v FROM t").fetchone()
        con2.close()
        t("バックアップの中身が元のDBと一致する", row and row[0] == "hello")

        print("── オフサイト複製(T37) ──")
        t("BACKUP_OFFSITE_TARGET未設定ならlast_offsite_success()は(False, None)",
          last_offsite_success() == (False, None))

        global sync_offsite
        orig_sync_offsite = sync_offsite
        try:
            sync_offsite = lambda p: (True, "ok")
            ok2, path2, msg2 = run_backup()
            t("オフサイト複製成功時もrun_backup()は成功のまま", ok2 and "オフサイト" not in msg2)
            configured, offsite_at = last_offsite_success()
            t("オフサイト複製成功時はconfigured=Trueで直近の成功時刻が取れる",
              configured and offsite_at is not None
              and (datetime.now() - offsite_at).total_seconds() < 10)

            sync_offsite = lambda p: (False, "test-rsync-failure")
            ok3, path3, msg3 = run_backup()
            t("オフサイト複製が失敗してもローカルバックアップ自体は成功扱い",
              ok3 and "オフサイト複製に失敗" in msg3)
            configured3, offsite_at3 = last_offsite_success()
            t("直近が失敗しても、前回までの成功時刻は消えずに残る(offsite_atを上書きしない)",
              configured3 and offsite_at3 == offsite_at)
        finally:
            sync_offsite = orig_sync_offsite

        files = list_backups()
        t("list_backups()に作成したファイルが出てくる", path in files)

        # 保持日数を過ぎた古いバックアップは掃除されることを確認する
        old = C.BACKUP_DIR / "companies_20000101_000000.db"
        shutil.copy2(path, old)
        import os
        old_time = (datetime.now() - timedelta(days=C.BACKUP_RETENTION_DAYS + 1)).timestamp()
        os.utime(old, (old_time, old_time))
        _prune_old_backups()
        t("保持期間を過ぎたバックアップは自動削除される", not old.exists())
        t("保持期間内のバックアップは残る", path.exists())

        # 壊れたファイルの検知
        broken = C.BACKUP_DIR / "broken.db"
        broken.write_bytes(b"not a valid sqlite file")
        ok_b, detail_b = verify_backup(broken)
        t("壊れたファイルはintegrity_checkでok以外を返す", ok_b is False)

        print("── restore() ──")
        # DBを別内容に変えてから、バックアップ時点の内容へ復元できることを確認する
        con3 = sqlite3.connect(str(C.DB_PATH))
        con3.execute("INSERT INTO t (v) VALUES ('after-backup')")
        con3.commit(); con3.close()

        import builtins
        orig_input = builtins.input
        builtins.input = lambda _: "yes"
        try:
            restored = restore(path)
        finally:
            builtins.input = orig_input
        t("restore()がyes入力で実行される", restored is True)
        con4 = sqlite3.connect(str(C.DB_PATH))
        rows = con4.execute("SELECT v FROM t ORDER BY id").fetchall()
        con4.close()
        t("restore()後はバックアップ時点の内容に戻る(after-backupが消える)",
          [r[0] for r in rows] == ["hello"])

        builtins.input = lambda _: "no"
        try:
            declined = restore(path)
        finally:
            builtins.input = orig_input
        t("確認プロンプトで'yes'以外を入力すると復元しない", declined is False)
    finally:
        C.DB_PATH, C.BACKUP_DIR = orig_db_path, orig_backup_dir
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n  成功 {passed[0]} / 失敗 {failed[0]}")
    return failed[0] == 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("list")
    r = sub.add_parser("restore")
    r.add_argument("path")
    sub.add_parser("test")
    args = ap.parse_args()

    if args.cmd == "test":
        raise SystemExit(0 if test() else 1)
    elif args.cmd == "run":
        ok, path, msg = run_backup()
        print(f"{'✓' if ok else '✗'} {msg if not ok else f'バックアップ完了: {path}'}")
        raise SystemExit(0 if ok else 1)
    elif args.cmd == "list":
        list_backups()
    elif args.cmd == "restore":
        raise SystemExit(0 if restore(args.path) else 1)
