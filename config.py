"""
config.py — 全設定の単一情報源
各スクリプトに散っていた閾値・単価・文言をここに集約する。
本番で調整するのはこのファイルだけ、という状態を保つこと。
"""
import os
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "out" / "companies.db"
OUT_DIR = BASE / "out"

# クリック計測(MIKOMERUの「URLアクセスの記録」相当)のリダイレクトリンクに使う
# 公開URL。本番では実際の公開ドメインを環境変数で上書きすること
# (api.py LP_URLと同じ考え方)。
TRACK_BASE_URL = os.environ.get("TRACK_BASE_URL", "https://ashibase.jp")

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

# ── フォーム自動送信のペーシング ─────────────
# T29(2026-08-24)でテナント公平型に再設計。それまでは「全テナント合算で
# 1日100件」という単一の共有プールしかなく、契約社数が増えるほど1社あたりの
# 実質的な取り分が目減りする作りだった(極端な例: 100社が契約しても合計100件/日
# のまま=1社1件/日)。最低プランでも月4,000件(MIKOMERU最低ランクの水準)を
# 送れることを目標に、「グローバルなサーキットブレーカー」と「テナントごとの
# 公平な取り分」を分離した:
#   - FORM_MAX_PER_HOUR/DAY: 全テナント合算の上限。通常運用では到達しない
#     水準まで引き上げ、バグ・異常時の被害を止める最終防波堤として残す
#     (相手サイト群への負荷や送信元IPの評判悪化を、システム全体の暴走から守る)。
#   - FORM_MAX_PER_TENANT_PER_HOUR: テナント1社が短時間に固め打ちしないための
#     ペーシング(相手サイトへの礼儀・bot判定回避が目的。月間クォータの残りが
#     あっても、これを超える速さでは送らせない)。
#   - tenants.monthly_send_quota / daily_send_quota: プランに応じたテナント別の
#     クォータ(未設定=NULLの場合は下の_DEFAULT値を使う)。月間が契約プランの
#     実体、日次は月間クォータ内での使いすぎ防止のブレーキ。
FORM_MAX_PER_RUN = 50                        # 1回の実行(cron/API呼び出し1回)あたりの上限
FORM_MAX_PER_HOUR = 2000                     # 全テナント合算・直近1時間のサーキットブレーカー
FORM_MAX_PER_DAY = 20000                     # 全テナント合算・直近24時間のサーキットブレーカー
FORM_MAX_PER_TENANT_PER_HOUR = 50            # テナント1社・直近1時間のペーシング上限
FORM_MAX_PER_TENANT_PER_DAY_DEFAULT = 300    # tenants.daily_send_quota未設定時の既定値
FORM_MAX_PER_TENANT_PER_MONTH_DEFAULT = 4000  # tenants.monthly_send_quota未設定時の既定値
                                               # (=最低プランの想定送信数)

# senders.send_campaign()が1回の呼び出し内で同時に処理する件数(T41)。
# フォーム送信はPlaywrightでの実ブラウザ操作(1件あたり数秒〜十数秒)が
# ボトルネックのため、DBの読み書きではなくここが並列化の効果が出る箇所。
# 上げすぎると相手サイト群への同時アクセスが増え、bot判定やこのサーバーの
# メモリ(Chromiumプロセスを同時分だけ起動する)を圧迫するため小さめに抑える。
FORM_SEND_CONCURRENCY = 3

# ── 送信元IPの分散(プロキシ。T42) ─────────────
# T41でフォーム送信を並列化した結果、複数ワーカーが同じサーバーIPから
# 短時間に一斉アクセスする形になり、相手サイト側のWAF/bot判定に
# 引っかかりやすくなる懸念がある。form_navigator.py がPlaywrightで
# ブラウザを起動するたびに、このプールからプロキシを1つ選んで経由させる
# ことでアクセス元IPを分散できるようにする(実際のプロキシサービスの契約は
# インフラ側の判断のため、ここではコード側の受け皿のみ用意する)。
#
# FORM_PROXY_POOL環境変数にカンマ区切りで設定する:
#   FORM_PROXY_POOL="http://user1:pass1@proxy1.example.com:8080,http://proxy2.example.com:8080"
# 未設定(既定=空リスト)ならプロキシを使わず直接アクセスする(現状と同じ挙動、
# 後方互換)。
FORM_PROXY_POOL = [p.strip() for p in os.environ.get("FORM_PROXY_POOL", "").split(",") if p.strip()]

# ── 原価計測(1送信あたりのコスト把握。β版・概算値) ──
# 厳密なクラウド原価配賦ではなく、事業判断に使える推定値を出すのが目的。
# サーバー月額費用を実行時間で按分する(実行時間ベースの単純な比例配分)。
SERVER_MONTHLY_COST_YEN = 15000  # Hetzner等の月額実費。実績に合わせて更新する

def estimate_server_cost_yen(execution_seconds):
    """実行時間(秒)から、月額サーバー費用の按分としての推定原価を返す。"""
    if not execution_seconds:
        return 0.0
    seconds_per_month = 30 * 24 * 3600
    return SERVER_MONTHLY_COST_YEN * (execution_seconds / seconds_per_month)


# モデルごとのAPI単価(1トークンあたり円)。ハードコードで散らばらせず、ここ1箇所を
# 更新すれば全体に反映される構造にする。現状フォーム送信はAIを使っていないため
# 実際の呼び出し箇所は無いが、将来compose.py等をここに接続する前提で用意しておく。
AI_PRICING_YEN_PER_TOKEN = {
    # "claude-sonnet-5": {"input": 0.0045, "output": 0.0225},  # 例: $3/$15 per 1M tokens換算
}

def estimate_ai_cost_yen(model, tokens_input, tokens_output):
    price = AI_PRICING_YEN_PER_TOKEN.get(model)
    if not price or not (tokens_input or tokens_output):
        return 0.0
    return tokens_input * price["input"] + tokens_output * price["output"]


# ── バックアップ(backup.py。T36/T37) ──────────
BACKUP_DIR = OUT_DIR / "backups"
BACKUP_RETENTION_DAYS = 14   # これより古いバックアップファイルは自動削除する
# monitor.pyがこの時間を超えてバックアップが成功していないことを検知したらアラートする
# (毎日1回の実行前提で、1回分の遅延は許容しつつ2日連続の失敗は見逃さない設定)
BACKUP_STALE_HOURS = 30
# オフサイト複製(BACKUP_OFFSITE_TARGET設定時のみ有効)についても同様の考え方。
# ローカルより長めに取っているのは、rsync先が一時的に落ちていても2回失敗する
# までは静観したいため(ローカルのバックアップ自体は既に安全に取れているので、
# オフサイト側はローカルほど緊急性が高くない)。
BACKUP_OFFSITE_STALE_HOURS = 54
