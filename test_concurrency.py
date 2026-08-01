"""
test_concurrency.py — 同時実行の耐性テスト
WAL+busy_timeoutが実際に効いているかを、並列書き込みで確認する。
「設定したつもり」で本番の並列処理が壊れるのを防ぐ。
"""
import sqlite3, threading, time
import db, resilience as R

def worker(n, results):
    con = db.connect()
    ok = 0
    for i in range(30):
        try:
            with db.write_lock(con):
                con.execute("INSERT INTO run_log (step,status,detail,started_at,finished_at) "
                            "VALUES (?,?,?,?,?)", (f"w{n}", "ok", str(i), "t", "t"))
            ok += 1
        except Exception as e:
            results.append(f"w{n}: {e}")
    results.append(f"w{n}: {ok}/30 成功")

con = db.connect(); db.migrate(con)
con.execute("DELETE FROM run_log WHERE step LIKE 'w%'"); con.commit()

print("── 並列書き込み (8スレッド × 30件) ──")
res, ts = [], []
t0 = time.time()
for n in range(8):
    t = threading.Thread(target=worker, args=(n, res)); t.start(); ts.append(t)
for t in ts: t.join()
for r in res: print("  " + r)
total = con.execute("SELECT COUNT(*) FROM run_log WHERE step LIKE 'w%'").fetchone()[0]
print(f"  書き込み総数 {total}/240  ({time.time()-t0:.2f}秒)")
assert total == 240, "書き込みが欠落している"
print("  ✓ 欠落なし")

print("\n── レートリミッター ──")
rl = R.RateLimiter(per_minute=600, burst=5)
t0 = time.time()
for _ in range(20): rl.acquire()
el = time.time() - t0
print(f"  20回取得: {el:.2f}秒 (理論最短 {(20-5)/10:.2f}秒)")
assert el >= 1.0, "レート制御が効いていない"
print("  ✓ 上限を守っている")

print("\n── リトライ ──")
calls = {"n": 0}
def flaky():
    calls["n"] += 1
    if calls["n"] < 3: raise RuntimeError("429 rate_limit")
    return "ok"
assert R.retry(flaky, base=0.05, job="test") == "ok" and calls["n"] == 3
print(f"  ✓ 一時的な失敗から復帰 ({calls['n']}回目で成功)")

def fatal():
    raise R.Fatal("400 invalid input")
try:
    R.retry(fatal, attempts=5, base=0.01, job="test"); print("  ✗ 諦めていない")
except R.Fatal:
    print("  ✓ 再試行しても無駄な失敗は即諦める")

print("\n── チェックポイント（再開） ──")
ck = R.Checkpoint(con, "t")   # ここでテーブルが作られる
con.execute("DELETE FROM checkpoints WHERE job='t'"); con.commit()
ids = list(range(10))
for i in ck.remaining(ids)[:6]: ck.done(i)
rest = ck.remaining(ids)
print(f"  6件処理後の残り: {len(rest)}件 {rest}")
assert rest == [6,7,8,9]
print("  ✓ 続きから再開できる")

print("\n── 冪等キー（二重送信防止） ──")
idem = R.Idempotency(con)
con.execute("DELETE FROM idempotency"); con.commit()
sent = {"n": 0}
def send():
    sent["n"] += 1; return {"id": "msg_1"}
k = idem.key("send", "campaign1", "company27", "step2")
idem.run_once(k, send); idem.run_once(k, send); idem.run_once(k, send)
print(f"  3回実行して実送信 {sent['n']}回")
assert sent["n"] == 1
print("  ✓ 二重送信しない")

con.execute("DELETE FROM run_log WHERE step LIKE 'w%'"); con.commit()
print("\n全項目パス")
