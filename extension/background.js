const DEFAULT_URL = "http://localhost:8000";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "FETCH_BRIEF") {
    chrome.storage.sync.get({ backendUrl: DEFAULT_URL }, ({ backendUrl }) => {
      const base = backendUrl.replace(/\/$/, "");
      const refresh = msg.refresh ? "?refresh=true" : "";
      const url = `${base}/api/brief/${msg.managerId}${refresh}`;

      fetch(url)
        .then(r => {
          if (r.status === 429) return r.json().then(body => { throw { status: 429, detail: body.detail }; });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.json();
        })
        .then(data => sendResponse({ ok: true, data }))
        .catch(err => sendResponse({ ok: false, status: err.status || 0, error: err.detail || err.message }));
    });
    return true;
  }

  if (msg.type === "TEST_CONNECTION") {
    chrome.storage.sync.get({ backendUrl: DEFAULT_URL }, ({ backendUrl }) => {
      const base = backendUrl.replace(/\/$/, "");
      fetch(`${base}/`)
        .then(r => sendResponse({ ok: r.ok, status: r.status }))
        .catch(err => sendResponse({ ok: false, error: err.message }));
    });
    return true;
  }
});
