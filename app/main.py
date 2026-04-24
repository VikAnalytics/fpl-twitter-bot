from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .database import (
    init_db, get_managers, add_manager,
    get_brief_cache, set_brief_cache,
    get_rate_limit_count, increment_rate_limit, DAILY_BRIEF_LIMIT,
    save_transfer_suggestions, get_unevaluated_suggestions,
    save_transfer_outcome, get_recent_outcomes,
)
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
    allow_origins=["chrome-extension://*", "http://localhost:*", "https://fantasy.premierleague.com"],
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


@app.get("/api/brief/{manager_id}")
async def api_brief(manager_id: int, refresh: bool = False):
    """
    JSON endpoint for the Chrome extension.
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
