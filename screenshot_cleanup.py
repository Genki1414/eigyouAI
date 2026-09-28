"""
screenshot_cleanup.py — 送信前後スクリーンショットの掃除(T142。2026-09-28)

フォーム送信は1試行につき「送信前」「送信後」の2枚を out/form_screenshots/ に保存する
(目視確認用。form_send_log.screenshot_before_path / screenshot_after_path にパスを持つ)。
11万社の送信中に1時間あたり約0.8GB増え、ディスク(75GB)の残りが11GBまで減った。
画像は目視確認のための補助情報で、送信結果(ログ)本体ではないので、古い分は消してよい。

やること:
  1. `--days N` より古い画像を消す(既定14日)
  2. それでも空き容量が `--min-free-gb G` 未満なら、古い順にさらに消す(既定8GB)
  3. 消した画像は form_send_log の該当パスを NULL にする(画面の「確認」ボタンが消える。
     消し忘れても API は404を返すだけで落ちない)

使い方:
  python3 screenshot_cleanup.py status                       # 枚数・容量・空きを表示するだけ
  python3 screenshot_cleanup.py run [--days 14] [--min-free-gb 8] [--dry]
  python3 screenshot_cleanup.py --selftest

送信そのものには触らない(送信中に実行しても、消すのは古いファイルだけ)。
"""
import argparse
import os
import shutil
import sys
import time
from pathlib import Path

import config as C

SCREENSHOT_DIR = C.OUT_DIR / "form_screenshots"


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


def plan(directory, *, days=14, min_free_gb=8.0, now=None, free_bytes=None):
    """消す対象を決めて返す(実際には消さない)。
    戻り値: dict(old=[...], for_space=[...], total_files, total_bytes, free_bytes)"""
    now = now if now is not None else time.time()
    files = _scan(directory)
    cutoff = now - days * 86400
    old = [f for f in files if f[1] < cutoff]
    keep = sorted((f for f in files if f[1] >= cutoff), key=lambda f: f[1])
    free = free_bytes if free_bytes is not None else _free_bytes(directory) if files else 0
    need = int(min_free_gb * (1024 ** 3))
    freed = sum(f[2] for f in old)
    for_space = []
    while free + freed < need and keep:
        f = keep.pop(0)
        for_space.append(f)
        freed += f[2]
    return {"old": old, "for_space": for_space, "total_files": len(files),
            "total_bytes": sum(f[2] for f in files), "free_bytes": free}


def apply(con, targets, *, dry=False):
    """ファイルを消して DB のパスを NULL にする。戻り値: (消した数, 消したバイト数)"""
    n = 0
    freed = 0
    paths = []
    for path, _mtime, size in targets:
        if dry:
            n += 1
            freed += size
            continue
        try:
            path.unlink()
            n += 1
            freed += size
            paths.append(str(path))
        except FileNotFoundError:
            continue
        except OSError as e:
            print(f"  消せませんでした: {path} ({e})", file=sys.stderr)
    if con is not None and paths:
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            q = ",".join("?" * len(chunk))
            con.execute(f"UPDATE form_send_log SET screenshot_before_path=NULL "
                        f"WHERE screenshot_before_path IN ({q})", chunk)
            con.execute(f"UPDATE form_send_log SET screenshot_after_path=NULL "
                        f"WHERE screenshot_after_path IN ({q})", chunk)
        con.commit()
    return n, freed


def cmd_status(directory):
    files = _scan(directory)
    total = sum(f[2] for f in files)
    now = time.time()
    oldest = min((f[1] for f in files), default=None)
    print(f"スクリーンショット: {len(files):,}枚 / {total / 1e9:.2f}GB  ({directory})")
    if oldest:
        print(f"  最古: {(now - oldest) / 86400:.1f}日前")
    try:
        print(f"ディスクの空き: {_free_bytes(directory) / 1e9:.2f}GB")
    except FileNotFoundError:
        print("ディスクの空き: (ディレクトリがまだ無い)")


def cmd_run(con, directory, *, days, min_free_gb, dry):
    p = plan(directory, days=days, min_free_gb=min_free_gb)
    label = "(dry: 消さない)" if dry else ""
    print(f"対象 {p['total_files']:,}枚 / {p['total_bytes'] / 1e9:.2f}GB、空き {p['free_bytes'] / 1e9:.2f}GB {label}")
    n1, b1 = apply(con, p["old"], dry=dry)
    print(f"  {days}日より古い分: {n1:,}枚 / {b1 / 1e9:.2f}GB")
    n2, b2 = apply(con, p["for_space"], dry=dry)
    if n2:
        print(f"  空き{min_free_gb}GB確保のため古い順に追加: {n2:,}枚 / {b2 / 1e9:.2f}GB")
    if not dry:
        print(f"空き(掃除後): {_free_bytes(directory) / 1e9:.2f}GB")
    return n1 + n2


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
        now = time.time()
        mk = []
        for i, age_days in enumerate([30, 20, 10, 5, 1, 0]):
            f = d / f"run{i}_before.jpg"
            f.write_bytes(b"x" * 1000)
            os.utime(f, (now - age_days * 86400, now - age_days * 86400))
            mk.append(f)
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE form_send_log (id INTEGER PRIMARY KEY, screenshot_before_path TEXT, screenshot_after_path TEXT)")
        for f in mk:
            con.execute("INSERT INTO form_send_log (screenshot_before_path, screenshot_after_path) VALUES (?, ?)",
                        (str(f), str(f)))
        con.commit()

        p = plan(d, days=14, min_free_gb=0, now=now)
        t("14日より古い2枚が対象", sorted(x[0].name for x in p["old"]) == ["run0_before.jpg", "run1_before.jpg"])
        t("空きが足りていれば追加削除は無い", p["for_space"] == [])

        # 空きが足りないとき: 古い順に足りるまで消す(1000B×n)。空き3000B、必要6000B → 古い分2000B + 追加1000B
        p2 = plan(d, days=14, min_free_gb=6000 / (1024 ** 3), now=now, free_bytes=3000)
        t("空きが足りないと古い順に追加で消す", [x[0].name for x in p2["for_space"]] == ["run2_before.jpg"])

        n, b = apply(con, p["old"], dry=True)
        t("dryでは消えない", n == 2 and all(f.exists() for f in mk))
        n, b = apply(con, p["old"], dry=False)
        t("消した数とバイト数", n == 2 and b == 2000)
        t("ファイルが消えている", not mk[0].exists() and not mk[1].exists() and mk[2].exists())
        rows = con.execute("SELECT screenshot_before_path, screenshot_after_path FROM form_send_log ORDER BY id").fetchall()
        t("消した分のDBパスがNULLになる", rows[0] == (None, None) and rows[1] == (None, None))
        t("残した分のDBパスはそのまま", rows[2] == (str(mk[2]), str(mk[2])))

        n, b = apply(con, p["old"], dry=False)
        t("同じ対象を二度消しても落ちない", n == 0)
        t("存在しないディレクトリでも空扱い", plan(d / "none", days=14, min_free_gb=0)["total_files"] == 0)

    print(f"\n{ok[0]} ✓ / {ok[1]} ✗")
    return ok[1] == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    p = sub.add_parser("run")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--min-free-gb", type=float, default=8.0)
    p.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if _selftest() else 1)
    if args.cmd == "status":
        cmd_status(SCREENSHOT_DIR)
    elif args.cmd == "run":
        import db
        con = None if args.dry else db.connect()
        cmd_run(con, SCREENSHOT_DIR, days=args.days, min_free_gb=args.min_free_gb, dry=args.dry)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
