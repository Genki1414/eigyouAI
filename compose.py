"""
compose.py — AIパーソナライズ文面生成
1社ごとに enrich_note（AIが読んだHP所見）を材料にして文面を出し分ける。
バリアントA/B/Cは「訴求軸そのもの」を変える。トーン違いではなくオファー構造の違いにする
ことで、A/Bテストの結果が「何が刺さるか」の知見になる。

  A: 実績称賛型   … 御社の施工実績を具体的に褒めて入る（王道・安全牌）
  B: 課題提起型   … 積算に時間を取られている前提で切り込む（強いが外すと嫌われる）
  C: 同業事例型   … 同じ地域・同じ規模の会社の導入結果を出す（社会的証明）

APIキーがなければテンプレートモードで動く（--offline）。

使い方:
  python3 compose.py --campaign 1 --limit 20
  python3 compose.py --campaign 1 --offline      # API不要のデモ生成
"""
import argparse, os, sqlite3, textwrap, time
from pathlib import Path

DB = Path(__file__).parent / "out" / "companies.db"
MODEL = "claude-sonnet-4-6"

# オファーは offers テーブルから読む（ハードコードしない）。
# このエンジンは自社の複数事業／販売先／譲渡先のオファーを差し替えて回すため。
def load_offer(con, offer_id):
    o = con.execute("""SELECT o.*, t.sender_name, t.optout_url FROM offers o
                       JOIN tenants t ON t.id=o.tenant_id WHERE o.id=?""", (offer_id,)).fetchone()
    if not o:
        raise SystemExit(f"オファー id={offer_id} が見つかりません。offers.py list で確認してください")
    price = "無料" if not o["price_yen"] else f"月額 {o['price_yen']:,}円"
    return o, f"""【オファー内容】
{o['offer_text']}
価格: {price}
（提供: {o['sender_name']}）"""

VARIANT_BRIEF = {
    "A": "実績称賛型。相手のHPで見つけた具体的な実績・こだわりに触れてから本題に入る。売り込み感を消す。",
    "B": "課題提起型。『積算に夜や休日を使っていませんか』という現場の痛点から入る。断定しすぎず問いかける。",
    "C": "同業事例型。同じ都道府県・同じ規模帯の足場会社が導入して積算時間が減った、という社会的証明から入る。",
}

CHANNEL_BRIEF = {
    "FAX": "FAX1枚。横書き400字以内。装飾なし。事務員が社長に回すことを想定し、冒頭3行で用件が分かるように。末尾に返信用のFAX番号記入欄への案内。",
    "郵送DM": "封書の挨拶状。500字以内。手紙の体裁（拝啓〜敬具は不要、やわらかい口語）。同封のQRコード案内で締める。",
    "メール": "件名＋本文300字以内。件名は開封される具体性を持たせる。1リンクのみ。",
    "SMS": "全角120字以内。1リンク。丁寧だが短く。",
}

PROMPT = """あなたは足場業界に精通したセールスライターです。以下の会社宛の営業文面を書いてください。

【宛先】
会社名: {name}
所在地: {pref}
業種: {trades}
規模: 従業員約{emp}名
AIリサーチ所見: {note}

{offer}

【チャネル要件】{channel_brief}
【訴求タイプ】{variant_brief}

【厳守事項】
- 足場屋の社長が読む文章。カタカナのIT用語・横文字は使わない（「DX」「ソリューション」等は禁止）
- 誇張・断定的な数値効果の約束をしない
- 敬語だが硬すぎない。現場の人が書いた温度感
- 出力は件名と本文のみ。解説や前置きは書かない

出力形式:
件名: （メール/FAXの場合のみ。それ以外は「-」）
本文:
（ここに本文）"""

# --- オフライン用テンプレート（API不要でパイプラインを通すため） ---
OFFLINE = {
("FAX","A"): ("-", """{name} 御中

突然のFAX失礼いたします。足場の積算ツールを作っております、AshiBaseと申します。

御社のホームページを拝見しました。{note_short}

弊社では、図面をお送りいただくと必要な部材と数量が自動で出るツールを、無料でお使いいただけるようにしております。費用は一切かかりません。

お試しいただける場合は、このFAXに社名とご連絡先をご記入のうえ返信いただければ、ご案内をお送りいたします。

AshiBase（足場ベース）
FAX: 03-XXXX-XXXX"""),
("FAX","B"): ("-", """{name} 御中

足場の積算に、夜や日曜の時間を使われていませんか。

図面を送るだけで必要部材と数量が出るツールを、無料で開放しております。拾い出しの時間がそのまま浮きます。

登録はメールアドレスのみ、費用はかかりません。お試しの場合は本紙に社名とご連絡先をご記入のうえご返信ください。

AshiBase（足場ベース）
FAX: 03-XXXX-XXXX"""),
("メール","A"): ("{name}様 ホームページを拝見しました（足場の積算ツールのご案内）", """{name} ご担当者様

はじめてご連絡いたします。足場の積算ツールを作っておりますAshiBaseと申します。

御社のホームページを拝見しました。{note_short}

図面を送るだけで必要部材と数量が出るツールを無料で開放しております。メールアドレスだけで使えます。

▼こちらからお試しいただけます
https://ashibase.jp/sekisan

AshiBase（足場ベース）"""),
("メール","B"): ("積算の拾い出し、無料のAIツールで試してみませんか", """{name} ご担当者様

急な連絡失礼いたします。足場の積算ツールを作っているAshiBaseと申します。

{pref}でも職人不足で現場が詰まっているという話をよく伺います。そんな中で積算の拾い出しに時間を取られるのはもったいないと思い、図面を送るだけで部材と数量が出るツールを無料で開放しました。

▼メールアドレスだけで使えます
https://ashibase.jp/sekisan

AshiBase（足場ベース）"""),
("郵送DM","C"): ("-", """{name} 御中

はじめてお便りいたします。足場の積算ツールを作っております、AshiBaseと申します。

{pref}の同じくらいの規模の足場会社さんから、「拾い出しにかかっていた時間が短くなった」というお声をいただくことが増えてまいりました。図面をお送りいただくと、必要な部材と数量が自動で出るツールです。

まずは無料でお試しいただけます。費用は一切かかりません。同封のご案内のQRコードから、メールアドレスだけでお使いいただけます。

もしご不明な点がございましたら、お気軽にご連絡ください。

AshiBase（足場ベース）"""),
}

def offline_compose(row):
    ch, va = row["channel"], row["variant"]
    key = (ch, va)
    if key not in OFFLINE:  # 近いものにフォールバック
        key = next((k for k in OFFLINE if k[0] == ch), ("メール", "A"))
    subj, body = OFFLINE[key]
    note = (row["enrich_note"] or "")
    note_short = note.split("。")[0] + "。" if note else "地域に根ざした施工をされている点が印象的でした。"
    ctx = dict(name=row["name"], pref=row["pref"] or "", note_short=note_short)
    return subj.format(**ctx), body.format(**ctx)

def ai_compose(client, row, offer_text=""):
    msg = client.messages.create(
        model=MODEL, max_tokens=1000,
        messages=[{"role": "user", "content": PROMPT.format(
            name=row["name"], pref=row["pref"] or "", trades=row["trades"],
            emp=row["est_employees"] or "不明", note=row["enrich_note"] or "情報なし",
            offer=offer_text, channel_brief=CHANNEL_BRIEF[row["channel"]],
            variant_brief=VARIANT_BRIEF[row["variant"]])}])
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    subj, body = "-", text
    if text.startswith("件名:"):
        head, _, rest = text.partition("\n")
        subj = head.replace("件名:", "").strip()
        body = rest.replace("本文:", "", 1).strip()
    return subj, body

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", type=int, required=True)
    ap.add_argument("--offer", type=int, default=1)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()

    import db, offers as OF
    con = db.connect()
    offer_row, offer_text = load_offer(con, a.offer)
    print(f"オファー: {offer_row['name']}")
    # キャンペーンが使うオファーをここで確定する。send_campaign()側はこれを見て
    # テナント(送信者情報)を解決するため、未設定のままだと送信時に解決できない
    con.execute("UPDATE campaigns SET offer_id=? WHERE id=?", (a.offer, a.campaign))
    con.commit()
    rows = con.execute("""SELECT t.id AS tid, t.channel, t.variant,
                                 c.name, c.pref, c.trades, c.est_employees, c.enrich_note
                          FROM touches t JOIN companies c ON c.id=t.company_id
                          WHERE t.campaign_id=? AND (t.body IS NULL OR t.body='')
                          LIMIT ?""", (a.campaign, a.limit)).fetchall()
    client = rl = None
    if not a.offline:
        import anthropic, resilience as R
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        rl = R.limiter_for("anthropic")

    for r in rows:
        try:
            subj, body = offline_compose(r) if a.offline else ai_compose(client, r, offer_text)
            # NGワード検査: AIは指示しても稀に踏むので機械で止める
            ok, hits = OF.check_ng(con, a.offer, body)
            if not ok:
                print(f"  ⚠ NGワード検出 {hits} → {r['name']} をスキップ")
                continue
            con.execute("UPDATE touches SET subject=?, body=? WHERE id=?", (subj, body, r["tid"]))
            con.commit()
            if rl:
                rl.acquire()
        except Exception as e:
            print(f"✗ {r['name']}: {e}")
    print(f"文面生成完了: {len(rows)}件 (mode={'offline' if a.offline else 'AI'})")

if __name__ == "__main__":
    main()
