"""
screenshot_cleanup.py — 送信前後スクリーンショットの掃除(T142。2026-09-28)

フォーム送信は1試行につき「送信前」「送信後」の2枚を out/form_screenshots/ に保存する
(目視確認用。form_send_log.screenshot_before_path / screenshot_after_path にパスを持つ)。
11万社の送信中に1時間あたり約0.8GB増え、ディスク(75GB)の残りが11GBまで減った。
画像は目視確認のための補助情報で、送信結果(ログ)本体ではないので、役目を終えたら消す。

消す基準(ユーザー指示 2026-09-29「リスト送信消化後3日で消える」):
  1. リストの送信が終わって `--days N`(既定3)経った画像を消す。
     「終わった」= そのリストに順番待ち・送信中・停止中の予約が無く、そのリストの最後の
     試行から N 日経っている。リスト無しの送信(list_id が無い古い記録)は試行から N 日
  2. DB に記録の無い孤児ファイルは 30 日より古ければ消す
  3. それでも空き容量が `--min-free-gb G`(既定8)未満なら、古い順にさらに消す(安全弁)
  消した画像は form_send_log の該当パスを NULL にする(画面の「確認」ボタンが消える。
  消し忘れても API は404を返すだけで落ちない)

使い方:
  python3 screenshot_cleanup.py status                        # 枚数・容量・空きを表示するだけ
  python3 screenshot_cleanup.py run [--days 3] [--min-free-gb 8] [--dry]
  python3 screenshot_cleanup.py --selftest

deploy/crontab から毎日実行する(worker コンテナ。engine-data ボリュームを sender と共有)。
送信中に走っても、消すのは「送信が終わったリスト」の分だけなので送信には影響しない。
"""
import argparse
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import config as C

SCREENSHOT_DIR = C.OUT_DIR / "form_screenshots"
ORPHAN_DAYS = 30           # DBに記録の無いファイルを消すまでの日数
ACTIVE_STATUSES = ("PENDING", "RUNNING", "PAUSED")


def _scan(directory):
    """(path, mtime, size) の一覧。消えたファイルは無視する(送信中に走るため)。"""
    out = []
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return out
    for e in entries:
        try:
            if e.is_file(follow_symlinks=False):
                st = e.stat(follow_symlinks=False)
                out.append((Path(e.path), st.st_mtime, st.st_size))
        except OSError:
            continue
    return out


def _free_bytes(directory):
    return shutil.disk_usage(directory).free


def _size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def done_lists(con, *, days=3, now=None):
    """送信が終わって days 日経ったリストの list_id 一覧(None=リスト無しの記録も含みうる)。
    「終わった」= 順番待ち・送信中・停止中の予約が無い かつ 最後の試行が days 日より前。"""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    active = {r[0] for r in con.execute(
        f"SELECT DISTINCT list_id FROM scheduled_sends WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
        ACTIVE_STATUSES).fetchall()}
    rows = con.execute("""SELECT list_id, MAX(started_at) last_at FROM form_send_log
        WHERE screenshot_before_path IS NOT NULL OR screenshot_after_path IS NOT NULL
        GROUP BY list_id""").fetchall()
    out = []
    for r in rows:
        list_id, last_at = r[0], r[1]
        if list_id is None:
            out.append(None)      # リスト無しの記録は rows_for_list() が1行ずつ試行日で絞る
            continue
        if list_id in active:
            continue
        if last_at and last_at < cutoff:
            out.append(list_id)
    return out


def rows_for_list(con, list_id, *, days=3, now=None):
    """そのリストの画像付き記録 [(id, before_path, after_path)]。list_id=None は試行日で絞る。"""
    if list_id is None:
        now = now or datetime.now()
        cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
        return con.execute("""SELECT id, screenshot_before_path, screenshot_after_path FROM form_send_log
            WHERE list_id IS NULL AND started_at < ?
              AND (screenshot_before_path IS NOT NULL OR screenshot_after_path IS NOT NULL)""",
            (cutoff,)).fetchall()
    return con.execute("""SELECT id, screenshot_before_path, screenshot_after_path FROM form_send_log
        WHERE list_id = ? AND (screenshot_before_path IS NOT NULL OR screenshot_after_path IS NOT NULL)""",
        (list_id,)).fetchall()


def delete_rows(con, rows, *, dry=False):
    """記録に紐づく画像を消し、パスを NULL にする。戻り値 (消した枚数, バイト数)"""
    n = 0
    freed = 0
    ids = []
    for r in rows:
        row_id, before, after = r[0], r[1], r[2]
        for p in (before, after):
            if not p:
                continue
            path = Path(p)
            size = _size(path)
            if dry:
                if size:
                    n += 1
                    freed += size
                continue
            try:
                path.unlink()
                n += 1
                freed += size
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"  消せませんでした: {path} ({e})", file=sys.stderr)
        ids.append(row_id)
    if not dry and ids:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            con.execute(f"UPDATE form_send_log SET screenshot_before_path=NULL, screenshot_after_path=NULL "
                        f"WHERE id IN ({q})", chunk)
        con.commit()
    return n, freed


def known_paths(con):
    """DB が参照しているパスの集合(孤児ファイルの判定用)。"""
    out = set()
    for r in con.execute("""SELECT screenshot_before_path, screenshot_after_path FROM form_send_log
        WHERE screenshot_before_path IS NOT NULL OR screenshot_after_path IS NOT NULL""").fetchall():
        if r[0]:
            out.add(str(r[0]))
        if r[1]:
            out.add(str(r[1]))
    return out


def delete_files(targets, *, dry=False):
    n = 0
    freed = 0
    for path, _mtime, size in targets:
        if dry:
            n += 1
            freed += size
            continue
        try:
            path.unlink()
            n += 1
            freed += size
        except FileNotFoundError:
            continue
        except OSError as e:
            print(f"  消せませんでした: {path} ({e})", file=sys.stderr)
    return n, freed


def cmd_status(con, directory):
    files = _scan(directory)
    total = sum(f[2] for f in files)
    now = time.time()
    oldest = min((f[1] for f in files), default=None)
    print(f"スクリーンショット: {len(files):,}枚 / {total / 1e9:.2f}GB  ({directory})")
    if oldest:
        print(f"  最古: {(now - oldest) / 86400:.1f}日前")
    if con is not None:
        lists = done_lists(con)
        n = sum(len(rows_for_list(con, lid)) for lid in lists)
        print(f"  送信が終わって3日経ったリスト: {len(lists)}件(記録 {n:,}行が掃除の対象)")
    try:
        print(f"ディスクの空き: {_free_bytes(directory) / 1e9:.2f}GB")
    except FileNotFoundError:
        print("ディスクの空き: (ディレクトリがまだ無い)")


def cmd_run(con, directory, *, days, min_free_gb, dry, now=None):
    label = " (dry: 消さない)" if dry else ""
    now_dt = now or datetime.now()
    print(f"掃除開始{label}: 送信が終わって{days}日経ったリストの画像を消す")

    # 1. 送信が終わったリストの分
    n1 = b1 = 0
    for list_id in done_lists(con, days=days, now=now_dt):
        rows = rows_for_list(con, list_id, days=days, now=now_dt)
        n, b = delete_rows(con, rows, dry=dry)
        n1 += n
        b1 += b
        print(f"  リスト{list_id if list_id is not None else '(無し)'}: {n:,}枚 / {b / 1e9:.2f}GB")
    print(f"  小計: {n1:,}枚 / {b1 / 1e9:.2f}GB")

    # 2. DBに記録の無い孤児ファイル(30日より古いもの)
    files = _scan(directory)
    known = known_paths(con)
    orphan_cutoff = now_dt.timestamp() - ORPHAN_DAYS * 86400
    orphans = [f for f in files if str(f[0]) not in known and f[1] < orphan_cutoff]
    n2, b2 = delete_files(orphans, dry=dry)
    if n2:
        print(f"  記録の無い古いファイル: {n2:,}枚 / {b2 / 1e9:.2f}GB")

    # 3. 空き容量の安全弁: まだ足りなければ古い順に
    n3 = b3 = 0
    try:
        free = _free_bytes(directory)
    except FileNotFoundError:
        free = None
    need = int(min_free_gb * (1024 ** 3))
    if free is not None and free + (b1 + b2 if dry else 0) < need:
        remaining = sorted((f for f in _scan(directory)), key=lambda f: f[1])
        deleted_paths = []
        shortfall = need - free - (b1 + b2 if dry else 0)
        for f in remaining:
            if shortfall <= 0:
                break
            n, b = delete_files([f], dry=dry)
            n3 += n
            b3 += b
            shortfall -= f[2]
            deleted_paths.append(str(f[0]))
        if not dry and deleted_paths:
            for i in range(0, len(deleted_paths), 500):
                chunk = deleted_paths[i:i + 500]
                q = ",".join("?" * len(chunk))
                con.execute(f"UPDATE form_send_log SET screenshot_before_path=NULL "
                            f"WHERE screenshot_before_path IN ({q})", chunk)
                con.execute(f"UPDATE form_send_log SET screenshot_after_path=NULL "
                            f"WHERE screenshot_after_path IN ({q})", chunk)
            con.commit()
        print(f"  空き{min_free_gb}GB確保のため古い順に追加: {n3:,}枚 / {b3 / 1e9:.2f}GB")

    if not dry and free is not None:
        print(f"空き(掃除後): {_free_bytes(directory) / 1e9:.2f}GB")
    return n1 + n2 + n3


def _selftest():
    import sqlite3
    import tempfile
    ok = [0, 0]

    def t(name, cond):
        ok[0 if cond else 1] += 1
        print(("✓ " if cond else "✗ ") + name)

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "shots"
        d.mkdir()
        now = datetime(2026, 9, 29, 5, 0, 0)
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE form_send_log (id INTEGER PRIMARY KEY, list_id INTEGER, started_at TEXT, "
                    "screenshot_before_path TEXT, screenshot_after_path TEXT)")
        con.execute("CREATE TABLE scheduled_sends (id INTEGER PRIMARY KEY, list_id INTEGER, status TEXT)")

        def mk(name, age_days=0):
            f = d / name
            f.write_bytes(b"x" * 1000)
            ts = (now - timedelta(days=age_days)).timestamp()
            os.utime(f, (ts, ts))
            return str(f)

        def log(list_id, age_days, before, after=None):
            con.execute("INSERT INTO form_send_log (list_id, started_at, screenshot_before_path, screenshot_after_path) "
                        "VALUES (?, ?, ?, ?)",
                        (list_id, (now - timedelta(days=age_days)).isoformat(timespec="seconds"), before, after))

        # リストA: 5日前に終わった(予約はDONE) → 消す
        a1 = mk("a1_before.jpg", 5); a2 = mk("a1_after.jpg", 5); a3 = mk("a2_before.jpg", 6)
        log(1, 5, a1, a2); log(1, 6, a3)
        con.execute("INSERT INTO scheduled_sends (list_id, status) VALUES (1, 'DONE')")
        # リストB: 最後の試行が1日前 → まだ消さない
        b1 = mk("b1_before.jpg", 1); log(2, 1, b1)
        con.execute("INSERT INTO scheduled_sends (list_id, status) VALUES (2, 'DONE')")
        # リストC: 最後の試行は5日前だが、停止中(PAUSED)の予約がある → 消さない
        c1 = mk("c1_before.jpg", 5); log(3, 5, c1)
        con.execute("INSERT INTO scheduled_sends (list_id, status) VALUES (3, 'PAUSED')")
        # リストD: 4日前に終わった回と、いま実行中(RUNNING)の回がある → 消さない(実行中を優先)
        d1 = mk("d1_before.jpg", 4); log(4, 4, d1)
        con.execute("INSERT INTO scheduled_sends (list_id, status) VALUES (4, 'DONE')")
        con.execute("INSERT INTO scheduled_sends (list_id, status) VALUES (4, 'RUNNING')")
        # リスト無しの古い記録(10日前) → 消す。新しい(1日前) → 残す
        n1 = mk("n1_before.jpg", 10); log(None, 10, n1)
        n2 = mk("n2_before.jpg", 1); log(None, 1, n2)
        # 孤児ファイル: 40日前 → 消す。10日前 → 残す
        o1 = mk("orphan_old.png", 40); o2 = mk("orphan_new.png", 10)
        # 記録はあるがファイルは既に無い(前回消し損ね) → 落ちずに NULL になる
        log(1, 7, str(d / "gone_before.jpg"))
        con.commit()

        lists = done_lists(con, days=3, now=now)
        t("終わって3日経ったリストだけが対象(A と リスト無し)", sorted(x for x in lists if x is not None) == [1] and None in lists)

        n_dry = cmd_run(con, d, days=3, min_free_gb=0, dry=True, now=now)
        t("dryでは何も消えない", all(Path(p).exists() for p in (a1, a2, a3, b1, c1, d1, n1, n2, o1, o2)))
        t("dryでも枚数は数える(A:3 + リスト無し:1 + 孤児:1)", n_dry == 5)

        cmd_run(con, d, days=3, min_free_gb=0, dry=False, now=now)
        t("リストAの画像が消える", not any(Path(p).exists() for p in (a1, a2, a3)))
        t("リストB(1日前)・C(停止中)・D(実行中あり)は残る", all(Path(p).exists() for p in (b1, c1, d1)))
        t("リスト無しは10日前だけ消える", not Path(n1).exists() and Path(n2).exists())
        t("孤児ファイルは40日前だけ消える", not Path(o1).exists() and Path(o2).exists())
        rows = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT id, screenshot_before_path, screenshot_after_path FROM form_send_log").fetchall()}
        t("消した記録のパスはNULL", rows[1] == (None, None) and rows[2] == (None, None))
        t("残した記録のパスはそのまま", rows[3] == (b1, None) and rows[4] == (c1, None))
        t("ファイルが既に無い記録もNULLになる", rows[8] == (None, None))

        # 空き容量の安全弁: 空きを偽って「足りない」状態にする
        g = sys.modules[__name__]
        orig = g._free_bytes
        g._free_bytes = lambda _d: 1000
        try:
            cmd_run(con, d, days=3, min_free_gb=3000 / (1024 ** 3), dry=False, now=now)
        finally:
            g._free_bytes = orig
        remaining = sorted(p.name for p in d.iterdir())
        # 空き1000B・必要3000B → 不足2000B。古い順に orphan_new(10日) と c1(5日) の2枚で足りる
        t("安全弁は足りる分だけ古い順に消す(orphan_new・c1 が消え、d1・b1・n2 は残る)",
          "orphan_new.png" not in remaining and "c1_before.jpg" not in remaining
          and "d1_before.jpg" in remaining and "b1_before.jpg" in remaining and "n2_before.jpg" in remaining)
        rows = {r[0]: r[1] for r in con.execute("SELECT id, screenshot_before_path FROM form_send_log").fetchall()}
        t("安全弁で消した分だけDBのパスがNULL", rows[4] is None and rows[5] == d1)

        t("存在しないディレクトリでも落ちない", cmd_run(con, d / "none", days=3, min_free_gb=0, dry=True, now=now) == 0)

    print(f"\n{ok[0]} ✓ / {ok[1]} ✗")
    return ok[1] == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    p = sub.add_parser("run")
    p.add_argument("--days", type=int, default=3, help="リストの送信が終わってから何日で消すか(既定3)")
    p.add_argument("--min-free-gb", type=float, default=8.0)
    p.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if _selftest() else 1)
    import db
    con = db.connect()
    if args.cmd == "status":
        cmd_status(con, SCREENSHOT_DIR)
    elif args.cmd == "run":
        cmd_run(con, SCREENSHOT_DIR, days=args.days, min_free_gb=args.min_free_gb, dry=args.dry)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
