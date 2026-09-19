// ヒラケル自動入力アシスト — background service worker (Manifest V3)
//
// ブックマークレット版(list_builder.htmlに埋め込んでいた旧方式)は、対象企業の
// フォームページが厳しめのContent-Security-Policyやmixed content制限を持っていると、
// javascript:リンクからのfetch()やスクリプト実行そのものがブロックされ、
// 「自動入力ボタンを押してもフォームが手入力のまま」になる実運用上の不具合があった
// (ローカルの緩いテストページでは再現しないが、実際の企業サイトでは高頻度に起こりうる)。
// 拡張機能なら、APIへのfetch()はページのCSPに縛られないbackground(このファイル)側で行い、
// フォームへのDOM書き込みだけをchrome.scripting.executeScript()で対象ページに注入するため、
// ページ側のCSPに影響されずに動作する。

async function getCreds() {
  const { apiBase, apiKey } = await chrome.storage.local.get(["apiBase", "apiKey"]);
  return { apiBase, apiKey };
}

// list_builder.html側の「拡張機能と連携する」ボタンから、apiBase/apiKeyを受け取る。
// manifest.jsonのexternally_connectableで許可されたページからのみ届く。
chrome.runtime.onMessageExternal.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "setup") return;
  chrome.storage.local.set({ apiBase: message.apiBase, apiKey: message.apiKey }, () => {
    sendResponse({ ok: true });
  });
  return true; // sendResponseを非同期で呼ぶため
});

// 対象ページのDOMへ注入して実行する関数。chrome.scripting.executeScript()でシリアライズされる
// ため、background.js内の他の変数やクロージャを一切参照できない完全に自己完結な関数にする
// (list_builder.htmlのHINTS/classifyロジックと同じ内容を保つこと)。
function fillFieldsInPage(values) {
  var HINTS = {
    email: ["メールアドレス", "メール", "eメール", "e-mail", "email", "mail"],
    phone: ["電話番号", "電話番号(必須)", "tel", "phone"],
    postal_code: ["郵便番号", "〒", "zip", "postal"],
    prefecture: ["都道府県", "都道府県名", "prefecture", "pref"],
    city: ["市区町村", "市町村", "city"],
    block: ["丁目番地", "丁目・番地", "町名・番地", "丁目", "番地"],
    building: ["ビル名", "建物名", "マンション名", "部屋番号", "building"],
    address: ["住所", "所在地", "address"],
    message: ["お問い合わせ内容", "ご相談内容", "内容", "メッセージ", "message", "ご要望", "本文", "comment"],
    company: ["会社名", "法人名", "貴社名", "御社名", "団体名", "company", "organization"],
    subject: ["件名", "タイトル", "subject"],
    furigana: ["フリガナ", "ふりがな", "カナ", "かな", "kana"],
    name: ["お名前", "氏名", "担当者名", "ご担当者", "ご担当者名", "your name"],
    last_name: ["姓", "苗字", "last name", "family name"],
    first_name: ["名", "first name", "given name"],
  };
  function textFor(el) {
    var label = "";
    try {
      if (el.id) { var l = document.querySelector('label[for="' + el.id + '"]'); if (l) label = l.textContent || ""; }
      if (!label && el.closest) { var lp = el.closest("label"); if (lp) label = lp.textContent || ""; }
    } catch (e) {}
    return [el.name, el.id, el.placeholder, el.getAttribute("aria-label"), label].join(" ").toLowerCase();
  }
  function classify(el) {
    var t = textFor(el), tag = el.tagName.toLowerCase();
    if (tag === "textarea") return "message";
    var order = ["email", "phone", "postal_code", "prefecture", "city", "block", "building",
                 "address", "company", "subject", "furigana", "name", "last_name", "first_name"];
    for (var i = 0; i < order.length; i++) {
      var kind = order[i];
      for (var j = 0; j < HINTS[kind].length; j++) {
        if (t.indexOf(HINTS[kind][j].toLowerCase()) >= 0) return kind;
      }
    }
    return null;
  }
  function visible(el) {
    // offsetParentはposition:fixedの要素でもnullになるので、描画矩形の有無で判定する
    try { return el.getClientRects().length > 0; } catch (e) { return true; }
  }
  var filled = 0;
  var els = document.querySelectorAll("input, textarea");
  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    var type = (el.getAttribute("type") || "text").toLowerCase();
    if (["hidden", "checkbox", "radio", "submit", "button", "file", "image"].indexOf(type) >= 0) continue;
    if (!visible(el)) continue;
    var kind = classify(el);
    if (!kind || !values[kind]) continue;
    el.value = values[kind];
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    filled++;
  }
  // 都道府県などのプルダウン: 選択肢の表示文字が値と一致するものを選ぶ
  var sels = document.querySelectorAll("select");
  for (var k = 0; k < sels.length; k++) {
    var sel = sels[k];
    if (!visible(sel)) continue;
    var skind = classify(sel);
    if (!skind || !values[skind]) continue;
    var want = String(values[skind]).trim();
    for (var o = 0; o < sel.options.length; o++) {
      var txt = (sel.options[o].textContent || "").trim();
      if (txt && (txt === want || want.indexOf(txt) === 0 || txt.indexOf(want) === 0)) {
        sel.value = sel.options[o].value;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        filled++;
        break;
      }
    }
  }
  return filled;
}

function showPageAlert(message) {
  alert(message);
}

// 対象ページへメッセージを出す。chrome://等の注入できないページでは通知にフォールバックする
async function tell(tabId, message) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, func: showPageAlert, args: [message] });
  } catch (e) {
    try {
      chrome.notifications.create({ type: "basic", iconUrl: "icons/icon128.png",
        title: "ヒラケル自動入力アシスト", message });
    } catch (e2) { /* 通知も出せない環境では諦める */ }
  }
  try { await chrome.storage.local.set({ lastResult: new Date().toISOString() + " " + message }); } catch (e) {}
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch (e) { return ""; }
}

async function runAutofill(tab) {
  const tabId = tab.id;
  const { apiBase, apiKey } = await getCreds();
  if (!apiBase || !apiKey) {
    await tell(tabId, "ヒラケルとの連携が未設定です。ヒラケル管理画面の「自動送信ログ」で"
      + "「拡張機能と連携する」を押してください。");
    return;
  }
  let data;
  try {
    const res = await fetch(apiBase.replace(/\/$/, "") + "/api/tenant/autofill/pending", {
      headers: { Authorization: "Bearer " + apiKey },
    });
    if (!res.ok) {
      let msg = "";
      try { msg = (await res.json()).error || ""; } catch (e) {}
      if (res.status === 401) msg = "ヒラケルの接続情報が古いようです。管理画面で「拡張機能と連携する」を押し直してください。";
      throw new Error(msg || ("HTTP " + res.status));
    }
    data = await res.json();
  } catch (e) {
    await tell(tabId, "自動入力の準備が見つかりません(" + (e && e.message ? e.message : e) + ")。"
      + "自動送信ログ画面の「自動入力」ボタンを先に押してから、10分以内にこのタブで拡張機能アイコンを押してください。");
    return;
  }
  // 「自動入力」で開いたタブ以外(ヒラケル管理画面など)で押された場合は入力せずに案内する
  const targetHost = hostOf(data.url || "");
  const thisHost = hostOf(tab.url || "");
  if (targetHost && thisHost && targetHost !== thisHost) {
    await tell(tabId, "このタブは対象企業のページではありません。「自動入力」ボタンで開いたタブ("
      + data.url + ")で拡張機能アイコンを押してください。");
    return;
  }
  let filled = 0;
  try {
    // フォームがiframe内(フォームサービス埋め込み等)にあることも多いので全フレームへ注入する
    const results = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: fillFieldsInPage,
      args: [data.values || {}],
    });
    for (const r of results || []) filled += Number(r && r.result) || 0;
  } catch (e) {
    await tell(tabId, "このページには入力できませんでした(" + (e && e.message ? e.message : e) + ")。"
      + "ページを一度再読み込みしてから、もう一度拡張機能アイコンを押してください。");
    return;
  }
  await tell(tabId, filled > 0
    ? "入力しました(" + filled + "項目)。内容を確認のうえ、送信ボタンはご自身で押してください。"
      + "CAPTCHA(画像認証)がある場合はご自身で解いてください。"
    : "入力できそうな項目が見つかりませんでした(入力欄の名前を判定できないフォーム)。お手数ですが手動で入力してください。");
}

chrome.action.onClicked.addListener((tab) => {
  if (tab && tab.id != null) runAutofill(tab);
});
