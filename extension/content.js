(() => {
  "use strict";

  // ── State ──────────────────────────────────────────────────────────────────
  let currentManagerId = null;
  let sidebarOpen = false;
  let isLoading = false;
  let cachedData = {};
  let lastFetched = {};
  let shadow = null;

  // ── Manager ID extraction ──────────────────────────────────────────────────
  function extractManagerId(url) {
    const m = url.match(/\/entry\/(\d+)\//);
    return m ? parseInt(m[1], 10) : null;
  }

  // ── CSS ────────────────────────────────────────────────────────────────────
  const CSS = `
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :host { pointer-events: none; }

    #fab {
      pointer-events: all;
      position: fixed;
      bottom: 28px;
      right: 28px;
      width: 50px;
      height: 50px;
      background: #38bdf8;
      border: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 0 18px rgba(56,189,248,0.4);
      transition: transform 0.15s ease, filter 0.15s ease;
      z-index: 2147483647;
    }
    #fab:hover { transform: translateY(-2px); filter: brightness(1.1); }
    #fab svg { width: 24px; height: 24px; fill: #070d1a; }

    #sidebar {
      pointer-events: all;
      position: fixed;
      top: 0; right: 0;
      width: 420px;
      max-width: 100vw;
      height: 100vh;
      background: #070d1a;
      border-left: 1px solid rgba(56,189,248,0.2);
      display: flex;
      flex-direction: column;
      transform: translateX(100%);
      transition: transform 0.28s cubic-bezier(0.4,0,0.2,1);
      overflow: hidden;
      z-index: 2147483646;
      font-family: -apple-system, 'Segoe UI', system-ui, monospace;
    }
    #sidebar.open { transform: translateX(0); }

    .sb-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 13px 16px;
      border-bottom: 1px solid rgba(255,255,255,0.07);
      background: #0d1629;
      flex-shrink: 0;
      gap: 8px;
    }
    .sb-logo { font-size: 14px; font-weight: 700; letter-spacing: 0.1em; color: #e2e8f4; font-family: monospace; flex: 1; }
    .sb-logo span { color: #38bdf8; }
    .sb-updated { font-family: monospace; font-size: 9px; color: #334155; letter-spacing: 0.08em; }

    .sb-btn {
      background: none;
      border: 1px solid rgba(255,255,255,0.1);
      color: #64748b;
      font-size: 11px;
      padding: 4px 9px;
      cursor: pointer;
      font-family: monospace;
      letter-spacing: 0.05em;
      transition: border-color 0.15s, color 0.15s;
      flex-shrink: 0;
    }
    #sb-refresh:hover { border-color: #38bdf8; color: #38bdf8; }
    #sb-close:hover   { border-color: #f43f5e; color: #f43f5e; }

    .sb-body {
      flex: 1;
      overflow-y: auto;
      padding: 16px 18px 40px;
      scrollbar-width: thin;
      scrollbar-color: rgba(56,189,248,0.2) transparent;
    }

    .sb-loading {
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      height: 200px; gap: 14px;
      color: #64748b; font-family: monospace;
      font-size: 11px; letter-spacing: 0.12em;
    }
    .spinner {
      width: 26px; height: 26px;
      border: 2px solid rgba(56,189,248,0.2);
      border-top-color: #38bdf8;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    .sb-error {
      padding: 16px;
      border: 1px solid rgba(244,63,94,0.25);
      border-left: 3px solid #f43f5e;
      font-family: monospace; font-size: 11px;
      color: #94a3b8; line-height: 1.7;
    }
    .sb-error strong { color: #f43f5e; display: block; margin-bottom: 8px; font-size: 12px; }
    .sb-error .hint { color: #64748b; font-size: 10px; margin-top: 10px; }

    .s-label {
      font-family: monospace; font-size: 9px; color: #38bdf8;
      letter-spacing: 0.22em; text-transform: uppercase;
      margin-bottom: 10px;
      display: flex; align-items: center; gap: 7px;
    }
    .s-label::before { content: '//'; color: #38bdf8; opacity: 0.5; }

    .divider { height: 1px; background: rgba(255,255,255,0.07); margin: 14px 0; }

    .manager-team {
      font-family: monospace; font-size: 15px; font-weight: 700;
      color: #e2e8f4; letter-spacing: 0.05em; text-transform: uppercase;
      margin-bottom: 3px;
    }
    .manager-sub {
      font-family: monospace; font-size: 10px;
      color: #64748b; letter-spacing: 0.08em; margin-bottom: 14px;
    }

    .stat-row { display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 14px; }
    .stat { display: flex; flex-direction: column; }
    .stat-val { font-family: monospace; font-size: 20px; font-weight: 700; color: #38bdf8; line-height: 1; }
    .stat-val.amber { color: #f59e0b; }
    .stat-val.rose  { color: #f43f5e; }
    .stat-val.em    { color: #10b981; }
    .stat-lbl { font-family: monospace; font-size: 9px; color: #64748b; letter-spacing: 0.12em; text-transform: uppercase; margin-top: 3px; }

    .deadline-banner {
      display: flex; align-items: center; justify-content: space-between;
      background: rgba(245,158,11,0.04);
      border: 1px solid rgba(245,158,11,0.2);
      border-left: 3px solid #f59e0b;
      padding: 10px 14px; margin-bottom: 14px;
    }
    .dl-lbl { font-family: monospace; font-size: 9px; color: #64748b; letter-spacing: 0.12em; text-transform: uppercase; margin-bottom: 3px; }
    .dl-time { font-family: monospace; font-size: 13px; font-weight: 600; color: #f59e0b; }
    .dl-pulse {
      width: 8px; height: 8px; border-radius: 50%; background: #f59e0b;
      animation: pulse 2s ease-in-out infinite;
    }
    @keyframes pulse {
      0%,100% { box-shadow: 0 0 0 0 rgba(245,158,11,0.5); }
      50% { box-shadow: 0 0 0 6px rgba(245,158,11,0); }
    }

    .budget-strip {
      display: flex; flex-wrap: wrap; gap: 14px;
      background: #0d1629; border: 1px solid rgba(255,255,255,0.07);
      padding: 10px 14px; margin-bottom: 14px;
    }
    .bud-item { display: flex; flex-direction: column; }
    .bud-val { font-family: monospace; font-size: 13px; font-weight: 600; color: #e2e8f4; }
    .bud-lbl { font-family: monospace; font-size: 9px; color: #64748b; letter-spacing: 0.1em; text-transform: uppercase; margin-top: 2px; }

    .narrative {
      background: rgba(56,189,248,0.03);
      border: 1px solid rgba(56,189,248,0.18);
      border-left: 3px solid #38bdf8;
      padding: 12px 14px; margin-bottom: 14px;
      font-size: 12px; line-height: 1.75;
      color: #e2e8f4; font-weight: 300;
    }

    .t-card {
      background: #0d1629;
      border: 1px solid rgba(255,255,255,0.07);
      border-left: 3px solid #38bdf8;
      margin-bottom: 10px; overflow: hidden;
    }
    .t-header {
      display: flex; align-items: center; gap: 10px;
      padding: 11px 13px 10px;
      border-bottom: 1px solid rgba(255,255,255,0.07);
    }
    .t-num { font-family: monospace; font-size: 17px; font-weight: 700; color: #38bdf8; opacity: 0.35; flex-shrink: 0; width: 18px; }
    .t-arrow-row { flex: 1; display: flex; align-items: center; gap: 8px; min-width: 0; }
    .t-player { display: flex; flex-direction: column; min-width: 0; }
    .t-name { font-family: monospace; font-size: 11.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .t-name.out { color: #f43f5e; }
    .t-name.in  { color: #10b981; }
    .t-club { font-family: monospace; font-size: 9px; color: #64748b; margin-top: 1px; }
    .t-chevron { font-size: 13px; color: #64748b; flex-shrink: 0; }
    .t-conf { flex-shrink: 0; }

    .t-signals {
      display: flex; flex-wrap: wrap; gap: 4px;
      padding: 7px 13px;
      border-bottom: 1px solid rgba(255,255,255,0.07);
    }
    .t-signal {
      font-family: monospace; font-size: 9px; letter-spacing: 0.06em;
      color: #64748b; background: rgba(255,255,255,0.03);
      border: 1px solid rgba(255,255,255,0.07); padding: 2px 6px;
    }

    .t-body { display: grid; grid-template-columns: 1fr 1fr; }
    .t-section { padding: 9px 13px; }
    .t-section:first-child { border-right: 1px solid rgba(255,255,255,0.07); }
    .t-section-lbl { font-family: monospace; font-size: 9px; letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 5px; }
    .t-section-lbl.sell { color: #f43f5e; }
    .t-section-lbl.buy  { color: #10b981; }
    .t-section-text { font-size: 11px; line-height: 1.6; color: #e2e8f4; font-weight: 300; }

    .t-footer-row {
      display: flex; align-items: baseline; gap: 8px;
      padding: 7px 13px;
      border-top: 1px solid rgba(255,255,255,0.07);
    }
    .t-footer-lbl { font-family: monospace; font-size: 9px; letter-spacing: 0.1em; text-transform: uppercase; flex-shrink: 0; }
    .t-footer-lbl.budget { color: #f59e0b; }
    .t-footer-lbl.ext    { color: #818cf8; }
    .t-footer-text { font-family: monospace; font-size: 10px; color: #64748b; line-height: 1.55; }

    .inj-card {
      background: rgba(244,63,94,0.04);
      border: 1px solid rgba(244,63,94,0.18);
      border-left: 3px solid #f43f5e;
      padding: 10px 13px; margin-bottom: 8px;
    }
    .inj-name { font-family: monospace; font-size: 12px; font-weight: 600; color: #e2e8f4; margin-bottom: 3px; }
    .inj-news { font-family: monospace; font-size: 9.5px; color: #64748b; line-height: 1.5; margin-bottom: 6px; }
    .inj-footer { display: flex; justify-content: space-between; align-items: center; }

    .player-row {
      display: flex; align-items: center; justify-content: space-between;
      padding: 6px 0;
      border-bottom: 1px solid rgba(255,255,255,0.04);
      font-family: monospace; font-size: 11px; color: #e2e8f4;
    }
    .player-row:last-child { border-bottom: none; }
    .player-club { font-size: 9px; color: #64748b; }

    .badge {
      display: inline-block; font-family: monospace; font-size: 8.5px;
      letter-spacing: 0.08em; padding: 2px 7px; text-transform: uppercase;
    }
    .b-blue  { color: #38bdf8; border: 1px solid rgba(56,189,248,0.3);  background: rgba(56,189,248,0.06); }
    .b-em    { color: #10b981; border: 1px solid rgba(16,185,129,0.3);  background: rgba(16,185,129,0.06); }
    .b-amber { color: #f59e0b; border: 1px solid rgba(245,158,11,0.3);  background: rgba(245,158,11,0.06); }
    .b-rose  { color: #f43f5e; border: 1px solid rgba(244,63,94,0.3);   background: rgba(244,63,94,0.06);  }
    .b-orange{ color: #fb923c; border: 1px solid rgba(251,146,60,0.3);  background: rgba(251,146,60,0.06); }
    .b-dim   { color: #64748b; border: 1px solid rgba(255,255,255,0.1); }

    .empty { font-family: monospace; font-size: 10px; color: #64748b; font-style: italic; padding: 6px 0; }

    .outcome-card {
      border: 1px solid rgba(255,255,255,0.07);
      padding: 10px 13px; margin-bottom: 8px;
      display: grid; grid-template-columns: 1fr auto;
      gap: 6px; align-items: start;
    }
    .outcome-card.implemented { border-left: 3px solid #10b981; }
    .outcome-card.skipped     { border-left: 3px solid #f59e0b; }
    .outcome-move {
      font-family: monospace; font-size: 11px; font-weight: 600;
      color: #e2e8f4; margin-bottom: 4px;
    }
    .outcome-move .out { color: #f43f5e; }
    .outcome-move .arr { color: #64748b; }
    .outcome-move .in  { color: #10b981; }
    .outcome-meta {
      font-family: monospace; font-size: 9px; color: #64748b; letter-spacing: 0.06em;
    }
    .outcome-delta {
      font-family: monospace; font-size: 18px; font-weight: 700; text-align: right; line-height: 1;
    }
    .outcome-delta.pos { color: #10b981; }
    .outcome-delta.neg { color: #f43f5e; }
    .outcome-delta.neu { color: #64748b; }
    .outcome-pts {
      font-family: monospace; font-size: 9px; color: #64748b; text-align: right; margin-top: 2px;
    }
  `;

  // ── Helpers ────────────────────────────────────────────────────────────────
  function confBadge(c) {
    if (c === "High")   return `<span class="badge b-em">● High</span>`;
    if (c === "Medium") return `<span class="badge b-amber">● Medium</span>`;
    return `<span class="badge b-dim">● Low</span>`;
  }

  function chanceBadge(chance) {
    if (chance === null || chance === undefined) return `<span class="badge b-dim">Doubt</span>`;
    if (chance >= 75) return `<span class="badge b-em">${chance}%</span>`;
    if (chance >= 50) return `<span class="badge b-orange">${chance}%</span>`;
    return `<span class="badge b-rose">${chance}%</span>`;
  }

  function timeAgo(ts) {
    const secs = Math.floor((Date.now() - ts) / 1000);
    if (secs < 60) return "just now";
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    return `${Math.floor(mins / 60)}h ago`;
  }

  // ── Render ─────────────────────────────────────────────────────────────────
  function renderContent(data) {
    const m = data.manager;
    const budget = data.budget;
    const transfers = data.transfer_recommendations || [];
    const injuries = data.injury_flags || [];
    const dgw = data.dgw_players || [];
    const bgw = data.bgw_players || [];

    let html = `
      <div class="manager-team">${m.team_name}</div>
      <div class="manager-sub">${m.name} · GW${m.current_gameweek}</div>
      <div class="stat-row">
        <div class="stat"><span class="stat-val">${Number(m.overall_rank).toLocaleString()}</span><span class="stat-lbl">Rank</span></div>
        <div class="stat"><span class="stat-val amber">${m.total_points}</span><span class="stat-lbl">Pts</span></div>
        <div class="stat"><span class="stat-val em">${dgw.length}</span><span class="stat-lbl">DGW</span></div>
        <div class="stat"><span class="stat-val rose">${injuries.length}</span><span class="stat-lbl">Injuries</span></div>
      </div>
    `;

    if (data.deadline_str) {
      html += `<div class="deadline-banner"><div><div class="dl-lbl">Next Deadline</div><div class="dl-time">${data.deadline_str}</div></div><div class="dl-pulse"></div></div>`;
    }

    if (budget) {
      html += `<div class="budget-strip">
        <div class="bud-item"><span class="bud-val">£${budget.itb}m</span><span class="bud-lbl">In the Bank</span></div>
        <div class="bud-item"><span class="bud-val">${budget.free_transfers}</span><span class="bud-lbl">Free Transfers</span></div>
        <div class="bud-item"><span class="bud-val">£${budget.team_value}m</span><span class="bud-lbl">Team Value</span></div>
      </div>`;
    }

    html += `<div class="s-label">Analysis</div><div class="narrative">${data.brief_narrative}</div><div class="divider"></div>`;

    if (transfers.length) {
      html += `<div class="s-label">Transfer Picks</div>`;
      transfers.forEach((t, i) => {
        html += `<div class="t-card">
          <div class="t-header">
            <span class="t-num">${i + 1}</span>
            <div class="t-arrow-row">
              <div class="t-player"><span class="t-name out">${t.out}</span><span class="t-club">${t.out_club} · ${t.out_price}</span></div>
              <span class="t-chevron">→</span>
              <div class="t-player"><span class="t-name in">${t.in_}</span><span class="t-club">${t.in_club} · ${t.in_price}</span></div>
            </div>
            <div class="t-conf">${confBadge(t.confidence)}</div>
          </div>`;
        if (t.signals && t.signals.length) {
          html += `<div class="t-signals">${t.signals.map(s => `<span class="t-signal">${s}</span>`).join("")}</div>`;
        }
        html += `<div class="t-body">
            <div class="t-section"><div class="t-section-lbl sell">↓ Why Sell</div><p class="t-section-text">${t.sell_reasoning}</p></div>
            <div class="t-section"><div class="t-section-lbl buy">↑ Why Buy</div><p class="t-section-text">${t.buy_reasoning}</p></div>
          </div>
          <div class="t-footer-row"><span class="t-footer-lbl budget">£ Budget</span><span class="t-footer-text">${t.budget_check}</span></div>`;
        if (t.external_context) {
          html += `<div class="t-footer-row"><span class="t-footer-lbl ext">⚡ Context</span><span class="t-footer-text">${t.external_context}</span></div>`;
        }
        html += `</div>`;
      });
      html += `<div class="divider"></div>`;
    }

    if (dgw.length || bgw.length) {
      html += `<div class="s-label">Gameweek Exposure</div>`;
      dgw.forEach(p => {
        html += `<div class="player-row"><span>${p.web_name}</span><span class="player-club">${p.team_name}</span><span class="badge b-em">DGW</span></div>`;
      });
      bgw.forEach(p => {
        html += `<div class="player-row"><span>${p.web_name}</span><span class="player-club">${p.team_name}</span><span class="badge b-amber">BLANK</span></div>`;
      });
      html += `<div class="divider"></div>`;
    }

    if (injuries.length) {
      html += `<div class="s-label">Injury Radar</div>`;
      injuries.forEach(p => {
        html += `<div class="inj-card">
          <div class="inj-name">${p.web_name}</div>
          <div class="inj-news">${p.news || "Injury concern"}</div>
          <div class="inj-footer">${chanceBadge(p.chance_of_playing_next_round)}<span class="player-club">${p.team_name}</span></div>
        </div>`;
      });
    }

    const outcomes = data.past_outcomes || [];
    if (outcomes.length) {
      html += `<div class="divider"></div><div class="s-label">Track Record</div>`;
      outcomes.forEach(o => {
        const implemented = o.implemented;
        const delta = o.delta;
        const deltaClass = delta === null ? "neu" : delta > 0 ? "pos" : delta < 0 ? "neg" : "neu";
        const deltaStr  = delta === null ? "—" : (delta > 0 ? `+${delta}` : `${delta}`);
        const ptsStr    = (o.out_points !== null && o.in_points !== null)
          ? `${o.out_name} ${o.out_points}pts · ${o.in_name} ${o.in_points}pts`
          : "";
        const statusLabel = implemented
          ? `<span class="badge b-em">✓ Made</span>`
          : `<span class="badge b-amber">✗ Skipped</span>`;
        const caption = implemented
          ? `GW${o.gameweek} · Transfer made`
          : `GW${o.gameweek} · Kept ${o.out_name}`;
        html += `<div class="outcome-card ${implemented ? "implemented" : "skipped"}">
          <div>
            <div class="outcome-move">
              <span class="out">${o.out_name}</span>
              <span class="arr"> → </span>
              <span class="in">${o.in_name}</span>
            </div>
            <div class="outcome-meta">${caption} · ${statusLabel}</div>
          </div>
          <div>
            <div class="outcome-delta ${deltaClass}">${deltaStr}<span style="font-size:10px;opacity:0.6">pts</span></div>
            <div class="outcome-pts">${ptsStr}</div>
          </div>
        </div>`;
      });
    }

    return html;
  }

  // ── Sidebar actions ────────────────────────────────────────────────────────
  function openSidebar() {
    const sidebar = shadow && shadow.getElementById("sidebar");
    const fab = shadow && shadow.getElementById("fab");
    if (!sidebar) return;
    sidebar.classList.add("open");
    sidebarOpen = true;
    if (fab) fab.style.display = "none";
  }

  function closeSidebar() {
    const sidebar = shadow && shadow.getElementById("sidebar");
    const fab = shadow && shadow.getElementById("fab");
    if (!sidebar) return;
    sidebar.classList.remove("open");
    sidebarOpen = false;
    if (fab) fab.style.display = "flex";
  }

  function updateTimestamp(mid) {
    const el = shadow && shadow.getElementById("sb-updated");
    if (!el) return;
    el.textContent = lastFetched[mid] ? `Updated ${timeAgo(lastFetched[mid])}` : "";
  }

  function fetchBrief(managerId, sbBody, forceRefresh) {
    if (isLoading) return;
    isLoading = true;
    sbBody.innerHTML = `<div class="sb-loading"><div class="spinner"></div><span>${forceRefresh ? "REFRESHING..." : "LOADING INTEL..."}</span></div>`;

    const refreshBtn = shadow && shadow.getElementById("sb-refresh");
    if (refreshBtn) refreshBtn.disabled = true;

    chrome.runtime.sendMessage({ type: "FETCH_BRIEF", managerId, refresh: !!forceRefresh }, resp => {
      isLoading = false;
      if (refreshBtn) refreshBtn.disabled = false;

      if (chrome.runtime.lastError) {
        sbBody.innerHTML = `<div class="sb-error"><strong>Extension error</strong>${chrome.runtime.lastError.message}</div>`;
        return;
      }
      if (!resp || !resp.ok) {
        const isLimit = resp && resp.status === 429;
        sbBody.innerHTML = `<div class="sb-error">
          <strong>${isLimit ? "Daily limit reached" : "Could not reach FPL Gaffer server"}</strong>
          ${isLimit
            ? resp.error
            : "Check that your backend URL is configured correctly in the extension popup."}
          ${!isLimit ? `<span class="hint">Error: ${resp ? resp.error : "No response from background"}</span>` : ""}
        </div>`;
        return;
      }
      cachedData[managerId] = resp.data;
      lastFetched[managerId] = Date.now();
      sbBody.innerHTML = renderContent(resp.data);
      updateTimestamp(managerId);
    });
  }

  // ── Init ───────────────────────────────────────────────────────────────────
  function buildUI() {
    const existing = document.getElementById("fpl-gaffer-host");
    if (existing) existing.remove();

    const host = document.createElement("div");
    host.id = "fpl-gaffer-host";
    document.body.appendChild(host);
    shadow = host.attachShadow({ mode: "open" });

    const style = document.createElement("style");
    style.textContent = CSS;
    shadow.appendChild(style);

    // FAB
    const fab = document.createElement("button");
    fab.id = "fab";
    fab.setAttribute("aria-label", "Open FPL Gaffer");
    fab.innerHTML = `<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>`;
    shadow.appendChild(fab);

    // Sidebar
    const sidebar = document.createElement("div");
    sidebar.id = "sidebar";
    sidebar.innerHTML = `
      <div class="sb-header">
        <span class="sb-logo">FPL<span>Gaffer</span></span>
        <span class="sb-updated" id="sb-updated"></span>
        <button class="sb-btn" id="sb-refresh" title="Refresh brief">↻</button>
        <button class="sb-btn" id="sb-close">✕ ESC</button>
      </div>
      <div class="sb-body" id="sb-body">
        <div class="sb-loading"><div class="spinner"></div><span>LOADING INTEL...</span></div>
      </div>
    `;
    shadow.appendChild(sidebar);

    const sbBody = shadow.getElementById("sb-body");

    fab.addEventListener("click", () => {
      if (!currentManagerId) {
        sbBody.innerHTML = `<div class="sb-error"><strong>No manager detected</strong>Navigate to your FPL team page first.</div>`;
        openSidebar();
        return;
      }
      if (cachedData[currentManagerId]) {
        sbBody.innerHTML = renderContent(cachedData[currentManagerId]);
        updateTimestamp(currentManagerId);
      } else {
        fetchBrief(currentManagerId, sbBody, false);
      }
      openSidebar();
    });

    shadow.getElementById("sb-refresh").addEventListener("click", () => {
      if (!currentManagerId || isLoading) return;
      delete cachedData[currentManagerId];
      fetchBrief(currentManagerId, sbBody, true);
    });

    shadow.getElementById("sb-close").addEventListener("click", closeSidebar);
  }

  function init() {
    if (!document.body) return;
    buildUI();
  }

  // ── Watch for React wiping body ───────────────────────────────────────────
  function watchBody() {
    const observer = new MutationObserver(() => {
      if (!document.getElementById("fpl-gaffer-host")) buildUI();
    });
    observer.observe(document.body, { childList: true });
  }

  // ── URL change detection ───────────────────────────────────────────────────
  function onUrlChange() {
    const mid = extractManagerId(location.href);
    if (mid && mid !== currentManagerId) {
      currentManagerId = mid;
    }
  }

  const origPush    = history.pushState.bind(history);
  const origReplace = history.replaceState.bind(history);
  history.pushState    = (...args) => { origPush(...args);    onUrlChange(); };
  history.replaceState = (...args) => { origReplace(...args); onUrlChange(); };
  window.addEventListener("popstate", onUrlChange);

  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && sidebarOpen) closeSidebar();
  });

  onUrlChange();

  if (document.body) {
    init();
    watchBody();
  } else {
    document.addEventListener("DOMContentLoaded", () => { init(); watchBody(); });
  }
})();
