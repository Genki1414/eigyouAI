"""
enrich_review.py — エンリッチメント結果の検品用レポート生成
enrich.py で処理した会社を人が目視できる形にまとめる。判断は人間が行うため、
ここでは集計と一覧の提示のみを行い、しきい値判定やプロンプト修正は行わない。

使い方:
  python3 enrich_review.py [--pref 東京都]   → out/enrich_review.html を生成
"""
import argparse, json
from pathlib import Path

import db

OUT = Path(__file__).parent / "out" / "enrich_review.html"

EMP_BINS = [(0, 5), (5, 10), (10, 25), (25, 50), (50, 100), (100, 300), (300, 1000), (1000, None)]
PRIME_BINS = [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]


def histogram(rows, field, bins):
    out = []
    for lo, hi in bins:
        c = sum(1 for r in rows if r[field] is not None and lo <= r[field] < (hi if hi is not None else float("inf")))
        label = f"{lo}-{hi}" if hi is not None else f"{lo}+"
        out.append({"label": label, "count": c})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pref", default=None, help="checkpointのjob絞込に使う都道府県(enrich.pyの--prefと合わせる)")
    args = ap.parse_args()

    con = db.connect()
    rows = [dict(r) for r in con.execute("""
        SELECT id, name, pref, city, address, trades, capital,
               has_website, website_url, website_quality, hiring_now, hiring_source,
               est_employees, is_target_business, prime_ratio, enrich_note, enriched_at
        FROM companies WHERE enriched_at IS NOT NULL AND dedup_of IS NULL
        ORDER BY capital DESC
    """).fetchall()]
    # 資本金300万〜1億円が本来のターゲット層。それ以外(巨大企業パイロット分)は参考値として区別する。
    for r in rows:
        r["sample_group"] = "sample" if r["capital"] is not None and 3000 <= r["capital"] <= 100000 else "large_cap"

    job = f"enrich{':' + args.pref if args.pref else ''}"
    fails = [dict(r) for r in con.execute(
        "SELECT item_id, error, attempts FROM checkpoints WHERE job=? AND status='failed'", (job,)).fetchall()]

    n = len(rows)
    fields = ["has_website", "website_url", "website_quality", "hiring_now", "hiring_source",
              "est_employees", "is_target_business", "prime_ratio", "enrich_note"]
    null_rates = [{"field": f, "null": sum(1 for r in rows if r[f] is None),
                   "pct": round(sum(1 for r in rows if r[f] is None) / n * 100, 1) if n else 0}
                  for f in fields]

    contradictions = {
        "has_website1_no_url": [r for r in rows if r["has_website"] == 1 and not r["website_url"]],
        "has_website0_has_url": [r for r in rows if r["has_website"] == 0 and r["website_url"]],
        "has_website0_quality_gt0": [r for r in rows if r["has_website"] == 0 and (r["website_quality"] or 0) > 0],
        "hiring1_emp0": [r for r in rows if r["hiring_now"] == 1 and (r["est_employees"] or 0) == 0],
        # hiring_source/is_target_businessは今回追加した項目のため、旧プロンプトで
        # エンリッチ済みの会社(is_target_business is None)はチェック対象から除く
        "hiring1_no_source": [r for r in rows if r["hiring_now"] == 1 and not r["hiring_source"]
                               and r["is_target_business"] is not None],
    }

    id_to_name = {str(r["id"]): r["name"] for r in con.execute("SELECT id, name FROM companies").fetchall()}
    fail_reasons = {}
    for f in fails:
        err = f["error"] or ""
        if "credit balance" in err:
            key = "credit_exhausted"
        elif "substring not found" in err:
            key = "json_parse_error"
        else:
            key = "other"
        fail_reasons.setdefault(key, []).append(f)

    n_sample_group = sum(1 for r in rows if r["sample_group"] == "sample")
    stats = {
        "n_success": n,
        "n_failed": len(fails),
        "n_sample_group": n_sample_group,
        "n_large_cap_group": n - n_sample_group,
        "has_website_dist": [{"label": "あり", "count": sum(1 for r in rows if r["has_website"] == 1)},
                              {"label": "なし", "count": sum(1 for r in rows if r["has_website"] == 0)}],
        "website_quality_dist": [{"label": str(k), "count": sum(1 for r in rows if r["website_quality"] == k)}
                                  for k in (0, 1, 2, 3)],
        "hiring_now_dist": [{"label": "出稿中", "count": sum(1 for r in rows if r["hiring_now"] == 1)},
                             {"label": "なし", "count": sum(1 for r in rows if r["hiring_now"] == 0)}],
        "target_dist": [{"label": "施工実態あり", "count": sum(1 for r in rows if r["is_target_business"] == 1)},
                         {"label": "施工実態なし", "count": sum(1 for r in rows if r["is_target_business"] == 0)},
                         {"label": "未判定(旧データ)", "count": sum(1 for r in rows if r["is_target_business"] is None)}],
        "emp_hist": histogram(rows, "est_employees", EMP_BINS),
        "prime_hist": histogram(rows, "prime_ratio", PRIME_BINS),
        "null_rates": null_rates,
        "contradictions": {k: {"count": len(v), "examples": [r["name"] for r in v[:5]]}
                            for k, v in contradictions.items()},
        "fail_reasons": {k: {"count": len(v),
                              "examples": [id_to_name.get(f["item_id"], f["item_id"]) for f in v[:8]]}
                         for k, v in fail_reasons.items()},
    }

    html = TEMPLATE.replace("__DATA__", json.dumps({"rows": rows, "stats": stats}, ensure_ascii=False))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"検品レポート: 成功{n}件 / 失敗{len(fails)}件 → {OUT}")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>エンリッチメント検品レポート</title>
<style>
:root{
  color-scheme: light;
  --surface-1:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --border:rgba(11,11,11,0.10);
  --blue:#2a78d6; --orange:#eb6834; --aqua:#1baf7a; --yellow:#eda100;
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --mono:'IBM Plex Mono',ui-monospace,monospace;
}
@media (prefers-color-scheme: dark){
  :root:where(:not([data-theme="light"])){
    color-scheme: dark;
    --surface-1:#1a1a19; --page:#0d0d0d; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --border:rgba(255,255,255,0.10);
    --blue:#3987e5; --orange:#d95926; --aqua:#199e70; --yellow:#c98500;
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --surface-1:#1a1a19; --page:#0d0d0d; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --border:rgba(255,255,255,0.10);
  --blue:#3987e5; --orange:#d95926; --aqua:#199e70; --yellow:#c98500;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",sans-serif;font-size:14px;line-height:1.6}
header{padding:24px;max-width:1200px;margin:0 auto}
h1{font-size:20px;font-weight:800;margin:0 0 4px}
.sub{color:var(--muted);font-size:12px;font-family:var(--mono)}
main{max-width:1200px;margin:0 auto;padding:0 24px 60px}
h2{font-size:15px;font-weight:700;margin:32px 0 12px;display:flex;align-items:center;gap:8px}
h2::before{content:"";width:4px;height:14px;background:var(--blue)}
h2 span{font-size:11px;color:var(--muted);font-weight:500;font-family:var(--mono)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:12px 14px}
.card b{display:block;font-size:22px;font-family:var(--mono);font-weight:600}
.card span{font-size:11px;color:var(--muted)}
.card.critical b{color:var(--critical)} .card.good b{color:var(--good)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.panel{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:14px}
.panel h3{font-size:12px;color:var(--muted);margin:0 0 10px;font-weight:700;letter-spacing:.02em}
.bar-row{display:grid;grid-template-columns:70px 1fr 40px;gap:8px;align-items:center;margin-bottom:6px;font-size:12px}
.bar-row label{color:var(--ink2);text-align:right}
.bar-track{background:var(--grid);border-radius:3px;height:16px;overflow:hidden}
.bar-fill{height:100%;background:var(--blue);border-radius:3px 0 0 3px}
.bar-row .n{font-family:var(--mono);color:var(--ink2)}
table{width:100%;border-collapse:collapse;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;overflow:hidden;font-size:12.5px}
th{background:var(--page);text-align:left;padding:8px 10px;font-size:10.5px;color:var(--muted);letter-spacing:.04em;position:sticky;top:0}
td{padding:7px 10px;border-top:1px solid var(--grid);vertical-align:top}
td.num{font-family:var(--mono);text-align:right;white-space:nowrap}
.pill{display:inline-block;background:var(--page);border:1px solid var(--border);border-radius:4px;padding:1px 6px;font-size:10.5px;font-family:var(--mono)}
.pill.on{background:var(--good);color:#fff;border-color:var(--good)}
.pill.off{background:var(--grid);color:var(--muted)}
.note{max-width:280px;color:var(--ink2);font-size:12px}
.wrap{max-height:640px;overflow:auto;border-radius:8px}
.wrap table{border-radius:0}
a{color:var(--blue)}
.reasons{display:flex;flex-direction:column;gap:8px}
.reason{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;background:var(--page);border-radius:6px;font-size:12.5px}
.reason b{font-family:var(--mono)}
.reason.credit{border-left:3px solid var(--critical)}
.reason.json{border-left:3px solid var(--warning)}
.reason.other{border-left:3px solid var(--muted)}
.exlist{font-size:11px;color:var(--muted);margin-top:4px}
.check{display:flex;justify-content:space-between;padding:6px 0;border-top:1px dashed var(--grid);font-size:12.5px}
.check:first-child{border-top:none}
.check b{font-family:var(--mono)}
.check.ok b{color:var(--good)} .check.ng b{color:var(--critical)}
footer{text-align:center;color:var(--muted);font-size:11px;padding:24px}
</style>
</head>
<body>
<header>
  <h1>エンリッチメント検品レポート</h1>
  <div class="sub" id="sub"></div>
</header>
<main>
  <div class="cards" id="cards"></div>
  <div class="sub" id="groupsub" style="margin-top:8px"></div>

  <h2>各項目のNULL率 <span>DATA COMPLETENESS</span></h2>
  <div class="panel"><div id="nullrates"></div></div>

  <h2>分布 <span>DISTRIBUTIONS</span></h2>
  <div class="grid2">
    <div class="panel"><h3>従業員数推定 (est_employees)</h3><div id="empHist"></div></div>
    <div class="panel"><h3>施工実態 (is_target_business)</h3><div id="targetDist"></div></div>
    <div class="panel"><h3>元請比率 (prime_ratio)</h3><div id="primeHist"></div></div>
    <div class="panel"><h3>HP品質 (website_quality) / has_website / hiring_now</h3><div id="qualDist"></div></div>
  </div>

  <h2>矛盾チェック <span>CONSISTENCY</span></h2>
  <div class="panel" id="contradictions"></div>

  <h2>失敗の内訳 <span>FAILURES</span></h2>
  <div class="panel"><div class="reasons" id="reasons"></div></div>

  <h2>個社一覧 <span id="rowcount"></span></h2>
  <div class="wrap"><table id="tbl"></table></div>
</main>
<footer>enrich_review.py で生成 / 判断はここでは行わない（検品専用）</footer>
<script>
const D = __DATA__;
const rows = D.rows, S = D.stats;

document.getElementById("sub").textContent =
  `成功 ${S.n_success}件 / 失敗 ${S.n_failed}件 (試行 ${S.n_success + S.n_failed}件)`;

document.getElementById("cards").innerHTML = `
  <div class="card"><b>${S.n_success}</b><span>成功</span></div>
  <div class="card critical"><b>${S.n_failed}</b><span>失敗</span></div>
  <div class="card"><b>${S.has_website_dist[0].count}</b><span>HPあり</span></div>
  <div class="card"><b>${S.hiring_now_dist[0].count}</b><span>求人出稿中</span></div>
  <div class="card"><b>${rows.length ? (rows.reduce((a,r)=>a+(r.est_employees||0),0)/rows.length).toFixed(0) : "-"}</b><span>平均従業員数推定</span></div>`;
document.getElementById("groupsub").textContent =
  `内訳: 資本金300万〜1億円のサンプル層 ${S.n_sample_group}社 / 巨大企業パイロット層(参考値) ${S.n_large_cap_group}社`;

function barList(el, items, max) {
  max = max || Math.max(...items.map(i=>i.count), 1);
  document.getElementById(el).innerHTML = items.map(i => `
    <div class="bar-row"><label>${i.label}</label>
      <div class="bar-track"><div class="bar-fill" style="width:${i.count/max*100}%"></div></div>
      <div class="n">${i.count}</div></div>`).join("");
}

document.getElementById("nullrates").innerHTML = S.null_rates.map(r => `
  <div class="check ${r.null>0?'ng':'ok'}"><span>${r.field}</span><b>${r.null}/${S.n_success} (${r.pct}%)</b></div>`).join("");

barList("empHist", S.emp_hist);
barList("targetDist", S.target_dist);
barList("primeHist", S.prime_hist);
document.getElementById("qualDist").innerHTML =
  `<div style="font-size:11px;color:var(--muted);margin-bottom:4px">website_quality (0-3)</div>` +
  S.website_quality_dist.map(i=>`<div class="bar-row"><label>${i.label}</label>
    <div class="bar-track"><div class="bar-fill" style="width:${i.count/Math.max(...S.website_quality_dist.map(x=>x.count),1)*100}%"></div></div>
    <div class="n">${i.count}</div></div>`).join("") +
  `<div style="font-size:11px;color:var(--muted);margin:8px 0 4px">has_website</div>` +
  S.has_website_dist.map(i=>`<div class="bar-row"><label>${i.label}</label>
    <div class="bar-track"><div class="bar-fill" style="width:${i.count/S.n_success*100}%;background:var(--aqua)"></div></div>
    <div class="n">${i.count}</div></div>`).join("") +
  `<div style="font-size:11px;color:var(--muted);margin:8px 0 4px">hiring_now</div>` +
  S.hiring_now_dist.map(i=>`<div class="bar-row"><label>${i.label}</label>
    <div class="bar-track"><div class="bar-fill" style="width:${i.count/S.n_success*100}%;background:var(--orange)"></div></div>
    <div class="n">${i.count}</div></div>`).join("");

const CLABEL = {
  has_website1_no_url: "has_website=1 なのに website_url が空",
  has_website0_has_url: "has_website=0 なのに website_url がある",
  has_website0_quality_gt0: "has_website=0 なのに website_quality>0",
  hiring1_emp0: "hiring_now=1 なのに est_employees=0",
  hiring1_no_source: "hiring_now=1 なのに hiring_source が空(新項目のみ対象)",
};
document.getElementById("contradictions").innerHTML = Object.entries(S.contradictions).map(([k,v]) => `
  <div class="check ${v.count>0?'ng':'ok'}"><span>${CLABEL[k]}</span><b>${v.count}件</b></div>
  ${v.count>0 ? `<div class="exlist">例: ${v.examples.join(" / ")}</div>` : ""}`).join("");

const RLABEL = {credit_exhausted:"APIクレジット枯渇(400)", json_parse_error:"JSON抽出失敗(substring not found)", other:"その他"};
document.getElementById("reasons").innerHTML = Object.entries(S.fail_reasons).map(([k,v]) => `
  <div class="reason ${k==='credit_exhausted'?'credit':k==='json_parse_error'?'json':'other'}">
    <span>${RLABEL[k]||k}</span><b>${v.count}件</b></div>
  ${v.count>0 ? `<div class="exlist">company_id例: ${v.examples.join(", ")}</div>` : ""}`).join("");

document.getElementById("rowcount").textContent = `${rows.length}社`;
document.getElementById("tbl").innerHTML = `
  <tr><th>会社名</th><th>層</th><th>所在地</th><th>業種</th><th class="num">資本金(千円)</th>
      <th>HP</th><th>URL</th><th class="num">品質</th><th>求人</th><th>求人根拠</th>
      <th class="num">従業員数</th><th>施工実態</th><th class="num">元請比率</th><th>所見</th></tr>` +
  rows.map(r => `<tr>
    <td><b>${r.name}</b></td>
    <td><span class="pill">${r.sample_group === "sample" ? "サンプル層" : "巨大企業(参考)"}</span></td>
    <td>${(r.pref||"")+(r.city||"")}</td>
    <td>${(r.trades||"").split(",").join(" / ")}</td>
    <td class="num">${(r.capital||0).toLocaleString()}</td>
    <td><span class="pill ${r.has_website?'on':'off'}">${r.has_website?'あり':'なし'}</span></td>
    <td>${r.website_url ? `<a href="${r.website_url}" target="_blank" rel="noopener">${r.website_url.replace(/^https?:\/\//,'').slice(0,28)}</a>` : "-"}</td>
    <td class="num">${r.website_quality ?? "-"}</td>
    <td><span class="pill ${r.hiring_now?'on':'off'}">${r.hiring_now?'出稿中':'なし'}</span></td>
    <td class="note">${r.hiring_source || ""}</td>
    <td class="num">${r.est_employees ?? "-"}</td>
    <td>${r.is_target_business === null ? '<span class="pill">未判定</span>' :
          `<span class="pill ${r.is_target_business ? 'on':'off'}">${r.is_target_business ? 'あり':'なし'}</span>`}</td>
    <td class="num">${r.prime_ratio ?? "-"}</td>
    <td class="note">${r.enrich_note || ""}</td>
  </tr>`).join("");
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
