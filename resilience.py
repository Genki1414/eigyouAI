"""
resilience.py — 落ちない・止まらない・二重送信しないための共通基盤

3万社を回す処理は必ず途中で落ちる（API側の429、ネットワーク断、レート超過）。
落ちること自体は避けられないので、「落ちても失うものが1件分だけ」になる設計にする。

提供するもの:
  1. RateLimiter    … トークンバケット。API/送信APIの秒間・分間上限を守る
  2. retry()        … 指数バックオフ + ジッター。429/5xxのみ再試行し、4xxは即諦める
  3. Checkpoint     … 処理済みIDをDBに刻む。再実行すると続きから走る
  4. Idempotency    … 同じ送信を二度実行しない鍵。二重送信は信用を一発で失う

使い方:
    from resilience import RateLimiter, retry, Checkpoint

    rl = RateLimiter(per_minute=50)
    ck = Checkpoint(con, job="enrich")
    for cid in ck.remaining(all_ids):
        rl.acquire()
        result = retry(lambda: call_api(cid), job=f"enrich:{cid}")
        ck.done(cid)
"""
import json
import random
import sqlite3
import threading
import time
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
  job TEXT NOT NULL,
  item_id TEXT NOT NULL,
  status TEXT NOT NULL,          -- done / failed
  attempts INTEGER DEFAULT 1,
  error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (job, item_id)
);
CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY,          -- 例: send:campaign1:company27:step2
  result TEXT,
  created_at TEXT NOT NULL
);
"""


# ── 1. レートリミッター ──────────────────────
class RateLimiter:
    """トークンバケット。複数スレッドから使っても上限を割らない。"""

    def __init__(self, per_minute=60, burst=None):
        self.rate = per_minute / 60.0          # トークン/秒
        self.capacity = burst or max(1, per_minute // 6)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, n=1):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
            time.sleep(min(wait, 5.0))


# ── 2. リトライ ─────────────────────────────
class Fatal(Exception):
    """再試行しても無駄な失敗（400番台・入力不正など）"""


RETRYABLE_HINTS = ("429", "500", "502", "503", "504", "timeout", "timed out",
                   "connection", "overloaded", "rate_limit")


def is_retryable(err: Exception) -> bool:
    if isinstance(err, Fatal):
        return False
    s = f"{type(err).__name__} {err}".lower()
    return any(h in s for h in RETRYABLE_HINTS)


def retry(fn, attempts=5, base=1.0, cap=60.0, job="", on_retry=None):
    """指数バックオフ + ジッター。ジッターは同時に落ちた処理が同時に殺到するのを防ぐ。"""
    last = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if not is_retryable(e) or i == attempts:
                break
            delay = min(cap, base * (2 ** (i - 1)))
            delay = delay * (0.5 + random.random())   # フルジッター
            if on_retry:
                on_retry(i, delay, e)
            else:
                print(f"    ↻ {job} 再試行 {i}/{attempts-1} ({delay:.1f}秒待機): {str(e)[:80]}")
            time.sleep(delay)
    raise last


# ── 3. チェックポイント ─────────────────────
class Checkpoint:
    """処理済みを刻んで、再実行時に続きから走らせる。"""

    def __init__(self, con, job):
        self.con, self.job = con, job
        con.executescript(SCHEMA)
        con.commit()

    def _done_ids(self):
        return {r[0] for r in self.con.execute(
            "SELECT item_id FROM checkpoints WHERE job=? AND status='done'", (self.job,))}

    def remaining(self, all_ids, include_failed=True):
        """未処理のIDを返す。include_failed=Falseで失敗済みも除外（諦める運用）"""
        done = self._done_ids()
        if not include_failed:
            done |= {r[0] for r in self.con.execute(
                "SELECT item_id FROM checkpoints WHERE job=? AND status='failed' AND attempts>=3",
                (self.job,))}
        rest = [i for i in all_ids if str(i) not in done]
        skipped = len(all_ids) - len(rest)
        if skipped:
            print(f"  再開: {skipped}件は処理済みのためスキップ / 残り {len(rest)}件")
        return rest

    def done(self, item_id):
        self.con.execute("""INSERT INTO checkpoints (job,item_id,status,updated_at)
            VALUES (?,?,'done',?)
            ON CONFLICT(job,item_id) DO UPDATE SET status='done', error=NULL, updated_at=excluded.updated_at""",
            (self.job, str(item_id), datetime.now().isoformat(timespec="seconds")))
        self.con.commit()

    def failed(self, item_id, error):
        self.con.execute("""INSERT INTO checkpoints (job,item_id,status,attempts,error,updated_at)
            VALUES (?,?,'failed',1,?,?)
            ON CONFLICT(job,item_id) DO UPDATE SET
              status='failed', attempts=attempts+1, error=excluded.error, updated_at=excluded.updated_at""",
            (self.job, str(item_id), str(error)[:1000],
             datetime.now().isoformat(timespec="seconds")))
        self.con.commit()

    def summary(self):
        rows = self.con.execute(
            "SELECT status, COUNT(*) FROM checkpoints WHERE job=? GROUP BY status", (self.job,)).fetchall()
        return dict(rows)

    def retry_failed(self, max_attempts=3):
        """失敗分だけをもう一度対象に戻す"""
        ids = [r[0] for r in self.con.execute(
            "SELECT item_id FROM checkpoints WHERE job=? AND status='failed' AND attempts<?",
            (self.job, max_attempts))]
        return ids


# ── 4. 冪等キー ─────────────────────────────
class Idempotency:
    """同じ送信を二度実行させない。二重送信は一発で信用を失うので、
    ネットワークが不安定な環境ほどここが効く。"""

    def __init__(self, con):
        self.con = con
        con.executescript(SCHEMA)
        con.commit()

    @staticmethod
    def key(*parts):
        return ":".join(str(p) for p in parts)

    def seen(self, key):
        r = self.con.execute("SELECT result FROM idempotency WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r and r[0] else (r is not None and {})

    def record(self, key, result=None):
        self.con.execute("""INSERT OR IGNORE INTO idempotency (key, result, created_at)
            VALUES (?,?,?)""",
            (key, json.dumps(result, ensure_ascii=False) if result else None,
             datetime.now().isoformat(timespec="seconds")))
        self.con.commit()

    def run_once(self, key, fn):
        """既に実行済みならスキップして記録済みの結果を返す"""
        prev = self.seen(key)
        if prev is not False and prev != {} or self.con.execute(
                "SELECT 1 FROM idempotency WHERE key=?", (key,)).fetchone():
            return prev, True   # (結果, スキップしたか)
        result = fn()
        self.record(key, result)
        return result, False


# ── 送信APIごとのレート上限プリセット ──────────
LIMITS = {
    "anthropic":  dict(per_minute=50),    # tier次第。実運用値に合わせる
    "sendgrid":   dict(per_minute=600),
    "fax_api":    dict(per_minute=30),    # FAXは物理的に遅い
    "sms":        dict(per_minute=120),
}


def limiter_for(service):
    return RateLimiter(**LIMITS.get(service, dict(per_minute=60)))
