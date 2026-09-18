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

import math

from ..models import Fixture, PlayerSummary

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
    # v4 — the model had no idea what position a player was, so a defender's
    # prediction was an attacker's prediction with less xGI; clean sheets,
    # saves and defensive contributions were invisible to it.
    "pos_gkp",
    "pos_def",
    "pos_mid",
    "pos_fwd",
    "cs_prob_gw",       # P(clean sheet) this fixture, from xGC/90 and the opponent
    "saves_per_90",
    "dc_per_90",
]

LEAGUE_AVG_GOALS_CONCEDED = 1.4   # per game; the prior before a player has 90 minutes of xGC


def fixture_goal_factor(directional_fdr: float | None, base_fdr: float = 3.0) -> float:
    """Scale a per-90 concession rate by the fixture: directional FDR 3 is
    par, 1 is ~0.6x (weak attack), 5 is ~1.4x (strong attack)."""
    d = directional_fdr if directional_fdr is not None else float(base_fdr)
    return 0.4 + 0.2 * d


def concession_rate(xgc_per_90: float, minutes: float) -> float:
    return xgc_per_90 if (xgc_per_90 > 0 and minutes >= 90) else LEAGUE_AVG_GOALS_CONCEDED


def clean_sheet_prob(xgc_per_90: float, minutes: float, directional_fdr: float | None, base_fdr: float = 3.0) -> float:
    """Poisson zero on the fixture-scaled concession rate. THE definition —
    used by training, live inference and ranking alike."""
    return math.exp(-concession_rate(xgc_per_90, minutes) * fixture_goal_factor(directional_fdr, base_fdr))


def position_one_hot(position: str) -> dict[str, float]:
    return {
        "pos_gkp": 1.0 if position == "GKP" else 0.0,
        "pos_def": 1.0 if position == "DEF" else 0.0,
        "pos_mid": 1.0 if position == "MID" else 0.0,
        "pos_fwd": 1.0 if position == "FWD" else 0.0,
    }


def opponent_strength(opp_entry: dict | None, player_venue: str) -> float:
    """
    Strength of the OPPONENT in the upcoming fixture, on the 1-5 scale
    build_team_strength_lookup normalizes to. Higher = harder opponent.

    THE single definition, imported by both app/ml/train.py and the live
    inference path, because they disagreed badly before: training passed
    `opponent_team` id / 20 (an alphabetical index, not a strength) while
    inference passed the player's OWN team strength — and that was 0.0 for
    every player anyway, since FPL had started serving zeros. The feature was
    noise in training and a constant at inference.

    `player_venue` is the PLAYER's venue, so the opponent's rating is the
    mirror of it: the player at home faces an opponent playing away.
    """
    if not opp_entry:
        return 3.0
    return float(opp_entry["overall_away" if player_venue == "H" else "overall_home"])


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
    gw_fixture: Fixture | None = None,
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

    fixture = gw_fixture or (player.fixtures_next_3[0] if player.fixtures_next_3 else None)
    cs_prob = clean_sheet_prob(
        player.xgc_per_90, player.minutes,
        fixture.directional_fdr if fixture else None, float(fixture.fdr) if fixture else 3.0,
    )

    return {
        **position_one_hot(player.position),
        "cs_prob_gw": cs_prob,
        "saves_per_90": player.saves_per_90,
        "dc_per_90": player.dc_per_90,
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
