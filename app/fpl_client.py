from __future__ import annotations

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from fastapi import HTTPException
from .models import BudgetInfo, Fixture, LeagueStanding, ManagerInfo, PlayerSummary, SquadPick
from . import cache as _cache

FPL_BASE = "https://fantasy.premierleague.com/api"


def _get(url: str) -> dict | list:
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail=f"FPL API unavailable: {e}")


def fetch_bootstrap() -> dict:
    cached = _cache.get_bootstrap()
    if cached is not None:
        return cached
    data = _get(f"{FPL_BASE}/bootstrap-static/")
    _cache.set_bootstrap(data)
    return data


def fetch_fixtures() -> list:
    cached = _cache.get_fixtures()
    if cached is not None:
        return cached
    data = _get(f"{FPL_BASE}/fixtures/")
    _cache.set_fixtures(data)
    return data


def fetch_manager_entry(manager_id: int) -> dict:
    data = _get(f"{FPL_BASE}/entry/{manager_id}/")
    if isinstance(data, dict) and data.get("detail") == "Not found.":
        raise HTTPException(status_code=404, detail="Manager ID not found")
    return data


def fetch_manager_info(manager_id: int, bootstrap: dict) -> ManagerInfo:
    data = fetch_manager_entry(manager_id)
    gw = get_current_gameweek(bootstrap)
    return ManagerInfo(
        id=manager_id,
        name=f"{data['player_first_name']} {data['player_last_name']}",
        team_name=data["name"],
        overall_rank=data.get("summary_overall_rank") or 0,
        total_points=data.get("summary_overall_points") or 0,
        current_gameweek=gw,
    )


def fetch_current_picks(manager_id: int, gameweek: int) -> tuple[list[dict], dict, str | None]:
    data = _get(f"{FPL_BASE}/entry/{manager_id}/event/{gameweek}/picks/")
    return data.get("picks", []), data.get("entry_history", {}), data.get("active_chip")


def fetch_transfer_history(manager_id: int) -> list[dict]:
    data = _get(f"{FPL_BASE}/entry/{manager_id}/transfers/")
    return data if isinstance(data, list) else []


def build_budget_info(entry_history: dict, transfer_history: list[dict], current_gw: int) -> BudgetInfo:
    transfers_made = entry_history.get("event_transfers") or 0
    # Count transfers used last GW to determine if one was banked
    prev_gw_used = sum(1 for t in transfer_history if t.get("event") == current_gw - 1)
    available = 2 if (current_gw > 1 and prev_gw_used == 0) else 1
    free_transfers = max(0, available - transfers_made)
    return BudgetInfo(
        itb=round((entry_history.get("bank") or 0) / 10, 1),
        team_value=round((entry_history.get("value") or 0) / 10, 1),
        transfers_made=transfers_made,
        hit_cost=entry_history.get("event_transfers_cost") or 0,
        free_transfers=free_transfers,
    )


def fetch_player_gw_points(player_id: int, gameweek: int) -> int | None:
    try:
        data = _get(f"{FPL_BASE}/element-summary/{player_id}/")
        for h in data.get("history", []):
            if h["round"] == gameweek:
                return h["total_points"]
        return None
    except HTTPException:
        return None


def fetch_player_recent_form(player_id: int) -> list[int]:
    try:
        data = _get(f"{FPL_BASE}/element-summary/{player_id}/")
        history = data.get("history", [])
        pts = [h["total_points"] for h in history[-5:]]
        pts.reverse()  # newest first
        return pts
    except HTTPException:
        return []


def fetch_squad_recent_forms(player_ids: list[int]) -> dict[int, list[int]]:
    results: dict[int, list[int]] = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_id = {executor.submit(fetch_player_recent_form, pid): pid for pid in player_ids}
        for future in as_completed(future_to_id):
            pid = future_to_id[future]
            try:
                results[pid] = future.result()
            except Exception:
                results[pid] = []
    return results


def fetch_player_history_past(player_id: int) -> list[dict]:
    """
    Prior-season summaries (season_name, total_points, minutes, goals, assists, etc.)
    from the same element-summary endpoint already called for recent form — free,
    just unused fields until now. Used as the "last season form" ML feature.
    """
    try:
        data = _get(f"{FPL_BASE}/element-summary/{player_id}/")
        return data.get("history_past", [])
    except HTTPException:
        return []


def build_team_form(fixtures: list[dict], current_gw: int, lookback: int = 5) -> dict[int, dict]:
    """
    team_id -> {results, goals_for, goals_against, points_per_game, goal_diff_per_game}
    over the last `lookback` finished fixtures. Derived purely from fixtures/ (already
    fetched), no new API dependency — the "team form" ML feature.
    """
    finished = [
        f for f in fixtures
        if f.get("finished") and f.get("event") is not None and f["event"] < current_gw
        and f.get("team_h_score") is not None and f.get("team_a_score") is not None
    ]
    finished.sort(key=lambda f: f["event"])

    by_team: dict[int, list[dict]] = {}
    for f in finished:
        h, a = f["team_h"], f["team_a"]
        hs, as_ = f["team_h_score"], f["team_a_score"]
        by_team.setdefault(h, []).append({"gf": hs, "ga": as_})
        by_team.setdefault(a, []).append({"gf": as_, "ga": hs})

    result: dict[int, dict] = {}
    for team_id, matches in by_team.items():
        recent = matches[-lookback:]
        gf = sum(m["gf"] for m in recent)
        ga = sum(m["ga"] for m in recent)
        points, results = 0, []
        for m in recent:
            if m["gf"] > m["ga"]:
                points += 3
                results.append("W")
            elif m["gf"] == m["ga"]:
                points += 1
                results.append("D")
            else:
                results.append("L")
        n = len(recent) or 1
        result[team_id] = {
            "results": results,
            "goals_for": gf,
            "goals_against": ga,
            "points_per_game": round(points / n, 2),
            "goal_diff_per_game": round((gf - ga) / n, 2),
        }
    return result


def fetch_league_standings(manager_id: int) -> list[LeagueStanding]:
    try:
        entry_data = fetch_manager_entry(manager_id)
    except HTTPException:
        return []

    classic_leagues = entry_data.get("leagues", {}).get("classic", [])
    # id > 314 skips FPL system leagues (overall, country, etc.)
    mini_leagues = [l for l in classic_leagues if l.get("id", 0) > 314][:4]

    standings = []
    for league in mini_leagues:
        rank = league.get("entry_rank") or 0
        if rank == 0:
            continue
        try:
            data = _get(f"{FPL_BASE}/leagues-classic/{league['id']}/standings/")
            total = data.get("standings", {}).get("count", 0)
        except HTTPException:
            total = 0
        standings.append(LeagueStanding(
            name=league["name"],
            rank=rank,
            total_managers=total,
        ))
    return standings


def build_player_lookup(bootstrap: dict) -> dict[int, dict]:
    element_types = {et["id"]: et["singular_name_short"] for et in bootstrap["element_types"]}
    lookup = {}
    for p in bootstrap["elements"]:
        p["position_label"] = element_types.get(p["element_type"], "UNK")
        lookup[p["id"]] = p
    return lookup


def build_team_lookup(bootstrap: dict) -> dict[int, str]:
    return {t["id"]: t["short_name"] for t in bootstrap["teams"]}


def _norm_strength(value, fallback) -> float:
    """
    Normalizes a team strength field onto FPL's 1-5 tier scale.

    FPL has served strength on two different scales — a 1000-1400 rating
    historically, and 1-5 tiers now — and currently returns a literal 0 for
    every strength_attack_* / strength_defence_* field. Because the KEY is
    present, `t.get(key, 1100)` never fired its default: the lookup produced
    zeros, _directional_fdr computed `(0 - 0) / 100 = 0`, and every directional
    FDR silently collapsed to the base FDR. Treat 0/None as absent and fall
    back to the overall rating, which FPL does populate.
    """
    v = float(value or 0)
    if v <= 0:
        v = float(fallback or 3)
    if v > 10:  # legacy 1000-1400 rating
        v = (v - 900) / 100.0
    return max(1.0, min(5.0, v))


def build_team_strength_lookup(bootstrap: dict) -> dict[int, dict]:
    """team_id -> {attack_home, attack_away, defence_home, defence_away, overall_home, overall_away}, all on a 1-5 scale."""
    lookup = {}
    for t in bootstrap["teams"]:
        home, away = t.get("strength_overall_home"), t.get("strength_overall_away")
        lookup[t["id"]] = {
            "attack_home":   _norm_strength(t.get("strength_attack_home"),   home),
            "attack_away":   _norm_strength(t.get("strength_attack_away"),   away),
            "defence_home":  _norm_strength(t.get("strength_defence_home"),  home),
            "defence_away":  _norm_strength(t.get("strength_defence_away"),  away),
            "overall_home":  _norm_strength(home, 3),
            "overall_away":  _norm_strength(away, 3),
        }
    return lookup


def _directional_fdr(
    player_position: str,
    own_team_id: int,
    opp_team_id: int,
    venue: str,
    strength: dict[int, dict],
    base_fdr: int,
) -> float:
    """
    Position-aware difficulty.
    Attacking assets (MID/FWD, attacking DEF): opp defence strength matters.
    Defensive assets (GKP + CB-type DEF): opp attack strength matters.
    Returns a 1.0–5.0 float (lower = easier). Mixes base FDR with strength delta.
    """
    own = strength.get(own_team_id)
    opp = strength.get(opp_team_id)
    if not own or not opp:
        return float(base_fdr)

    if player_position in ("MID", "FWD"):
        opp_rating = opp["defence_home"] if venue == "A" else opp["defence_away"]
        own_rating = own["attack_home"]  if venue == "H" else own["attack_away"]
    else:  # GKP, DEF — defensive return matters more (CS prob)
        opp_rating = opp["attack_home"]  if venue == "A" else opp["attack_away"]
        own_rating = own["defence_home"] if venue == "H" else own["defence_away"]

    # Ratings are on FPL's 1-5 scale (see _norm_strength), so delta spans
    # -4..+4 and 0.4 shifts the base FDR by at most ±1.6. The old /100.0 came
    # from the retired 1000-1400 rating scale and rounded to nothing once FPL
    # switched, which is half of why directional FDR stopped doing anything.
    delta = opp_rating - own_rating
    directional = base_fdr + (delta * 0.4)
    return max(1.0, min(5.0, round(directional, 2)))


def get_next_fixtures(
    team_id: int,
    current_gw: int,
    fixtures: list[dict],
    team_lookup: dict[int, str],
    n: int = 3,
    player_position: str | None = None,
    strength_lookup: dict[int, dict] | None = None,
) -> list[Fixture]:
    result = []
    for fix in fixtures:
        if fix["event"] is None or fix["event"] < current_gw:
            continue
        if fix["team_h"] == team_id:
            opp_id = fix["team_a"]
            venue = "H"
            base_fdr = fix.get("team_h_difficulty") or 3
        elif fix["team_a"] == team_id:
            opp_id = fix["team_h"]
            venue = "A"
            base_fdr = fix.get("team_a_difficulty") or 3
        else:
            continue

        directional = None
        if player_position and strength_lookup:
            directional = _directional_fdr(
                player_position, team_id, opp_id, venue, strength_lookup, base_fdr
            )

        result.append(Fixture(
            opp=team_lookup.get(opp_id, "?"),
            venue=venue,
            fdr=base_fdr,
            directional_fdr=directional,
            event=fix["event"],
        ))
        if len(result) == n:
            break
    return result


# Back-compat shim
def get_next_3_fixtures(
    team_id: int,
    current_gw: int,
    fixtures: list[dict],
    team_lookup: dict[int, str],
) -> list[Fixture]:
    return get_next_fixtures(team_id, current_gw, fixtures, team_lookup, n=3)


_YC_THRESHOLDS = (5, 10, 15)


def _build_player_summary(
    p: dict,
    team_name_lookup: dict[int, str],
    team_lookup: dict[int, str],
    current_gw: int,
    fixtures: list[dict],
    strength_lookup: dict[int, dict],
    recent_form: list[int],
) -> PlayerSummary:
    chance = p.get("chance_of_playing_next_round")
    position = p["position_label"]
    starts = int(p.get("starts") or 0)
    minutes = int(p.get("minutes") or 0)
    yc = int(p.get("yellow_cards") or 0)
    # suspension: one YC from threshold
    suspension_risk = any(yc == thr - 1 for thr in _YC_THRESHOLDS)
    # appearances proxy: starts + (minutes > 0 but not started) is harder to derive; use starts as floor
    appearances = starts  # best available proxy from bootstrap

    return PlayerSummary(
        id=p["id"],
        web_name=p["web_name"],
        team_name=team_name_lookup.get(p["team"], ""),
        position=position,
        total_points=p["total_points"],
        form=float(p.get("form") or 0),
        selected_by_percent=float(p.get("selected_by_percent") or 0),
        now_cost=round((p.get("now_cost") or 0) / 10, 1),
        ep_next=float(p.get("ep_next") or 0),
        points_per_game=float(p.get("points_per_game") or 0),
        recent_form_5gw=recent_form,
        chance_of_playing_next_round=int(chance) if chance is not None else None,
        news=p.get("news") or "",
        news_added=p.get("news_added"),
        fixtures_next_3=get_next_fixtures(
            p["team"], current_gw, fixtures, team_lookup,
            n=3, player_position=position, strength_lookup=strength_lookup,
        ),
        xg=float(p.get("expected_goals") or 0),
        xa=float(p.get("expected_assists") or 0),
        xgi_per_90=float(p.get("expected_goal_involvements_per_90") or 0),
        xgc_per_90=float(p.get("expected_goals_conceded_per_90") or 0),
        goals_scored=int(p.get("goals_scored") or 0),
        assists=int(p.get("assists") or 0),
        clean_sheets=int(p.get("clean_sheets") or 0),
        minutes=minutes,
        starts=starts,
        appearances=appearances,
        starts_pct=min(100.0, round(starts / max(current_gw - 1, 1) * 100.0, 1)),
        yellow_cards=yc,
        suspension_risk=suspension_risk,
        penalties_order=p.get("penalties_order"),
        direct_freekicks_order=p.get("direct_freekicks_order"),
        corners_order=p.get("corners_and_indirect_freekicks_order"),
        cost_change_event=int(p.get("cost_change_event") or 0),
        transfers_in_event=int(p.get("transfers_in_event") or 0),
        transfers_out_event=int(p.get("transfers_out_event") or 0),
        role_score=float(p.get("expected_goal_involvements_per_90") or 0),
    )


def build_squad_picks(
    raw_picks: list[dict],
    player_lookup: dict[int, dict],
    team_lookup: dict[int, str],
    current_gw: int,
    fixtures: list[dict],
    bootstrap: dict,
    recent_forms: dict[int, list[int]] | None = None,
    strength_lookup: dict[int, dict] | None = None,
) -> list[SquadPick]:
    team_name_lookup = {t["id"]: t["name"] for t in bootstrap["teams"]}
    if strength_lookup is None:
        strength_lookup = build_team_strength_lookup(bootstrap)
    picks = []
    for pick in raw_picks:
        p = player_lookup.get(pick["element"])
        if not p:
            continue
        player = _build_player_summary(
            p, team_name_lookup, team_lookup, current_gw, fixtures,
            strength_lookup, (recent_forms or {}).get(p["id"], []),
        )
        picks.append(SquadPick(
            player=player,
            position=pick["position"],
            multiplier=pick["multiplier"],
            is_captain=pick["is_captain"],
            is_vice_captain=pick["is_vice_captain"],
        ))
    return picks


def get_current_gameweek(bootstrap: dict) -> int:
    for event in bootstrap["events"]:
        if event["is_current"]:
            return event["id"]
    for event in bootstrap["events"]:
        if event["is_next"]:
            return event["id"]
    return 1


def get_deadline_str(bootstrap: dict) -> str:
    for event in bootstrap["events"]:
        if event["is_next"]:
            raw = event["deadline_time"]
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.strftime("%a %d %b, %H:%M UTC")
    return "Unknown"


def find_valid_replacements(
    sell_player: PlayerSummary,
    budget_max: float,           # sell_price + itb
    squad: list[SquadPick],
    player_lookup: dict[int, dict],
    team_lookup: dict[int, str],
    team_name_lookup: dict[int, str],
    current_gw: int,
    fixtures: list[dict],
    strength_lookup: dict[int, dict] | None = None,
    recently_sold_ids: set[int] | None = None,
    top_n: int = 8,
    enrich_form: bool = True,
) -> list[PlayerSummary]:
    """
    Return top_n composite-ranked replacements for sell_player.
    Filters: same FPL position, affordable, not same club, not already owned,
    not sold within last 3 GWs (if recently_sold_ids given), minutes floor.
    Ranking done by ranking.score_buy; top ~15 enriched with recent_form_5gw.
    """
    from . import ranking

    owned_ids = {pick.player.id for pick in squad}
    excluded = owned_ids | (recently_sold_ids or set())
    position = sell_player.position
    exclude_club = sell_player.team_name

    # Build summaries for all feasible candidates (no form yet — expensive)
    feasible: list[PlayerSummary] = []
    for pid, p in player_lookup.items():
        if pid in excluded:
            continue
        if p.get("position_label") != position:
            continue
        club_name = team_name_lookup.get(p["team"], "")
        if club_name == exclude_club:
            continue
        price = round((p.get("now_cost") or 0) / 10, 1)
        if price > budget_max:
            continue
        # minutes floor — skip pure bench fodder (unless early season)
        minutes = int(p.get("minutes") or 0)
        if current_gw > 6 and minutes < (current_gw - 1) * 30:
            continue
        chance = p.get("chance_of_playing_next_round")
        if chance is not None and chance < 50:
            continue
        summary = _build_player_summary(
            p,
            team_name_lookup,
            team_lookup,
            current_gw,
            fixtures,
            strength_lookup or {},
            [],  # form added later for top-N
        )
        feasible.append(summary)

    if not feasible:
        return []

    # First-pass rank by composite (no form trend yet)
    feasible.sort(key=lambda x: ranking.score_buy(x, sell_player), reverse=True)
    shortlist = feasible[: max(top_n * 2, 15)]

    # Enrich shortlist with recent_form_5gw (parallel fetch)
    if enrich_form and shortlist:
        forms = fetch_squad_recent_forms([c.id for c in shortlist])
        for c in shortlist:
            c.recent_form_5gw = forms.get(c.id, [])

    # Re-rank with form trend available
    shortlist.sort(key=lambda x: ranking.score_buy(x, sell_player), reverse=True)
    return shortlist[:top_n]


def detect_dgw_bgw(
    bootstrap: dict, fixtures: list[dict], gameweek: int
) -> tuple[list[int], list[int]]:
    all_teams = {t["id"] for t in bootstrap["teams"]}
    gw_fixtures = [f for f in fixtures if f["event"] == gameweek]
    team_counts: dict[int, int] = {t: 0 for t in all_teams}
    for fix in gw_fixtures:
        team_counts[fix["team_h"]] = team_counts.get(fix["team_h"], 0) + 1
        team_counts[fix["team_a"]] = team_counts.get(fix["team_a"], 0) + 1
    dgw = [tid for tid, count in team_counts.items() if count > 1]
    bgw = [tid for tid, count in team_counts.items() if count == 0]
    return dgw, bgw
