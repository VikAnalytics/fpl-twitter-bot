const BACKEND_URL = "https://fpl-gaffer.onrender.com";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "FETCH_BRIEF") {
    const refresh = msg.refresh ? "?refresh=true" : "";
    const url = `${BACKEND_URL}/api/brief/${msg.managerId}${refresh}`;

    fetch(url)
      .then(r => {
        if (r.status === 429) return r.json().then(body => { throw { status: 429, detail: body.detail }; });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(data => sendResponse({ ok: true, data }))
      .catch(err => sendResponse({ ok: false, status: err.status || 0, error: err.detail || err.message }));

    return true;
  }
});
