"""
Feature schema for the expected-points model.

Five named factors from the product spec, mapped to concrete fields:
- player_form         -> form
- team_form           -> team_ppg, team_gd_pg
- opponent            -> opponent_strength
- last_season_form    -> last_season_pts_per90
- latest_news         -> chance_of_playing

Plus supporting signals already computed elsewhere in the app (xgi_per_90,
xgc_per_90, avg_fdr, starts_pct, ep_next) reused rather than recomputed.
"""
from __future__ import annotations

from .. import ranking
from ..models import PlayerSummary

FEATURE_NAMES = [
    "form",
    "team_ppg",
    "team_gd_pg",
    "opponent_strength",
    "last_season_pts_per90",
    "chance_of_playing",
    "xgi_per_90",
    "xgc_per_90",
    "avg_fdr",
    "starts_pct",
    "ep_next",
]


def build_feature_row(inputs: dict) -> dict:
    """Coerce an inputs dict into the fixed feature schema (missing -> 0.0)."""
    return {name: float(inputs.get(name) or 0.0) for name in FEATURE_NAMES}


def feature_vector(row: dict) -> list[float]:
    return [row[name] for name in FEATURE_NAMES]


def build_live_inputs(
    player: PlayerSummary,
    team_form: dict,
    opponent_strength_value: float,
    history_past: list[dict],
) -> dict:
    """
    Assemble a feature-input dict for a live player at inference time.
    `team_form` is one entry from fpl_client.build_team_form().
    `opponent_strength_value` is a single normalized scalar (0-1-ish) for the
    upcoming fixture, precomputed by the caller from build_team_strength_lookup.
    `history_past` is fpl_client.fetch_player_history_past()'s raw list.
    """
    last_season_pts_per90 = 0.0
    if history_past:
        last = history_past[-1]
        minutes = last.get("minutes") or 0
        pts = last.get("total_points") or 0
        if minutes > 0:
            last_season_pts_per90 = round((pts / minutes) * 90, 3)

    chance = player.chance_of_playing_next_round
    avg_fdr = 3.0
    if player.fixtures_next_3:
        vals = [f.directional_fdr if f.directional_fdr is not None else float(f.fdr) for f in player.fixtures_next_3]
        avg_fdr = sum(vals) / len(vals)

    return {
        "form": player.form,
        "team_ppg": team_form.get("points_per_game", 1.0) if team_form else 1.0,
        "team_gd_pg": team_form.get("goal_diff_per_game", 0.0) if team_form else 0.0,
        "opponent_strength": opponent_strength_value,
        "last_season_pts_per90": last_season_pts_per90,
        "chance_of_playing": (chance if chance is not None else 100) / 100.0,
        "xgi_per_90": player.xgi_per_90,
        "xgc_per_90": player.xgc_per_90,
        "avg_fdr": avg_fdr,
        "starts_pct": player.starts_pct,
        "ep_next": player.ep_next,
    }
