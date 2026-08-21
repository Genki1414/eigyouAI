"""
config.py — 全設定の単一情報源
各スクリプトに散っていた閾値・単価・文言をここに集約する。
本番で調整するのはこのファイルだけ、という状態を保つこと。
"""
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "out" / "companies.db"
OUT_DIR = BASE / "out"

# ── 対象業種 ──────────────────────────────
TARGET_TRADES = {"とび": "tobi", "土工": "tobi", "塗装": "tosou", "解体": "kaitai"}

# ── スコアリング V1 ────────────────────────
SCORE_THRESHOLDS = {"S": 75, "A": 60, "B": 45}   # 未満はC
EMP_SWEET_SPOT = (5, 50)                          # 従業員数のスイートスポット

# ── チャネル ──────────────────────────────
UNIT_COST_YEN = {"FAX": 12, "郵送DM": 95, "メール": 1, "SMS": 8, "架電": 180}
# 初回の推奨チャネル判定に使う（scoring.recommend_channel）
NEXT_CHANNEL = {
    "メール": ["SMS", "FAX"], "FAX": ["メール", "郵送DM"],
    "郵送DM": ["メール", "FAX"], "SMS": ["メール", "FAX"],
}

# ── シーケンス ────────────────────────────
SEQUENCE_DAYS = {1: 0, 2: 14, 3: 35}
MAX_STEP = 3
STEP_DECAY = {1: 1.0, 2: 0.62, 3: 0.38}

# ── 休眠 ─────────────────────────────────
COOLDOWN_DAYS = 180
CYCLE_OFFER = {
    1: ("無料ツール配布", "図面を送るだけで部材と数量が出るツールを無料開放"),
    2: ("積算代行", "図面を1枚お預かりして、こちらで積算した結果をお返しする"),
    3: ("地域事例訪問", "同じ地域の足場会社の導入結果を持って、10分だけ説明に伺う"),
}

# ── 経済性の前提 ──────────────────────────
LTV_MONTHS = 24            # LTV算定に使う継続月数（IMに明記される前提値）
PRICE_TIERS = [9800, 14800, 19800]

# ── AI ───────────────────────────────────
MODEL = "claude-sonnet-4-6"
ENRICH_SLEEP_SEC = 0.5
COMPOSE_SLEEP_SEC = 0.3

# ── コンプライアンス ──────────────────────
# 特定電子メール法: 送信者情報の明記と、受信拒否の意思表示を受ける窓口が必須。
SENDER_INFO = {
    "name": "AshiBase（足場ベース）",
    "address": "（本番: 登記上の住所を記載）",
    "email": "info@ashibase.jp",
    "optout_url": "https://ashibase.jp/optout",
}
# 1社あたりの生涯接触上限。これを超えたら以後どの巡目でも送らない。
MAX_LIFETIME_TOUCHES = 6
# 同一社への最短再接触間隔（日）
MIN_TOUCH_INTERVAL_DAYS = 10

# ── フォーム自動送信のペーシング(β版) ─────────
# 相手サイトへの負荷・bot判定回避・不具合時の被害抑制のため、
# 件数の上限は保守的な値から始める。実績を見てから引き上げる想定。
FORM_MAX_PER_RUN = 50              # 1回の実行(cron/API呼び出し1回)あたりの上限
FORM_MAX_PER_HOUR = 20             # 直近1時間の試行数上限(成否問わずカウント)
FORM_MAX_PER_DAY = 100             # 直近24時間の試行数上限
FORM_MAX_PER_TENANT_PER_DAY = 100  # テナントごとの直近24時間の試行数上限
