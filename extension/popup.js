const BACKEND_URL = "https://fpl-gaffer.onrender.com";

fetch(`${BACKEND_URL}/`)
  .then(r => {
    if (!r.ok) throw new Error();
  })
  .catch(() => {
    document.getElementById("dot").className = "dot grey";
    document.getElementById("status-text").textContent = "Server is waking up (~30s)…";
  });
