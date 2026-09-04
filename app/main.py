from __future__ import annotations

import os
import sys
import threading

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .database import (
    init_db,
    get_agent_decision, set_decision_status,
    get_decisions, get_conversation, get_candidates_for_decision,
    get_run_log, get_recent_runs,
)
from . import observability
from .fpl_auth import FplAuthError, fetch_my_team, get_access_token, set_lineup, submit_transfers
from .notify import answer_telegram_callback, escalate_execution_failure, send_telegram_alert
from .fpl_client import (
    build_player_lookup,
    fetch_bootstrap,
)

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


# ── Autonomous decision execution ────────────────────────────────────────────

def _execute_transfer_decision(decision: dict, run_id: str) -> dict:
    manager_id, gw = decision["manager_id"], decision["gameweek"]
    transfers = decision["proposal"].get("transfers", [])
    if not transfers:
        return {"ok": True, "note": "no transfers to execute"}

    try:
        with observability.step(run_id, "execution.login", gameweek=gw, manager_id=manager_id, decision_id=decision["id"]):
            bootstrap = fetch_bootstrap()
            player_lookup = build_player_lookup(bootstrap)
            access_token = get_access_token()
            my_team = fetch_my_team(access_token, manager_id)
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
        result = submit_transfers(access_token, manager_id, gw, payload)
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


def _reconcile_lineup(manager_id: int, gw: int, my_team_picks: list[dict], run_id: str) -> tuple[dict, dict]:
    """
    Recomputes the XI, captain and bench from the squad FPL actually holds.

    Called when the approved XI names players who aren't in the live squad —
    the transfer was rejected after the lineup was projected onto it, or a
    manual move happened. Submitting the stale payload would send 15 element
    ids that don't match the squad, so recompute rather than fail the pick.
    """
    from .agents.pipeline import _predicted_points_for
    from .fpl_client import (
        build_squad_picks, build_team_form, build_team_lookup, build_team_strength_lookup,
        fetch_fixtures, fetch_squad_recent_forms,
    )
    from . import ranking

    bootstrap = fetch_bootstrap()
    fixtures = fetch_fixtures()
    player_lookup = build_player_lookup(bootstrap)
    strength_lookup = build_team_strength_lookup(bootstrap)
    team_id_by_name = {t["name"]: t["id"] for t in bootstrap["teams"]}

    # /my-team/ mirrors /picks/, but default the slot fields anyway — they're
    # about to be recomputed and only `element` has to be right.
    raw_picks = [
        {
            "element": p["element"],
            "position": p.get("position", i + 1),
            "multiplier": p.get("multiplier", 1),
            "is_captain": p.get("is_captain", False),
            "is_vice_captain": p.get("is_vice_captain", False),
        }
        for i, p in enumerate(my_team_picks)
    ]
    squad = build_squad_picks(
        raw_picks, player_lookup, build_team_lookup(bootstrap), gw, fixtures, bootstrap,
        fetch_squad_recent_forms([p["element"] for p in raw_picks]), strength_lookup,
    )
    players = [pk.player for pk in squad]
    preds = _predicted_points_for(
        players, build_team_form(fixtures, gw), strength_lookup, team_id_by_name, gw
    )

    # Same fixture weighting the pipeline used, or a reconcile would silently
    # produce a different XI than the one that was approved.
    weighted = ranking.apply_fixture_weighting(players, preds, gw)
    selection = ranking.select_best_xi(players, weighted)
    cap = ranking.score_captain(selection.starting, weighted)
    bench_order = ranking.order_bench(selection.bench, weighted)

    cap_proposal = {
        "captain": cap.player.web_name, "captain_id": cap.player.id,
        "vice": cap.vice.web_name, "vice_id": cap.vice.id, "rationale": cap.rationale,
    }
    lineup_proposal = {
        "formation": selection.formation,
        "starting_xi": [{"player": p.web_name, "player_id": p.id} for p in selection.starting],
        "bench_order": [
            {"player": b.player.web_name, "player_id": b.player.id, "order": b.order} for b in bench_order
        ],
    }
    return cap_proposal, lineup_proposal


def _execute_lineup_decisions(captain_decision: dict, lineup_decision: dict, run_id: str) -> dict:
    manager_id, gw = lineup_decision["manager_id"], lineup_decision["gameweek"]
    try:
        with observability.step(run_id, "execution.login", gameweek=gw, manager_id=manager_id):
            access_token = get_access_token()
            my_team = fetch_my_team(access_token, manager_id)
    except (FplAuthError, Exception) as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}

    # The XI was chosen before the transfer resolved. Check it against the squad
    # FPL holds right now and recompute if they've diverged, rather than submit
    # element ids that are no longer in the squad.
    live_ids = {p["element"] for p in my_team.get("picks", [])}
    proposed_ids = (
        {p["player_id"] for p in lineup_decision["proposal"].get("starting_xi", [])}
        | {b["player_id"] for b in lineup_decision["proposal"].get("bench_order", [])}
    )
    reconciled = False
    if live_ids and proposed_ids != live_ids:
        with observability.step(run_id, "execution.reconcile_lineup", gameweek=gw, manager_id=manager_id) as ctx:
            cap_proposal, lineup_proposal = _reconcile_lineup(manager_id, gw, my_team.get("picks", []), run_id)
            captain_decision = {**captain_decision, "proposal": cap_proposal}
            lineup_decision = {**lineup_decision, "proposal": lineup_proposal}
            reconciled = True
            ctx["detail"] = {
                "reason": "approved XI does not match live squad",
                "missing_from_live": sorted(proposed_ids - live_ids),
                "unassigned_in_live": sorted(live_ids - proposed_ids),
                "recomputed_captain": cap_proposal["captain"],
                "recomputed_xi": [p["player"] for p in lineup_proposal["starting_xi"]],
            }

    with observability.step(run_id, "execution.set_lineup", gameweek=gw, manager_id=manager_id) as ctx:
        picks = _build_lineup_payload(captain_decision, lineup_decision)
        result = set_lineup(access_token, manager_id, picks)
        ctx["detail"] = {"picks": picks, "result": result, "reconciled": reconciled}

    if reconciled and result.get("ok"):
        # Never silently diverge from what was approved.
        send_telegram_alert(
            f"♻️ GW{gw} lineup recomputed at submission — your approved XI no longer matched "
            f"your actual squad (transfer rejected or squad changed manually).\n"
            f"Submitted XI: {', '.join(p['player'] for p in lineup_decision['proposal']['starting_xi'])}\n"
            f"Captain: {captain_decision['proposal']['captain']} "
            f"(vice: {captain_decision['proposal']['vice']})"
        )
    return result


def _pending_transfer(manager_id: int, gameweek: int) -> dict | None:
    """
    The gameweek's transfer decision if it hasn't reached a terminal state yet.

    Picks must not be submitted before it resolves. Submit first and FPL
    auto-places the incoming player when the transfer lands (a player bought to
    start can end up on the bench); submit after with the pre-transfer XI and
    the payload names a player no longer in the squad.
    """
    for d in get_decisions(manager_id, gameweek, "transfer"):
        if d["status"] in ("pending_approval", "approved"):
            return d
    return None


def _try_execute_lineup_pair(manager_id: int, gameweek: int, run_id: str) -> None:
    """
    Submits the combined captain+lineup payload once, when every precondition
    holds: both halves approved, and the gameweek's transfer decision settled.
    Safe to call repeatedly — it no-ops until then, which is what lets the
    transfer's own execution (or rejection) trigger the picks that were
    approved earlier and parked.
    """
    captains = get_decisions(manager_id, gameweek, "captain")
    lineups = get_decisions(manager_id, gameweek, "lineup")
    captain_decision = captains[0] if captains else None
    lineup_decision = lineups[0] if lineups else None
    if captain_decision is None or lineup_decision is None:
        return
    if captain_decision["status"] != "approved" or lineup_decision["status"] != "approved":
        return  # waiting on the other half of the pair (or already submitted)

    blocking = _pending_transfer(manager_id, gameweek)
    if blocking is not None:
        print(
            f"[lineup] GW{gameweek} picks held — transfer #{blocking['id']} still "
            f"'{blocking['status']}'",
            file=sys.stderr,
        )
        return

    result = _execute_lineup_decisions(captain_decision, lineup_decision, run_id)
    status = "executed" if result.get("ok") else "failed"
    set_decision_status(captain_decision["id"], status)
    set_decision_status(lineup_decision["id"], status)
    if not result.get("ok"):
        escalate_execution_failure(gameweek, lineup_decision["id"], result.get("error", "unknown error"))


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

    The picks additionally wait on the TRANSFER decision, because the XI is
    chosen for the post-transfer squad — so a transfer executing (or being
    rejected) is itself a trigger to retry the parked picks. Approval order no
    longer matters: whichever resolves last fires the submission.
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
        # The squad is now settled — release any picks parked behind this.
        _try_execute_lineup_pair(decision["manager_id"], decision["gameweek"], run_id)
        return

    if decision["decision_type"] in ("captain", "lineup"):
        _try_execute_lineup_pair(decision["manager_id"], decision["gameweek"], run_id)


# ── Scheduled pipeline tick (triggered externally by cron-job.org) ──────────
#
# GitHub Actions' `schedule:` trigger is unreliable (can be delayed or skipped
# under load), so scheduling lives outside GitHub entirely: an external cron
# service (cron-job.org) POSTs here every 30 minutes.
#
# Runs synchronously, in-request — NOT via FastAPI BackgroundTasks. Cloud Run
# only allocates CPU while a request is actively being served; a background
# task kicked off after the response is sent gets starved of CPU (or the
# instance is torn down entirely) and silently never finishes. Confirmed live:
# every tick before this fix died after logging `ml_predict` "started" with no
# "succeeded"/"failed" ever following, and zero decisions were ever produced.
# Running the work inside the request keeps Cloud Run's default CPU
# allocation active for the whole duration — costs the same compute-seconds
# either way, just actually completes. Cloud Run's request timeout is raised
# to cover a full debate run (see deploy notes / `--timeout`).

CRON_SECRET = os.environ.get("CRON_SECRET")
_tick_lock = threading.Lock()


def _check_cron_secret(x_cron_secret: str | None) -> None:
    if not CRON_SECRET or x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")


def _run_tick(manager_id: int, force: bool = False) -> dict:
    """
    Guarded by a lock so an overlapping trigger (e.g. cron-job.org retrying a
    slow response) skips instead of racing a run already in progress.

    One run_id ties together every stage this tick touches (pipeline,
    escalation check, evaluate) in pipeline_log — see GET /runs/{run_id}
    for the full trace, or GET /runs for recent runs at a glance.
    """
    if not _tick_lock.acquire(blocking=False):
        print("[tick] previous run still in progress, skipping", file=sys.stderr)
        return {"skipped": "previous run still in progress"}
    run_id = observability.new_run_id()
    result = {"run_id": run_id}
    try:
        from .agents.escalation_check import check as escalation_check
        from .agents.evaluate import run_evaluate
        from .agents.pipeline import run_weekly_pipeline

        print(f"[tick] run_id={run_id}")

        try:
            result["pipeline"] = run_weekly_pipeline(manager_id, run_id=run_id, force=force)
            print(f"[tick] pipeline: {result['pipeline']}")
        except Exception as e:  # noqa: BLE001 — one stage failing shouldn't skip the others
            result["pipeline_error"] = str(e)
            print(f"[tick] pipeline error: {e}", file=sys.stderr)

        try:
            result["escalation"] = escalation_check(manager_id, run_id=run_id)
            print(f"[tick] escalation: {result['escalation']}")
        except Exception as e:  # noqa: BLE001
            result["escalation_error"] = str(e)
            print(f"[tick] escalation error: {e}", file=sys.stderr)

        try:
            result["evaluate"] = run_evaluate(run_id=run_id)
            print(f"[tick] evaluate: {result['evaluate']}")
        except Exception as e:  # noqa: BLE001
            result["evaluate_error"] = str(e)
            print(f"[tick] evaluate error: {e}", file=sys.stderr)
    finally:
        _tick_lock.release()
    return result


@app.post("/internal/tick")
async def internal_tick(
    force: bool = False,
    x_cron_secret: str | None = Header(default=None),
):
    _check_cron_secret(x_cron_secret)
    manager_id = int(os.environ.get("FPL_MANAGER_ID", 0) or 0)
    if not manager_id:
        raise HTTPException(status_code=500, detail="FPL_MANAGER_ID not configured")
    result = _run_tick(manager_id, force=force)
    return JSONResponse(content=result)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    manager_id = int(os.environ.get("FPL_MANAGER_ID", 0) or 0)
    return templates.TemplateResponse("home.html", {
        "request": request,
        "manager_id": manager_id or None,
        "recent_runs": get_recent_runs(limit=5),
    })


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
    if decision["decision_type"] == "transfer":
        # Squad settled (unchanged) — release picks parked behind this transfer.
        # _execute_lineup_decisions reconciles the XI, which was projected onto
        # a transfer that is now not happening.
        _try_execute_lineup_pair(decision["manager_id"], decision["gameweek"], observability.new_run_id())
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
