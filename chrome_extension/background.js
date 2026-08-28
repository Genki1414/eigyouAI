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
  var filled = 0;
  var els = document.querySelectorAll("input, textarea");
  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    var type = (el.getAttribute("type") || "text").toLowerCase();
    if (["hidden", "checkbox", "radio", "submit", "button", "file", "image"].indexOf(type) >= 0) continue;
    if (el.offsetParent === null) continue;
    var kind = classify(el);
    if (!kind || !values[kind]) continue;
    el.value = values[kind];
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    filled++;
  }
  return filled;
}

function showPageAlert(message) {
  alert(message);
}

async function runAutofill(tabId) {
  const { apiBase, apiKey } = await getCreds();
  if (!apiBase || !apiKey) {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: showPageAlert,
      args: ["ヒラケルとの連携が未設定です。list_builder.htmlの「自動送信ログ」画面で"
           + "「拡張機能と連携する」を押してください。"],
    });
    return;
  }
  let data;
  try {
    const res = await fetch(apiBase.replace(/\/$/, "") + "/api/tenant/autofill/pending", {
      headers: { Authorization: "Bearer " + apiKey },
    });
    if (!res.ok) throw new Error("no-pending");
    data = await res.json();
  } catch (e) {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: showPageAlert,
      args: ["自動入力の準備が見つかりません。自動送信ログ画面の「自動入力」ボタンを先に押してから、"
           + "10分以内にこの拡張機能アイコンをクリックしてください。"],
    });
    return;
  }
  const [{ result: filled }] = await chrome.scripting.executeScript({
    target: { tabId },
    func: fillFieldsInPage,
    args: [data.values || {}],
  });
  await chrome.scripting.executeScript({
    target: { tabId },
    func: showPageAlert,
    args: [filled > 0
      ? "入力しました(" + filled + "項目)。内容を確認のうえ、送信ボタンはご自身で押してください。"
      : "入力できそうな項目が見つかりませんでした。お手数ですが手動で入力してください。"],
  });
}

chrome.action.onClicked.addListener((tab) => {
  if (tab && tab.id != null) runAutofill(tab.id);
});
