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


def fetch_current_picks(manager_id: int, gameweek: int) -> tuple[list[dict], dict]:
    data = _get(f"{FPL_BASE}/entry/{manager_id}/event/{gameweek}/picks/")
    return data.get("picks", []), data.get("entry_history", {})


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


def get_next_3_fixtures(
    team_id: int,
    current_gw: int,
    fixtures: list[dict],
    team_lookup: dict[int, str],
) -> list[Fixture]:
    result = []
    for fix in fixtures:
        if fix["event"] is None or fix["event"] < current_gw:
            continue
        if fix["team_h"] == team_id:
            result.append(Fixture(
                opp=team_lookup.get(fix["team_a"], "?"),
                venue="H",
                fdr=fix.get("team_h_difficulty") or 3,
            ))
        elif fix["team_a"] == team_id:
            result.append(Fixture(
                opp=team_lookup.get(fix["team_h"], "?"),
                venue="A",
                fdr=fix.get("team_a_difficulty") or 3,
            ))
        if len(result) == 3:
            break
    return result


def build_squad_picks(
    raw_picks: list[dict],
    player_lookup: dict[int, dict],
    team_lookup: dict[int, str],
    current_gw: int,
    fixtures: list[dict],
    bootstrap: dict,
    recent_forms: dict[int, list[int]] | None = None,
) -> list[SquadPick]:
    team_name_lookup = {t["id"]: t["name"] for t in bootstrap["teams"]}
    picks = []
    for pick in raw_picks:
        p = player_lookup.get(pick["element"])
        if not p:
            continue
        chance = p.get("chance_of_playing_next_round")
        player = PlayerSummary(
            id=p["id"],
            web_name=p["web_name"],
            team_name=team_name_lookup.get(p["team"], ""),
            position=p["position_label"],
            total_points=p["total_points"],
            form=float(p.get("form") or 0),
            selected_by_percent=float(p.get("selected_by_percent") or 0),
            now_cost=round((p.get("now_cost") or 0) / 10, 1),
            ep_next=float(p.get("ep_next") or 0),
            points_per_game=float(p.get("points_per_game") or 0),
            recent_form_5gw=(recent_forms or {}).get(p["id"], []),
            chance_of_playing_next_round=int(chance) if chance is not None else None,
            news=p.get("news") or "",
            fixtures_next_3=get_next_3_fixtures(p["team"], current_gw, fixtures, team_lookup),
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
    top_n: int = 8,
) -> list[PlayerSummary]:
    """
    Return top_n valid in-market replacements for sell_player.
    Filters: same FPL position, affordable, not same club, not already owned.
    """
    owned_ids = {pick.player.id for pick in squad}
    position = sell_player.position
    exclude_club = sell_player.team_name

    candidates: list[PlayerSummary] = []
    for pid, p in player_lookup.items():
        if pid in owned_ids:
            continue
        if p.get("position_label") != position:
            continue
        club_name = team_name_lookup.get(p["team"], "")
        if club_name == exclude_club:
            continue
        price = round((p.get("now_cost") or 0) / 10, 1)
        if price > budget_max:
            continue
        ep = float(p.get("ep_next") or 0)
        short_team = team_lookup.get(p["team"], "?")
        fixtures_next_3 = get_next_3_fixtures(p["team"], current_gw, fixtures, team_lookup)
        candidates.append(PlayerSummary(
            id=pid,
            web_name=p["web_name"],
            team_name=club_name,
            position=position,
            total_points=p.get("total_points", 0),
            form=float(p.get("form") or 0),
            selected_by_percent=float(p.get("selected_by_percent") or 0),
            now_cost=price,
            ep_next=ep,
            points_per_game=float(p.get("points_per_game") or 0),
            recent_form_5gw=[],
            fixtures_next_3=fixtures_next_3,
        ))

    candidates.sort(key=lambda x: x.ep_next, reverse=True)
    return candidates[:top_n]


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
