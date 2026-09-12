"""
products.py — テナントの「商材」登録とAIによるリスト自動生成(T83)
テナントが売りたい商材(商品/サービス)を登録すると、AIが商材の説明文から
「どんな企業に提案すべきか」を判断し、target_lists.build_filter_sql()と同じ
フィルタ条件に変換して、フィルタ型と同じ仕組みでリストを自動生成する。

何社リストアップするか(count)はテナントが指定する。同じ商材で2回目以降リストを
作る際は、その商材向けに過去作成した全リスト(target_lists.product_id経由)の
メンバーを除外するため、「回を重ねるほど新しい会社だけが積み上がる」運用になる
(=同じ会社に同じ商材を二度提案しない)。

必要環境変数: ANTHROPIC_API_KEY。enrich.py/compose.pyと違いオフライン代替は
用意しない(商材ごとの対象判断はテンプレ文で代替できる性質のものではないため)。
未設定または分類に失敗した場合は、その場でエラーを返す(呼び出し側がやり直せる)。

api.pyの /api/tenant/products* エンドポイントがこのモジュールを呼ぶ。

【分類呼び出しをリクエスト処理内で同期的に行うことについて】
enrich.py/compose.pyは何百〜何千社分をAIに投げるためcron/CLIの一括処理に
しているが、ここは「商材登録1件につきAI呼び出し1回」の軽い処理(web検索なし、
effort=low)であり性質が異なる。api.pyのHTTPServerはスレッド化していないため
長時間のブロッキングは避けたいが、通常は数秒で完了する処理を非同期化する方が
過剰と判断し、client側タイムアウトと再試行回数を絞ることで最悪時間を抑える
方針にした(classify_targeting()参照)。
"""
import json
import os
from datetime import datetime

import config as C
import target_lists as TL

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant_products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,      -- AIへ渡す商材説明(誰にどう刺さるかの元ネタ)
  last_filters_json TEXT,         -- 直近のAI判断結果(監査用。リスト作成のたびに再判定して上書きする)
  created_at TEXT NOT NULL,
  deleted_at TEXT,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);
"""

MODEL = "claude-sonnet-5"

# プロンプトへ渡す業種名の一覧(config.TARGET_TRADESのキー側。コードへの変換はここで行う)
_TRADE_NAMES = sorted(set(C.TARGET_TRADES.keys()))

CLASSIFY_PROMPT = """あなたはBtoB営業のターゲティング担当です。次の商材(商品/サービス)の
説明を読み、どんな企業に提案すべきかを判断してください。

商材名: {name}
商材説明: {description}

判断は、次の絞り込み項目の値としてJSONのみで返してください(前置き・コードブロック禁止)。
どの項目も「わからなければ入れない(null/空配列)」でよい。無理に全項目を埋めようとせず、
商材の説明から自信を持って言える範囲だけ答えてください。ただしtradesは、この商材が
明確に一業種だけをターゲットにしているのでない限り、刺さる可能性のある業種を
広めに(数十件程度でもよい)拾ってください——狭く絞りすぎるとリストが作れなくなります。

{{
  "trades": ["下の業種名一覧から、この商材が刺さる業種名を選ぶ(日本語の業種名そのまま)"],
  "ranks": ["S","A","B","C"のうち、刺さる会社の格付けを0件以上選ぶ(S/Aほど規模・実績が大きい)"],
  "capital_max": 資本金の上限(円の整数)。小規模事業者向けの商材なら設定、大企業向けならnull,
  "hiring_now": true/false。人手不足に効く商材(採用支援・省人化等)ならtrue、それ以外はfalse,
  "has_website": true/false。Webサイトを持つ企業でないと使えない商材(HP掲載型サービス等)ならtrue,
  "reasoning": "この判断をした理由を80字以内の日本語で"
}}

業種名一覧: {trade_names}
"""


def _client():
    import anthropic  # pip install anthropic(本番のみ必要。requirements.txt参照)
    # timeout: このAPIサーバは単一スレッドのため、Anthropic側の障害時に
    # リクエストを無期限に塞がないよう明示的に短く切る(モジュール docstring参照)。
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=12.0)


def classify_targeting(name, description):
    """商材の説明からtarget_lists.build_filter_sql()と同じ形のfiltersを判断する。
    戻り値: (filters dict, reasoning文字列)。
    未知の業種名・不正な値はbuild_filter_sql()側のホワイトリストでも無視されるが、
    ここでも業種名→コード変換の際に一覧に無いものは自然に落ちる。"""
    import resilience as R

    client = _client()
    rl = R.limiter_for("anthropic")

    def _call():
        rl.acquire()
        msg = client.messages.create(
            model=MODEL, max_tokens=1000, output_config={"effort": "low"},
            messages=[{"role": "user", "content": CLASSIFY_PROMPT.format(
                name=name, description=description, trade_names="、".join(_TRADE_NAMES))}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text[text.index("{"): text.rindex("}") + 1])

    # attempts=2・cap短めで、生成の重い処理(enrich.py等)と違い最悪時間を抑える
    d = R.retry(_call, attempts=2, base=1.0, cap=4.0, job="products.classify_targeting")

    trades = [C.TARGET_TRADES[name_] for name_ in (d.get("trades") or []) if name_ in C.TARGET_TRADES]
    filters = {
        "trades": trades,
        "ranks": [r for r in (d.get("ranks") or []) if r in ("S", "A", "B", "C")],
    }
    if d.get("capital_max"):
        filters["capital_max"] = d["capital_max"]
    if d.get("hiring_now"):
        filters["hiring_now"] = True
    if d.get("has_website"):
        filters["has_website"] = True
    return filters, (d.get("reasoning") or "")


def create_product(con, tenant_id, name, description):
    now = datetime.now().isoformat(timespec="seconds")
    cur = con.execute("""INSERT INTO tenant_products (tenant_id,name,description,created_at)
        VALUES (?,?,?,?)""", (tenant_id, name, description, now))
    con.commit()
    return cur.lastrowid


def list_products(con, tenant_id):
    """商材ごとに、これまで(全リスト合計で)何社リストアップ済みかも合わせて返す
    (テナントが「もう出尽くしたか」の目安にできるように)。"""
    rows = con.execute("""SELECT p.*,
        (SELECT COUNT(DISTINCT m.company_id) FROM target_list_members m
         JOIN target_lists l ON l.id=m.list_id WHERE l.product_id=p.id) AS listed_count
        FROM tenant_products p WHERE p.tenant_id=? AND p.deleted_at IS NULL
        ORDER BY p.created_at DESC""", (tenant_id,)).fetchall()
    return [dict(r) for r in rows]


def get_product(con, tenant_id, product_id):
    row = con.execute("""SELECT * FROM tenant_products
        WHERE id=? AND tenant_id=? AND deleted_at IS NULL""", (product_id, tenant_id)).fetchone()
    return dict(row) if row else None


def build_list_for_product(con, tenant_id, product_id, count, list_name=None):
    """商材向けのリストを新規作成する。過去にこの商材向けに作った全リストの
    メンバーは除外し(=同じ会社を二度リストアップしない)、AIが判断した条件に
    一致する会社の中からスコア降順でcount件を選ぶ。"""
    product = get_product(con, tenant_id, product_id)
    if not product:
        return {"error": "指定された商材が見つかりません"}
    count = max(1, min(int(count), TL.MAX_LIST_SIZE))

    try:
        filters, reasoning = classify_targeting(product["name"], product["description"])
    except KeyError:
        return {"error": "AI判断が利用できません(ANTHROPIC_API_KEY未設定)"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"AI判断に失敗しました: {str(e)[:200]}"}

    where, params = TL.build_filter_sql(tenant_id, filters)
    where += """ AND id NOT IN (
        SELECT m.company_id FROM target_list_members m
        JOIN target_lists l ON l.id=m.list_id WHERE l.product_id=?)"""
    params = params + [product_id]

    ids = [r[0] for r in con.execute(
        f"""SELECT id FROM companies WHERE {where}
            ORDER BY COALESCE(score_v2, score, 0) DESC LIMIT ?""",
        params + [count]).fetchall()]

    now = datetime.now().isoformat(timespec="seconds")
    name = list_name or f"{product['name']}向けリスト({now[:10]})"
    cur = con.execute("""INSERT INTO target_lists
        (tenant_id,name,source,filter_json,company_count,product_id,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (tenant_id, name, "ai_product", json.dumps(filters, ensure_ascii=False), len(ids),
         product_id, now, now))
    list_id = cur.lastrowid
    con.executemany("""INSERT OR IGNORE INTO target_list_members
        (list_id, company_id, send_status, created_at, updated_at) VALUES (?,?,'PENDING',?,?)""",
        [(list_id, cid, now, now) for cid in ids])
    con.execute("UPDATE tenant_products SET last_filters_json=? WHERE id=?",
                (json.dumps(filters, ensure_ascii=False), product_id))
    con.commit()
    return {"list_id": list_id, "count": len(ids), "filters": filters, "reasoning": reasoning}
