const urlInput = document.getElementById("backendUrl");
const saveBtn  = document.getElementById("save");
const testBtn  = document.getElementById("test");
const status   = document.getElementById("status");

chrome.storage.sync.get({ backendUrl: "" }, ({ backendUrl }) => {
  urlInput.value = backendUrl;
});

function setStatus(msg, cls) {
  status.textContent = msg;
  status.className = `status ${cls}`;
}

saveBtn.addEventListener("click", () => {
  const val = urlInput.value.trim().replace(/\/$/, "");
  if (!val) { setStatus("Enter a backend URL.", "err"); return; }
  chrome.storage.sync.set({ backendUrl: val }, () => {
    setStatus("Saved.", "ok");
    setTimeout(() => { status.textContent = ""; }, 2000);
  });
});

testBtn.addEventListener("click", () => {
  const val = urlInput.value.trim().replace(/\/$/, "");
  if (!val) { setStatus("Enter a URL to test.", "err"); return; }
  chrome.storage.sync.set({ backendUrl: val }, () => {
    setStatus("Testing connection...", "info");
    chrome.runtime.sendMessage({ type: "TEST_CONNECTION" }, resp => {
      if (chrome.runtime.lastError) {
        setStatus("Extension error: " + chrome.runtime.lastError.message, "err");
        return;
      }
      if (resp && resp.ok) {
        setStatus("Connected.", "ok");
      } else {
        setStatus("Cannot reach server. Check URL and try again.", "err");
      }
    });
  });
});

document.getElementById("railway-link").addEventListener("click", e => {
  e.preventDefault();
  chrome.tabs.create({ url: "https://railway.app" });
});
