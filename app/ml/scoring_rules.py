"""
Explicit encoding of FPL's current scoring rules — verified against the live
FPL rules page and cross-checked against real 2025-26 gameweek rows (see
docs/progress.md for the validation samples: Matz Sels 2pts, Petrović 2pts,
Nick Pope 9pts all matched exactly).

Why this exists: the training pipeline used to trust a historical row's own
`total_points` as the label. But FPL changes scoring rules between seasons
(2025-26 introduced defensive contribution points and raised goalkeeper goal
value to 10), so a season's `total_points` reflects whatever rules were live
THEN, not necessarily now. Recomputing points from raw match stats via this
one explicit rule table means the training label is always consistent with
ONE current rule set — update this file when FPL changes the rules, rather
than silently retraining on a mix of old and new scoring regimes.

Source: fantasy.premierleague.com/help/rules, cross-checked via
premierleague.com's "What's new in 2025/26" scoring articles. Review this
file whenever FPL announces a scoring change.
"""
from __future__ import annotations

GOAL_POINTS = {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}
CLEAN_SHEET_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
ASSIST_POINTS = 3
PENALTY_SAVE_POINTS = 5
PENALTY_MISS_POINTS = -2
YELLOW_CARD_POINTS = -1
RED_CARD_POINTS = -3
OWN_GOAL_POINTS = -2

# Defensive contribution (new 2025-26 rule): 2 points if the combined
# defensive-action count clears the position's threshold. FPL's own API
# already pre-combines the right stats per position into a single
# `defensive_contribution` field (verified against real rows: a defender
# with 6 clearances/blocks/interceptions + 2 tackles = 8, matching the
# field's value exactly), so no manual summing is needed here.
DEFENSIVE_CONTRIBUTION_THRESHOLD = {"DEF": 10, "MID": 12, "FWD": 12}
DEFENSIVE_CONTRIBUTION_POINTS = 2

# vaastav's historical CSVs (and, historically, some FPL API responses) use
# "GK" rather than the "GKP" used elsewhere in this app (app/ranking.py,
# app/models.py). Normalize before doing anything position-dependent.
_POSITION_ALIASES = {"GK": "GKP"}


def normalize_position(position: str) -> str:
    return _POSITION_ALIASES.get(position, position)


def compute_points(position: str, stats: dict) -> int:
    """
    stats keys (all optional, default 0/False): minutes, goals_scored,
    assists, clean_sheets (0/1), goals_conceded, saves, penalties_saved,
    penalties_missed, yellow_cards, red_cards, own_goals, bonus,
    defensive_contribution.
    """
    pos = normalize_position(position)

    def _int(key: str) -> int:
        try:
            return int(float(stats.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    minutes = _int("minutes")
    points = 0

    # Appearance
    if minutes >= 60:
        points += 2
    elif minutes > 0:
        points += 1

    # Goals / assists
    points += _int("goals_scored") * GOAL_POINTS.get(pos, 4)
    points += _int("assists") * ASSIST_POINTS

    # Clean sheet (only meaningful with 60+ minutes played)
    if minutes >= 60 and _int("clean_sheets") > 0:
        points += CLEAN_SHEET_POINTS.get(pos, 0)

    # Goals conceded (GKP/DEF only, -1 per 2 conceded). Unlike the clean
    # sheet bonus, this is NOT gated behind 60+ minutes — verified against
    # real rows (e.g. a keeper sent off after 4 minutes with the team
    # having conceded 2 still took the -1 hit).
    if pos in ("GKP", "DEF") and minutes > 0:
        points -= _int("goals_conceded") // 2

    # Saves (GKP only, 1pt per 3 saves)
    if pos == "GKP":
        points += _int("saves") // 3

    # Penalties
    points += _int("penalties_saved") * PENALTY_SAVE_POINTS
    points += _int("penalties_missed") * PENALTY_MISS_POINTS

    # Cards / own goals
    points += _int("yellow_cards") * YELLOW_CARD_POINTS
    points += _int("red_cards") * RED_CARD_POINTS
    points += _int("own_goals") * OWN_GOAL_POINTS

    # Defensive contribution (DEF/MID/FWD only — not a GKP mechanic)
    threshold = DEFENSIVE_CONTRIBUTION_THRESHOLD.get(pos)
    if threshold is not None and _int("defensive_contribution") >= threshold:
        points += DEFENSIVE_CONTRIBUTION_POINTS

    # Bonus points are awarded post-match from a relative BPS ranking among
    # all players in the fixture — not reconstructable from a single row's
    # stats, so it's taken as given rather than recomputed.
    points += _int("bonus")

    return points
