from __future__ import annotations

import os
import sys
import threading

from dotenv import load_dotenv
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, Form, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .database import (
    init_db, get_managers, add_manager,
    get_brief_cache, set_brief_cache,
    get_rate_limit_count, increment_rate_limit, DAILY_BRIEF_LIMIT,
    save_transfer_suggestions, get_unevaluated_suggestions,
    save_transfer_outcome, get_recent_outcomes,
    get_agent_decision, set_decision_status,
    get_decisions, get_conversation, get_candidates_for_decision,
    get_run_log, get_recent_runs,
)
from . import observability
from .fpl_auth import FplAuthError, fetch_my_team, login as fpl_login, set_lineup, submit_transfers
from .notify import answer_telegram_callback, escalate_execution_failure
from .fpl_client import (
    build_budget_info,
    build_player_lookup,
    build_squad_picks,
    build_team_lookup,
    build_team_strength_lookup,
    detect_dgw_bgw,
    fetch_bootstrap,
    fetch_current_picks,
    fetch_fixtures,
    fetch_league_standings,
    fetch_manager_info,
    fetch_squad_recent_forms,
    fetch_player_gw_points,
    fetch_transfer_history,
    find_valid_replacements,
    get_current_gameweek,
    get_deadline_str,
)
from .llm import generate_pre_deadline_brief, generate_vibe_check
from .models import AuditResult, BriefResult, PlayerSummary, TransferOutcome
from . import ranking

app = FastAPI(title="FPL Gaffer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:*", "https://fantasy.premierleague.com"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def startup():
    init_db()


# ── Helpers ────────────────────────────────────────────────────────────────────

def derive_captain_score(squad) -> str:
    for pick in squad:
        if pick.is_captain:
            p = pick.player
            if p.chance_of_playing_next_round is not None and p.chance_of_playing_next_round < 75:
                return "Poor"
            if p.form < 4.0:
                return "Poor"
            if p.form < 6.0:
                return "Risky"
            return "Good"
    return "Poor"


def _save_suggestions(
    manager_id: int,
    gw: int,
    transfers: list,
    squad: list,
    player_lookup: dict,
) -> None:
    name_to_squad_id = {pick.player.web_name.lower(): pick.player.id for pick in squad}
    name_to_any_id   = {p["web_name"].lower(): pid for pid, p in player_lookup.items()}
    suggestions = []
    for t in transfers:
        out_id = name_to_squad_id.get(t.out.lower()) or name_to_any_id.get(t.out.lower())
        in_id  = name_to_any_id.get(t.in_.lower())
        suggestions.append({"out_id": out_id, "out_name": t.out, "in_id": in_id, "in_name": t.in_})
    save_transfer_suggestions(manager_id, gw, suggestions)


def _evaluate_pending_outcomes(
    manager_id: int,
    current_gw: int,
    transfer_history: list[dict],
) -> list[TransferOutcome]:
    pending = get_unevaluated_suggestions(manager_id, before_gw=current_gw)
    for s in pending:
        implemented = any(
            t.get("element_out") == s["out_id"]
            and t.get("element_in") == s["in_id"]
            and t.get("event") == s["gameweek"]
            for t in transfer_history
        )
        out_pts = fetch_player_gw_points(s["out_id"], s["gameweek"])
        in_pts  = fetch_player_gw_points(s["in_id"],  s["gameweek"])
        save_transfer_outcome(s["id"], implemented, out_pts, in_pts)

    rows = get_recent_outcomes(manager_id, limit=5)
    return [
        TransferOutcome(
            gameweek=r["gameweek"],
            out_name=r["out_name"],
            in_name=r["in_name"],
            implemented=bool(r["implemented"]),
            out_points=r["out_points"],
            in_points=r["in_points"],
            delta=r["delta"],
        )
        for r in rows
    ]


def _build_brief(manager_id: int) -> BriefResult:
    """
    Core brief logic shared by the HTML route, the JSON API, and the cache layer.
    Checks SQLite brief cache first; falls back to full FPL + LLM pipeline.
    """
    bootstrap = fetch_bootstrap()
    gw = get_current_gameweek(bootstrap)

    # ── Cache hit (free — doesn't count toward daily limit) ───────────────────
    cached = get_brief_cache(manager_id, gw)
    if cached:
        return BriefResult.model_validate(cached)

    # ── Rate limit (only applies to actual LLM generation) ────────────────────
    count = get_rate_limit_count(manager_id)
    if count >= DAILY_BRIEF_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit of {DAILY_BRIEF_LIMIT} analyses reached. Cached briefs are still available. Resets at midnight UTC.",
            headers={"Retry-After": "86400"},
        )

    # ── Full pipeline ──────────────────────────────────────────────────────────
    manager = fetch_manager_info(manager_id, bootstrap)
    raw_picks, entry_history, active_chip = fetch_current_picks(manager_id, gw)
    fixtures = fetch_fixtures()
    player_lookup = build_player_lookup(bootstrap)
    team_lookup = build_team_lookup(bootstrap)
    team_name_lookup = {t["id"]: t["name"] for t in bootstrap["teams"]}
    team_name_to_id = {t["name"]: t["id"] for t in bootstrap["teams"]}
    strength_lookup = build_team_strength_lookup(bootstrap)

    transfer_history = fetch_transfer_history(manager_id)
    past_outcomes = _evaluate_pending_outcomes(manager_id, gw, transfer_history)
    player_ids = [p["element"] for p in raw_picks]
    recent_forms = fetch_squad_recent_forms(player_ids)
    squad = build_squad_picks(
        raw_picks, player_lookup, team_lookup, gw, fixtures, bootstrap,
        recent_forms, strength_lookup,
    )

    injury_flags: list[PlayerSummary] = [
        pick.player for pick in squad
        if pick.player.chance_of_playing_next_round is not None
        and pick.player.chance_of_playing_next_round < 75
    ]

    deadline_str = get_deadline_str(bootstrap)
    budget = build_budget_info(entry_history, transfer_history, gw)
    league_standings = fetch_league_standings(manager_id)

    dgw_team_ids, bgw_team_ids = detect_dgw_bgw(bootstrap, fixtures, gw)
    dgw_players = [p.player for p in squad if team_name_to_id.get(p.player.team_name) in dgw_team_ids]
    bgw_players = [p.player for p in squad if team_name_to_id.get(p.player.team_name) in bgw_team_ids]

    # ── Rank sell candidates first (XI only) ──────────────────────────────────
    xi_picks = [p for p in squad if p.position <= 11]
    sell_reports = sorted(
        (ranking.score_sell(pk.player, gw) for pk in xi_picks),
        key=lambda r: r.score,
        reverse=True,
    )
    top_sell_reports = sell_reports[:5]

    # ── Ground targets against the actual top sell candidates ────────────────
    recent_sold_ids = ranking.recently_sold_ids(transfer_history, gw, lookback=3)
    grounded_targets: dict[str, list] = {}
    for report in top_sell_reports:
        sell_p = report.player
        replacements = find_valid_replacements(
            sell_player=sell_p,
            budget_max=round(sell_p.now_cost + budget.itb, 1),
            squad=squad,
            player_lookup=player_lookup,
            team_lookup=team_lookup,
            team_name_lookup=team_name_lookup,
            current_gw=gw,
            fixtures=fixtures,
            strength_lookup=strength_lookup,
            recently_sold_ids=recent_sold_ids,
        )
        if replacements:
            grounded_targets[sell_p.web_name] = replacements

    narrative, transfers = generate_pre_deadline_brief(
        manager, squad, injury_flags, dgw_players, bgw_players,
        deadline_str, transfer_history, budget, league_standings,
        grounded_targets=grounded_targets,
        sell_reports=top_sell_reports,
        active_chip=active_chip,
        past_outcomes=past_outcomes,
    )

    _save_suggestions(manager_id, gw, transfers, squad, player_lookup)

    result = BriefResult(
        manager=manager,
        squad=squad,
        deadline_str=deadline_str,
        brief_narrative=narrative,
        transfer_recommendations=transfers,
        injury_flags=injury_flags,
        dgw_players=dgw_players,
        bgw_players=bgw_players,
        budget=budget,
        league_standings=league_standings,
        past_outcomes=past_outcomes,
    )

    increment_rate_limit(manager_id)
    set_brief_cache(manager_id, gw, result.model_dump())
    return result


# ── Autonomous decision execution ────────────────────────────────────────────

def _execute_transfer_decision(decision: dict, run_id: str) -> dict:
    manager_id, gw = decision["manager_id"], decision["gameweek"]
    transfers = decision["proposal"].get("transfers", [])
    if not transfers:
        return {"ok": True, "note": "no transfers to execute"}

    with observability.step(run_id, "execution.login", gameweek=gw, manager_id=manager_id, decision_id=decision["id"]):
        bootstrap = fetch_bootstrap()
        player_lookup = build_player_lookup(bootstrap)
        try:
            session = fpl_login()
            my_team = fetch_my_team(session, manager_id)
        except (FplAuthError, Exception) as e:  # noqa: BLE001 — surfaced to caller for escalation
            return {"ok": False, "error": str(e)}

    selling_price_by_id = {p["element"]: p.get("selling_price") for p in my_team.get("picks", [])}

    payload = []
    for t in transfers:
        purchase_price = (player_lookup.get(t["in_id"]) or {}).get("now_cost")
        selling_price = selling_price_by_id.get(t["out_id"])
        if purchase_price is None or selling_price is None:
            return {"ok": False, "error": f"missing price data for {t['out']} -> {t['in']}"}
        payload.append({
            "element_in": t["in_id"], "element_out": t["out_id"],
            "purchase_price": purchase_price, "selling_price": selling_price,
        })

    with observability.step(run_id, "execution.submit_transfers", gameweek=gw, manager_id=manager_id, decision_id=decision["id"]) as ctx:
        result = submit_transfers(session, manager_id, gw, payload)
        ctx["detail"] = {"payload": payload, "result": result}
    return result


def _build_lineup_payload(captain_decision: dict, lineup_decision: dict) -> list[dict]:
    cap_id = captain_decision["proposal"]["captain_id"]
    vice_id = captain_decision["proposal"]["vice_id"]

    picks = []
    for i, p in enumerate(lineup_decision["proposal"]["starting_xi"], start=1):
        picks.append({
            "element": p["player_id"], "position": i,
            "is_captain": p["player_id"] == cap_id,
            "is_vice_captain": p["player_id"] == vice_id,
        })
    for b in lineup_decision["proposal"]["bench_order"]:
        picks.append({
            "element": b["player_id"], "position": 11 + b["order"],
            "is_captain": False, "is_vice_captain": False,
        })
    return picks


def _execute_lineup_decisions(captain_decision: dict, lineup_decision: dict, run_id: str) -> dict:
    manager_id, gw = lineup_decision["manager_id"], lineup_decision["gameweek"]
    with observability.step(run_id, "execution.set_lineup", gameweek=gw, manager_id=manager_id) as ctx:
        try:
            session = fpl_login()
        except (FplAuthError, Exception) as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        picks = _build_lineup_payload(captain_decision, lineup_decision)
        result = set_lineup(session, manager_id, picks)
        ctx["detail"] = {"picks": picks, "result": result}
    return result


def _execute_decision(decision_id: int) -> None:
    """
    Runs synchronously in the approval request — one human tap, then it
    executes immediately. On failure, status is set to 'failed' and a hard
    Telegram escalation fires (a tweet alone isn't reliable enough for a
    real-money-adjacent action with a hard deadline).

    Captain and lineup are separate decisions but map to ONE FPL API call
    (/my-team/{id}/ takes the full 15-pick payload with captain/vice flags
    baked in) — so approving one just marks it 'approved' and waits; only
    once BOTH the captain and lineup decisions for the same gameweek are
    approved does this submit the combined payload and mark both 'executed'.
    """
    run_id = observability.new_run_id()
    decision = get_agent_decision(decision_id)
    if decision is None:
        return

    if decision["decision_type"] == "transfer":
        result = _execute_transfer_decision(decision, run_id)
        if result.get("ok"):
            set_decision_status(decision_id, "executed")
        else:
            set_decision_status(decision_id, "failed")
            escalate_execution_failure(decision["gameweek"], decision_id, result.get("error", "unknown error"))
        return

    if decision["decision_type"] in ("captain", "lineup"):
        sibling_type = "lineup" if decision["decision_type"] == "captain" else "captain"
        siblings = get_decisions(decision["manager_id"], decision["gameweek"], sibling_type)
        sibling = siblings[0] if siblings else None

        if sibling is None or sibling["status"] not in ("approved", "executed"):
            return  # waiting on the other half of the pair
        if sibling["status"] == "executed":
            return  # already submitted together when the sibling was approved

        captain_decision = decision if decision["decision_type"] == "captain" else sibling
        lineup_decision = decision if decision["decision_type"] == "lineup" else sibling

        result = _execute_lineup_decisions(captain_decision, lineup_decision, run_id)
        status = "executed" if result.get("ok") else "failed"
        set_decision_status(captain_decision["id"], status)
        set_decision_status(lineup_decision["id"], status)
        if not result.get("ok"):
            escalate_execution_failure(decision["gameweek"], decision_id, result.get("error", "unknown error"))


# ── Scheduled pipeline tick (triggered externally by cron-job.org) ──────────
#
# GitHub Actions' `schedule:` trigger is unreliable (can be delayed or skipped
# under load), so scheduling lives outside GitHub entirely: an external cron
# service (cron-job.org) POSTs here every 30 minutes. This process is
# long-lived (Cloud Run keeps it warm across requests, or a real VM keeps it
# warm always), which is also what makes Turso-backed decision state
# consistent — no more split-brain between a GitHub Actions DB copy and a
# separately-hosted app's DB copy.

CRON_SECRET = os.environ.get("CRON_SECRET")
_tick_lock = threading.Lock()


def _check_cron_secret(x_cron_secret: str | None) -> None:
    if not CRON_SECRET or x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")


def _run_tick(manager_id: int) -> None:
    """
    Runs in the background after the webhook request returns 202 — a full
    debate can take well over cron-job.org's request timeout. Guarded by a
    lock so an overlapping trigger (e.g. cron-job.org retrying a slow
    response) skips instead of racing a run already in progress.

    One run_id ties together every stage this tick touches (pipeline,
    escalation check, evaluate) in pipeline_log — see GET /runs/{run_id}
    for the full trace, or GET /runs for recent runs at a glance.
    """
    if not _tick_lock.acquire(blocking=False):
        print("[tick] previous run still in progress, skipping", file=sys.stderr)
        return
    run_id = observability.new_run_id()
    try:
        from .agents.escalation_check import check as escalation_check
        from .agents.evaluate import run_evaluate
        from .agents.pipeline import run_weekly_pipeline

        print(f"[tick] run_id={run_id}")

        try:
            print(f"[tick] pipeline: {run_weekly_pipeline(manager_id, run_id=run_id)}")
        except Exception as e:  # noqa: BLE001 — one stage failing shouldn't skip the others
            print(f"[tick] pipeline error: {e}", file=sys.stderr)

        try:
            print(f"[tick] escalation: {escalation_check(manager_id, run_id=run_id)}")
        except Exception as e:  # noqa: BLE001
            print(f"[tick] escalation error: {e}", file=sys.stderr)

        try:
            print(f"[tick] evaluate: {run_evaluate(run_id=run_id)}")
        except Exception as e:  # noqa: BLE001
            print(f"[tick] evaluate error: {e}", file=sys.stderr)
    finally:
        _tick_lock.release()


@app.post("/internal/tick")
async def internal_tick(background_tasks: BackgroundTasks, x_cron_secret: str | None = Header(default=None)):
    _check_cron_secret(x_cron_secret)
    manager_id = int(os.environ.get("FPL_MANAGER_ID", 0) or 0)
    if not manager_id:
        raise HTTPException(status_code=500, detail="FPL_MANAGER_ID not configured")
    background_tasks.add_task(_run_tick, manager_id)
    return JSONResponse(content={"accepted": True}, status_code=202)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("home.html", {
        "request": request,
        "managers": get_managers(),
        "error": None,
    })


@app.post("/audit", response_class=HTMLResponse)
async def audit(request: Request, manager_id: int = Form(...)):
    try:
        bootstrap = fetch_bootstrap()
        manager = fetch_manager_info(manager_id, bootstrap)
    except HTTPException as e:
        if e.status_code == 404:
            return templates.TemplateResponse("home.html", {
                "request": request,
                "managers": get_managers(),
                "error": "Manager ID not found. Please check and try again.",
            })
        raise

    gw = get_current_gameweek(bootstrap)
    raw_picks, _, _ = fetch_current_picks(manager_id, gw)
    fixtures = fetch_fixtures()
    player_lookup = build_player_lookup(bootstrap)
    team_lookup = build_team_lookup(bootstrap)
    strength_lookup = build_team_strength_lookup(bootstrap)
    player_ids = [p["element"] for p in raw_picks]
    recent_forms = fetch_squad_recent_forms(player_ids)
    squad = build_squad_picks(
        raw_picks, player_lookup, team_lookup, gw, fixtures, bootstrap,
        recent_forms, strength_lookup,
    )

    injury_flags: list[PlayerSummary] = [
        pick.player for pick in squad
        if pick.player.chance_of_playing_next_round is not None
        and pick.player.chance_of_playing_next_round < 75
    ]

    captain_score = derive_captain_score(squad)
    vibe_check = generate_vibe_check(manager, squad, injury_flags)
    add_manager(manager_id)

    result = AuditResult(
        manager=manager,
        squad=squad,
        vibe_check_narrative=vibe_check,
        injury_flags=injury_flags,
        captain_score=captain_score,
    )
    return templates.TemplateResponse("audit.html", {"request": request, "result": result})


@app.get("/brief/{manager_id}", response_class=HTMLResponse)
async def brief(request: Request, manager_id: int):
    result = _build_brief(manager_id)
    return templates.TemplateResponse("brief.html", {"request": request, "result": result})


@app.get("/decisions/{manager_id}", response_class=HTMLResponse)
async def decisions_view(request: Request, manager_id: int, gameweek: int | None = None):
    decisions = get_decisions(manager_id, gameweek=gameweek)
    enriched = []
    for d in decisions:
        enriched.append({
            **d,
            "conversation": get_conversation(d["id"]),
            "candidates": get_candidates_for_decision(d["id"]),
        })
    return templates.TemplateResponse("decisions.html", {
        "request": request, "manager_id": manager_id, "decisions": enriched,
    })


@app.get("/runs", response_class=HTMLResponse)
async def runs_view(request: Request):
    """Recent pipeline runs at a glance — start time, step count, any failures."""
    return templates.TemplateResponse("runs.html", {
        "request": request, "runs": get_recent_runs(limit=30), "run_id": None, "steps": None,
    })


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_trace_view(request: Request, run_id: str):
    """Full step-by-step trace for one run — every stage's status, duration,
    and structured detail (ML predictions, each agent's raw output, backstop
    validation results, execution payloads). The observability surface for
    tracing exactly where and why a run failed."""
    return templates.TemplateResponse("runs.html", {
        "request": request, "runs": None, "run_id": run_id, "steps": get_run_log(run_id),
    })


def _approve(decision_id: int) -> dict:
    """Shared by the HTTP route and the Telegram webhook — one place owns
    the approve/execute logic so both surfaces stay in sync."""
    decision = get_agent_decision(decision_id)
    if decision is None:
        return {"ok": False, "error": "Decision not found"}
    if decision["status"] != "pending_approval":
        return {"ok": False, "error": f"Decision already '{decision['status']}'"}
    set_decision_status(decision_id, "approved")
    _execute_decision(decision_id)
    return {"ok": True, "decision": get_agent_decision(decision_id)}


def _reject(decision_id: int) -> dict:
    decision = get_agent_decision(decision_id)
    if decision is None:
        return {"ok": False, "error": "Decision not found"}
    if decision["status"] != "pending_approval":
        return {"ok": False, "error": f"Decision already '{decision['status']}'"}
    set_decision_status(decision_id, "rejected")
    return {"ok": True, "decision": get_agent_decision(decision_id)}


@app.post("/api/decisions/{decision_id}/approve")
async def approve_decision(decision_id: int):
    result = _approve(decision_id)
    if not result["ok"]:
        status_code = 404 if result["error"] == "Decision not found" else 400
        raise HTTPException(status_code=status_code, detail=result["error"])
    return JSONResponse(content={"decision": result["decision"]})


@app.post("/api/decisions/{decision_id}/reject")
async def reject_decision(decision_id: int):
    result = _reject(decision_id)
    if not result["ok"]:
        status_code = 404 if result["error"] == "Decision not found" else 400
        raise HTTPException(status_code=status_code, detail=result["error"])
    return JSONResponse(content={"decision": result["decision"]})


# ── Telegram approval webhook ────────────────────────────────────────────────
# Lets you approve/reject straight from the Telegram alert instead of
# visiting /decisions/{manager_id} — tap a button, done. Registered once via
# `setWebhook` (see docs/progress.md "Deployment Setup"). Verified via the
# secret_token Telegram echoes back in a header, same shared-secret pattern
# as CRON_SECRET.

TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET")


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not TELEGRAM_WEBHOOK_SECRET or x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

    update = await request.json()
    callback = update.get("callback_query")
    if not callback:
        return JSONResponse(content={"ok": True})  # ignore non-button updates (text messages, /start, etc.)

    data = callback.get("data", "")
    callback_id = callback["id"]
    action, _, raw_id = data.partition(":")
    try:
        decision_id = int(raw_id)
    except ValueError:
        answer_telegram_callback(callback_id, "Malformed button data")
        return JSONResponse(content={"ok": True})

    if action == "approve":
        result = _approve(decision_id)
    elif action == "reject":
        result = _reject(decision_id)
    else:
        answer_telegram_callback(callback_id, f"Unknown action: {action}")
        return JSONResponse(content={"ok": True})

    answer_telegram_callback(
        callback_id,
        f"✅ Approved" if result["ok"] and action == "approve"
        else "❌ Rejected" if result["ok"]
        else f"Failed: {result['error']}",
    )
    return JSONResponse(content={"ok": True})


@app.get("/api/brief/{manager_id}")
async def api_brief(manager_id: int, refresh: bool = False):
    """
    JSON brief endpoint.
    - Returns cached brief if available (< 2 hours old, same GW).
    - Pass ?refresh=true to force regeneration.
    """
    if refresh:
        bootstrap = fetch_bootstrap()
        gw = get_current_gameweek(bootstrap)
        from .database import invalidate_brief_cache
        invalidate_brief_cache(manager_id, gw)

    result = _build_brief(manager_id)
    add_manager(manager_id)
    return JSONResponse(content=result.model_dump())
