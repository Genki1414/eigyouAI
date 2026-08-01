"""
run.py — パイプライン実行の司令塔
スクリプトを手で順番に叩くと必ず事故る（実際に、文面生成前に増殖をかける／別キャンペーンを
シミュレートする、という取り違えが発生した）。各ステップに前提条件チェックを付けて、
順序が満たされていなければ止める。

  python3 run.py status                 # 今どこまで進んでいるか
  python3 run.py all --demo             # 全ステップを順に実行（デモデータ）
  python3 run.py step scoring           # 単一ステップだけ
"""
import argparse, subprocess, sys
from datetime import datetime

import db

PY = sys.executable


def _gen_im():
    """IM生成はstdoutをファイルへ束ねる"""
    import subprocess, config as C
    out = subprocess.run([PY, "im.py"], capture_output=True, text=True)
    if out.returncode != 0:
        print("    " + (out.stderr or "")[-400:]); return False
    (C.OUT_DIR / "IM.md").write_text(out.stdout)
    print(f"    IM.md を生成 ({len(out.stdout.splitlines())}行)")
    return True


def sh(cmd):
    print(f"  $ {' '.join(cmd[1:])}")
    r = subprocess.run([PY] + cmd, capture_output=True, text=True)
    out = (r.stdout or "").strip()
    if out:
        print("\n".join("    " + l for l in out.splitlines()[-8:]))
    if r.returncode != 0:
        print("    " + (r.stderr or "").strip()[-500:])
    return r.returncode == 0


# 各ステップ: (説明, 前提条件, 実行, 完了条件)
def n_companies(con):
    return con.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]

def n_scored(con):
    return con.execute("SELECT COUNT(*) c FROM companies WHERE rank IS NOT NULL").fetchone()["c"]

def n_touch(con, step=None):
    q = "SELECT COUNT(*) c FROM touches" + (f" WHERE step={step}" if step else "")
    return con.execute(q).fetchone()["c"]

def n_composed(con):
    return con.execute("SELECT COUNT(*) c FROM touches WHERE body IS NOT NULL AND body!=''").fetchone()["c"]

def n_sent(con, step=None):
    q = "SELECT COUNT(*) c FROM touches WHERE sent_at IS NOT NULL" + (f" AND step={step}" if step else "")
    return con.execute(q).fetchone()["c"]


STEPS = [
    dict(key="migrate", desc="スキーマ作成・マイグレーション",
         pre=lambda con: (True, ""),
         run=lambda con, demo: True,   # db.migrate() は起動時に常に実行済み
         done=lambda con: True),

    dict(key="ingest", desc="企業データ投入",
         pre=lambda con: (True, ""),
         run=lambda con, demo: sh(["generate_sample.py"]) if demo else
             (print("  → 本番: python3 ingest.py data/kyoka_gyosha.csv") or False),
         done=lambda con: n_companies(con) > 0),

    dict(key="dedup", desc="名寄せ・重複統合",
         pre=lambda con: (n_companies(con) > 0, "企業データが未投入"),
         run=lambda con, demo: (print(f"    重複 {db.dedup(con)}件を統合") or True),
         done=lambda con: con.execute(
             "SELECT COUNT(*) c FROM companies WHERE name_norm IS NOT NULL").fetchone()["c"] > 0),

    dict(key="enrich", desc="AIエンリッチメント",
         pre=lambda con: (n_companies(con) > 0, "企業データが未投入"),
         run=lambda con, demo: True if demo else
             (print("  → 本番: python3 enrich.py --limit 500") or False),
         done=lambda con: con.execute(
             "SELECT COUNT(*) c FROM companies WHERE has_website IS NOT NULL").fetchone()["c"] > 0),

    dict(key="offers", desc="オファー/テナント定義",
         pre=lambda con: (True, ""),
         run=lambda con, demo: sh(["offers.py", "init"]),
         done=lambda con: con.execute(
             "SELECT COUNT(*) c FROM sqlite_master WHERE name='offers'").fetchone()["c"] == 1
             and con.execute("SELECT COUNT(*) c FROM offers").fetchone()["c"] > 0),

    dict(key="scoring", desc="スコアリング V1",
         pre=lambda con: (con.execute("SELECT COUNT(*) c FROM companies WHERE has_website IS NOT NULL"
                                      ).fetchone()["c"] > 0, "エンリッチメント未実施"),
         run=lambda con, demo: sh(["scoring.py"]),
         done=lambda con: n_scored(con) > 0),

    dict(key="campaign", desc="キャンペーン作成（接触ガード適用）",
         pre=lambda con: (n_scored(con) > 0, "スコアリング未実施"),
         run=lambda con, demo: sh(["campaign.py", "create",
                                   f"{datetime.now():%Y年%m月} S+A 初回接触", "--target", "SA"]),
         done=lambda con: n_touch(con, 1) > 0),

    dict(key="compose", desc="AI文面生成",
         pre=lambda con: (n_touch(con, 1) > 0, "キャンペーン未作成"),
         run=lambda con, demo: sh(["compose.py", "--campaign", "1"] + (["--offline"] if demo else [])),
         done=lambda con: n_composed(con) > 0),

    dict(key="send1", desc="Step1 送信",
         pre=lambda con: (n_composed(con) > 0, "文面が未生成（空メールが飛ぶ）"),
         run=lambda con, demo: sh(["campaign.py", "simulate", "1"]) if demo else
             (print("  → 本番: 送信APIへ接続") or False),
         done=lambda con: n_sent(con, 1) > 0),

    dict(key="evolve", desc="勝ち文面の派生生成",
         pre=lambda con: (n_sent(con, 1) > 0, "Step1が未送信（勝ち文面が判定できない）"),
         run=lambda con, demo: sh(["followup.py", "--campaign", "1", "--evolve"] +
                                  (["--offline"] if demo else [])),
         done=lambda con: con.execute(
             "SELECT COUNT(*) c FROM touches WHERE variant LIKE '%''%'").fetchone()["c"] > 0),

    dict(key="step2", desc="Step2 フォロー（D+14）",
         pre=lambda con: (n_sent(con, 1) > 0, "Step1が未送信"),
         run=lambda con, demo: sh(["followup.py", "--campaign", "1", "--step", "2"] +
                                  (["--simulate"] if demo else [])),
         done=lambda con: n_touch(con, 2) > 0),

    dict(key="step3", desc="Step3 最終接触（D+35）",
         pre=lambda con: (n_sent(con, 2) > 0, "Step2が未送信"),
         run=lambda con, demo: sh(["followup.py", "--campaign", "1", "--step", "3"] +
                                  (["--simulate"] if demo else [])),
         done=lambda con: n_touch(con, 3) > 0),

    dict(key="dormant", desc="休眠プール構築",
         pre=lambda con: (n_sent(con, 3) > 0, "Step3が未送信"),
         run=lambda con, demo: sh(["dormant.py", "--campaign", "1", "--pool"]),
         done=lambda con: con.execute("SELECT COUNT(*) c FROM dormant").fetchone()["c"] > 0),

    dict(key="metrics", desc="メトリクス集計",
         pre=lambda con: (n_sent(con) > 0, "送信実績なし"),
         run=lambda con, demo: sh(["metrics.py"]),
         done=lambda con: (db.C.OUT_DIR / "metrics.json").exists()),

    dict(key="learn", desc="スコアV2 学習",
         pre=lambda con: (con.execute("SELECT SUM(responded) s FROM touches").fetchone()["s"] or 0) >= 20
                         and (True, "") or (False, "反応が20件未満（係数が不安定になる）"),
         run=lambda con, demo: sh(["learn.py"]),
         done=lambda con: (db.C.OUT_DIR / "model_v2.json").exists()),

    dict(key="audit", desc="配信停止の遵守監査",
         pre=lambda con: (True, ""),
         run=lambda con, demo: sh(["suppress_cli.py", "check"]),
         done=lambda con: True),

    dict(key="im", desc="M&A用IM生成",
         pre=lambda con: ((db.C.OUT_DIR / "model_v2.json").exists(), "学習が未実施"),
         run=lambda con, demo: _gen_im(),
         done=lambda con: (db.C.OUT_DIR / "IM.md").exists()),
]


def status(con):
    print("\n  ステップ                          状態")
    print("  " + "─" * 46)
    for s in STEPS:
        try:
            done = s["done"](con)
        except Exception:
            done = False
        mark = "✓ 完了" if done else "・未"
        print(f"  {s['key']:<10} {s['desc']:<22} {mark}")
    print()


def run_all(con, demo, only=None):
    for s in STEPS:
        if only and s["key"] != only:
            continue
        started = datetime.now().isoformat(timespec="seconds")
        pre = s["pre"](con)
        ok, why = pre if isinstance(pre, tuple) else (pre, "")
        print(f"\n▶ {s['key']} — {s['desc']}")
        if not ok:
            print(f"  ✗ 前提条件を満たしていません: {why}")
            db.log_run(con, s["key"], "blocked", why, started)
            if not only:
                print("\n  ここで停止します。前段を先に実行してください。")
                return False
            return False
        try:
            if s["done"](con) and not only:
                print("  （実行済みのためスキップ）")
                continue
        except Exception:
            pass
        success = s["run"](con, demo)
        db.log_run(con, s["key"], "ok" if success else "failed", None, started)
        if not success and not only:
            print("  ✗ 失敗したため停止します")
            return False
    print("\n完了。")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "all", "step"])
    ap.add_argument("key", nargs="?")
    ap.add_argument("--demo", action="store_true", help="シミュレーションで通す")
    a = ap.parse_args()

    con = db.connect()
    db.migrate(con)
    if a.cmd == "status":
        status(con)
    elif a.cmd == "all":
        run_all(con, a.demo)
        status(con)
    elif a.cmd == "step":
        run_all(con, a.demo, only=a.key)
