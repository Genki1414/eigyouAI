"""
learn.py — スコアV2: 実測反応をロジスティック回帰に学習させる
V1(手書きルール4軸)は「勘の言語化」でしかない。実際に送って反応が返ってきた時点で、
勘を実測に置き換えられる。ここがこの事業の堀になる部分:
接触するほどスコアが賢くなり、後発が同じ精度に追いつくには同じ量の接触が必要になる。

出力:
  - out/model_v2.json   … 学習済み係数(本番でscoring.pyが読む)
  - companies.score_v2  … 各社の予測反応確率(0-100)
  - V1 vs V2 のリフト検証（上位20%に反応が何倍集まるか）

使い方: python3 learn.py
"""
import json, sqlite3
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.metrics import roc_auc_score

import db as D

DB = Path(__file__).parent / "out" / "companies.db"
OUT = Path(__file__).parent / "out" / "model_v2.json"

# 学習に使う特徴量。ここに「送信側の条件(チャネル・文面型)」も入れることで、
# 「この会社にはどのチャネルで何型を送るべきか」まで一体で学習できる。
FEATURES = [
    "has_website", "website_quality", "hiring_now", "log_emp", "log_capital",
    "prime_ratio", "is_tobi", "is_kaitai", "license_seq_pct",
    "ch_mail", "ch_fax", "ch_dm", "va_A", "va_B", "va_C",
]

SQL = """
SELECT t.responded AS y,
       COALESCE(c.has_website,0)      AS has_website,
       COALESCE(c.website_quality,0)  AS website_quality,
       COALESCE(c.hiring_now,0)       AS hiring_now,
       COALESCE(c.est_employees,5)    AS emp,
       COALESCE(c.capital,3000)       AS capital,
       COALESCE(c.prime_ratio,0.3)    AS prime_ratio,
       COALESCE(c.trades,'')          AS trades,
       t.channel, t.variant, c.id AS cid, c.score AS score_v1
FROM touches t JOIN companies c ON c.id = t.company_id
WHERE t.delivered = 1
"""


def featurize(r):
    trades = r["trades"].split(",")
    return [
        r["has_website"], r["website_quality"], r["hiring_now"],
        np.log1p(r["emp"]), np.log1p(r["capital"] / 1000),
        r["prime_ratio"],
        1 if "tobi" in trades else 0, 1 if "kaitai" in trades else 0,
        r["license_seq_pct"],
        1 if r["channel"] == "メール" else 0,
        1 if r["channel"] == "FAX" else 0,
        1 if r["channel"] == "郵送DM" else 0,
        1 if r["variant"] == "A" else 0,
        1 if r["variant"] == "B" else 0,
        1 if r["variant"] == "C" else 0,
    ]

def lift_at_top(y, s, frac=0.2):
    """上位frac%に反応がベースの何倍集まるか = 営業リストとしての実用価値"""
    n = max(1, int(len(y) * frac))
    idx = np.argsort(-np.asarray(s))[:n]
    top_rate = np.asarray(y)[idx].mean()
    base = np.asarray(y).mean()
    return round(top_rate / base, 2) if base else 0.0

def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(SQL).fetchall()
    if len(rows) < 100:
        print(f"学習データ不足({len(rows)}件)。まず接触数を増やしてください。")
        return

    seq_pct = D.compute_license_seq_pct(con)
    rows = [dict(r, license_seq_pct=seq_pct.get(r["cid"], 0.5)) for r in rows]

    X = np.array([featurize(r) for r in rows], dtype=float)
    y = np.array([r["y"] for r in rows], dtype=int)
    v1 = np.array([r["score_v1"] for r in rows], dtype=float)
    print(f"学習データ: {len(y)}件 / 反応 {y.sum()}件 ({y.mean()*100:.2f}%)")

    if y.sum() < 20:
        print("反応数が少なすぎます(20件未満)。係数が不安定になるため参考値として出力します。")

    model = LogisticRegression(max_iter=2000, class_weight="balanced", C=0.5)
    # 交差検証で汎化性能を測る（学習データでの当たりは意味がない）
    cv = StratifiedKFold(5, shuffle=True, random_state=0)
    p_cv = cross_val_predict(model, X, y, cv=cv, method="predict_proba")[:, 1]
    auc_v2 = roc_auc_score(y, p_cv)
    auc_v1 = roc_auc_score(y, v1)
    print(f"\nAUC   V1(手書きルール) {auc_v1:.3f}  →  V2(学習) {auc_v2:.3f}")
    print(f"上位20%リフト  V1 {lift_at_top(y, v1)}倍  →  V2 {lift_at_top(y, p_cv)}倍")

    # ── 昇格ゲート ──
    # 学習したモデルが必ずV1より良いとは限らない。反応数が少ないと係数が不安定になり、
    # 交差検証でV1を下回ることがある（実際にこのテストで検出された）。
    # 「学習したから使う」のではなく「V1を上回った時だけ使う」。
    lift_v2, lift_v1 = lift_at_top(y, p_cv), lift_at_top(y, v1)
    promote = (auc_v2 > auc_v1) and (lift_v2 >= lift_v1) and y.sum() >= 30
    if not promote:
        reasons = []
        if auc_v2 <= auc_v1:
            reasons.append(f"AUCがV1以下({auc_v2:.3f} ≦ {auc_v1:.3f})")
        if lift_v2 < lift_v1:
            reasons.append(f"リフトがV1未満({lift_v2} < {lift_v1})")
        if y.sum() < 30:
            reasons.append(f"反応数不足({int(y.sum())}件 < 30件)")
        print(f"""
  ⚠ V2を採用しません: {' / '.join(reasons)}
    V1のスコアを維持します。接触数を増やしてから再学習してください。
    （学習結果は model_v2.json に参考値として保存されますが、
      companies.score_v2 は更新しないためリストの並びは変わりません）""")

    model.fit(X, y)
    coef = dict(zip(FEATURES, model.coef_[0].round(4).tolist()))
    OUT.write_text(json.dumps(
        {"features": FEATURES, "coef": coef, "intercept": round(float(model.intercept_[0]), 4),
         "auc_v1": round(auc_v1, 3), "auc_v2": round(auc_v2, 3),
         "lift_v1": lift_v1, "lift_v2": lift_v2,
         "n_samples": int(len(y)), "n_positive": int(y.sum()),
         "promoted": bool(promote),
         "active_model": "v2" if promote else "v1"},
        ensure_ascii=False, indent=1))

    print("\n反応に効いている要因 (係数順):")
    for k, v in sorted(coef.items(), key=lambda x: -abs(x[1]))[:8]:
        arrow = "↑" if v > 0 else "↓"
        print(f"  {arrow} {k:<16} {v:+.3f}")

    if not promote:
        print(f"\n参考値を保存 → {OUT}（採用: V1）")
        return

    # 全社にV2スコアを付与（未接触の会社にも予測を広げるのが本題）
    con.execute("ALTER TABLE companies ADD COLUMN score_v2 REAL" if not any(
        c[1] == "score_v2" for c in con.execute("PRAGMA table_info(companies)")) else "SELECT 1")
    allrows = con.execute("""SELECT id, COALESCE(has_website,0) has_website,
        COALESCE(website_quality,0) website_quality, COALESCE(hiring_now,0) hiring_now,
        COALESCE(est_employees,5) emp, COALESCE(capital,3000) capital,
        COALESCE(prime_ratio,0.3) prime_ratio,
        COALESCE(trades,'') trades
        FROM companies""").fetchall()
    # 最良チャネル×最良文面で送った場合の期待反応確率を各社のV2スコアとする
    best_ch, best_va = "メール", "A"
    for r in allrows:
        d = dict(r); d["channel"] = best_ch; d["variant"] = best_va
        d["license_seq_pct"] = seq_pct.get(r["id"], 0.5)
        p = model.predict_proba(np.array([featurize(d)], dtype=float))[0][1]
        con.execute("UPDATE companies SET score_v2=? WHERE id=?", (round(p * 100, 2), r["id"]))
    con.commit()
    print(f"\n全{len(allrows)}社にV2スコアを付与 → {OUT}")

if __name__ == "__main__":
    main()
