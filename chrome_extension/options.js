async function load() {
  const { apiBase, apiKey } = await chrome.storage.local.get(["apiBase", "apiKey"]);
  if (apiBase) document.getElementById("apiBase").value = apiBase;
  if (apiKey) document.getElementById("apiKey").value = apiKey;
}
document.getElementById("btnSave").addEventListener("click", async () => {
  const apiBase = document.getElementById("apiBase").value.trim();
  const apiKey = document.getElementById("apiKey").value.trim();
  await chrome.storage.local.set({ apiBase, apiKey });
  document.getElementById("result").textContent = "保存しました";
});
load();
