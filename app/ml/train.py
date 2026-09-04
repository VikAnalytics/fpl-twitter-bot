"""
Offline training script for the expected-points model.

Data source: vaastav/Fantasy-Premier-League (github.com/vaastav/Fantasy-Premier-League),
a free, MIT-licensed, community-standard mirror of multi-season FPL gameweek data —
the one free resource this app needed but didn't have (everything else comes from
the live FPL API already called elsewhere in the app).

Run: python -m app.ml.train

Promotion gate: a newly trained model only overwrites model.pkl if its holdout MAE
isn't meaningfully worse (>5%) than the currently deployed model's — prevents a bad
weekly training run from silently degrading prediction quality. The gate is skipped
when FEATURE_SCHEMA_VERSION changes, because MAE from two different feature
definitions isn't a comparison; leaving it in place meant a leaky v1 model
(0.641, achieved by reading the target gameweek's own ict_index) permanently
outranked every honest successor. Meta records the FPL xP baseline alongside, so
"is this model worth running at all" stays answerable.

Scoring-rule robustness: FPL changes its scoring rules between seasons (2025-26
added defensive contribution points and raised goalkeeper goal value to 10) — a
historical row's own `total_points` reflects whatever rules were live at the time,
not necessarily now. Labels here are recomputed from raw match stats via
app/ml/scoring_rules.py's explicit, verified rule table instead of trusted
as-is, so training stays consistent under ONE current rule regime. Also why
SEASONS defaults to just the most recently completed season rather than several —
combining multiple rule regimes (even after recomputing) still means the
*non-scoring* features (a player's role, a team's tactics) are less comparable
across years than within the same season. Update scoring_rules.py, not this
comment, when FPL announces a new rule change.

Feature parity: every feature is reconstructed to mean the same thing here as
it does at inference (app/ml/features.build_live_inputs), by joining teams.csv
and fixtures.csv per season. It did not used to, and the gap was not a matter
of approximation — `ep_next` was trained on the player's PRICE, `xgi_per_90` on
ict_index/10, and `opponent_strength` on the opponent's team id divided by 20,
while team_ppg, team_gd_pg, xgc_per_90, last_season_pts_per90 and avg_fdr were
frozen constants. Only `form` and `starts_pct` were approximately honest, and
the resulting model predicted 0.21 points for a first-choice goalkeeper.

The one feature still not reconstructable is `chance_of_playing`: the CSVs
carry no injury flag. It is held constant so the model never splits on it,
which makes it inert at inference rather than actively misleading. Availability
is applied outside the model instead (ranking.score_captain, order_bench).

If you add a feature, add it in BOTH places or leave it out of FEATURE_NAMES.
"""
from __future__ import annotations

import csv
import io
import json
import pickle
import statistics
import sys
from pathlib import Path

import requests
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import train_test_split

from ..fpl_client import _directional_fdr, _norm_strength
from .features import FEATURE_NAMES, build_feature_row, feature_vector, opponent_strength
from .scoring_rules import compute_points, normalize_position

VAASTAV_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
# Most recently completed season only — see "Scoring-rule robustness" above.
# Update this each close season (e.g. to ["2026-27"] once that one finishes).
SEASONS = ["2025-26"]

# Bump when a feature's MEANING changes, not merely when the name list changes.
# The promotion gate compares holdout MAE against the deployed model, and that
# comparison is meaningless across two different feature definitions — v1 scored
# 0.641 only because `xgi_per_90` was the row's OWN ict_index, i.e. same-gameweek
# leakage, and it would have permanently blocked every honest model that followed.
# v3 also narrowed the TARGET POPULATION to gameweeks the player featured in,
# which moves MAE onto a different, higher-variance distribution — again not
# comparable with the previous number.
FEATURE_SCHEMA_VERSION = 3

# Used only to source the `last_season_pts_per90` feature.
_SEASON_BEFORE = {"2025-26": "2024-25", "2026-27": "2025-26"}

MODEL_PATH = Path(__file__).parent / "model.pkl"
META_PATH = Path(__file__).parent / "model_meta.json"


def _fetch_season_gws(season: str) -> list[dict]:
    url = f"{VAASTAV_BASE}/{season}/gws/merged_gw.csv"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[train] skipping {season}: {e}", file=sys.stderr)
        return []
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fetch_season_csv(season: str, path: str) -> list[dict]:
    url = f"{VAASTAV_BASE}/{season}/{path}"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[train] {season}/{path} unavailable: {e}", file=sys.stderr)
        return []
    return list(csv.DictReader(io.StringIO(resp.text)))


def _build_strength_lookup(teams_rows: list[dict]) -> dict[int, dict]:
    """teams.csv -> the same 1-5 normalized shape fpl_client.build_team_strength_lookup returns."""
    return {
        int(t["id"]): {
            "attack_home":  _norm_strength(t.get("strength_attack_home"),  t.get("strength_overall_home")),
            "attack_away":  _norm_strength(t.get("strength_attack_away"),  t.get("strength_overall_away")),
            "defence_home": _norm_strength(t.get("strength_defence_home"), t.get("strength_overall_home")),
            "defence_away": _norm_strength(t.get("strength_defence_away"), t.get("strength_overall_away")),
            "overall_home": _norm_strength(t.get("strength_overall_home"), 3),
            "overall_away": _norm_strength(t.get("strength_overall_away"), 3),
        }
        for t in teams_rows
        if t.get("id")
    }


def _build_team_schedule(fixtures_rows: list[dict]) -> dict[int, list[dict]]:
    """team_id -> [{event, opp_id, venue, fdr}], sorted by gameweek."""
    schedule: dict[int, list[dict]] = {}
    for f in fixtures_rows:
        event = f.get("event")
        if not event:
            continue
        ev, h, a = int(float(event)), int(f["team_h"]), int(f["team_a"])
        schedule.setdefault(h, []).append(
            {"event": ev, "opp_id": a, "venue": "H", "fdr": int(_to_float(f.get("team_h_difficulty"), 3))})
        schedule.setdefault(a, []).append(
            {"event": ev, "opp_id": h, "venue": "A", "fdr": int(_to_float(f.get("team_a_difficulty"), 3))})
    for legs in schedule.values():
        legs.sort(key=lambda x: x["event"])
    return schedule


def _build_team_form(fixtures_rows: list[dict]) -> dict[tuple[int, int], dict]:
    """
    (team_id, gameweek) -> {points_per_game, goal_diff_per_game} over that team's
    last 5 fixtures BEFORE the gameweek — mirrors fpl_client.build_team_form,
    which is what feeds these two features at inference time. They were
    hardcoded to 1.0 and 0.0 in training.
    """
    played: dict[int, list[tuple[int, int, int]]] = {}
    for f in fixtures_rows:
        if not f.get("event") or f.get("team_h_score") in (None, "") or f.get("team_a_score") in (None, ""):
            continue
        ev, h, a = int(float(f["event"])), int(f["team_h"]), int(f["team_a"])
        hs, as_ = int(_to_float(f["team_h_score"])), int(_to_float(f["team_a_score"]))
        played.setdefault(h, []).append((ev, hs, as_))
        played.setdefault(a, []).append((ev, as_, hs))

    out: dict[tuple[int, int], dict] = {}
    for team_id, matches in played.items():
        matches.sort()
        for gw in range(1, 39):
            recent = [m for m in matches if m[0] < gw][-5:]
            if not recent:
                out[(team_id, gw)] = {"points_per_game": 1.0, "goal_diff_per_game": 0.0}
                continue
            pts = sum(3 if gf > ga else 1 if gf == ga else 0 for _, gf, ga in recent)
            gd = sum(gf - ga for _, gf, ga in recent)
            out[(team_id, gw)] = {
                "points_per_game": round(pts / len(recent), 2),
                "goal_diff_per_game": round(gd / len(recent), 2),
            }
    return out


def _last_season_pts_per90(season: str) -> dict[str, float]:
    """player name -> prior-season points per 90, the `last_season_pts_per90` feature."""
    prior = _SEASON_BEFORE.get(season)
    if not prior:
        return {}
    totals: dict[str, list[float]] = {}
    for row in _fetch_season_csv(prior, "gws/merged_gw.csv"):
        name = row.get("name")
        if not name:
            continue
        agg = totals.setdefault(name, [0.0, 0.0])
        agg[0] += _to_float(row.get("total_points"))
        agg[1] += _to_float(row.get("minutes"))
    return {n: round(pts / mins * 90, 3) for n, (pts, mins) in totals.items() if mins > 0}


def _build_training_rows(
    raw_rows: list[dict],
    strength: dict[int, dict],
    schedule: dict[int, list[dict]],
    team_form: dict[tuple[int, int], dict],
    last_season: dict[str, float],
    team_id_by_name: dict[str, int],
) -> tuple[list[list[float]], list[float]]:
    """
    Group by player, sort by GW, use PRIOR rows' signals (lag features) to predict
    the CURRENT row's points — avoids same-GW leakage (predicting a GW's points
    from that same GW's outcome data would be circular).

    Labels come from scoring_rules.compute_points(), not the row's own
    `total_points` — validated to match exactly (0 mismatches across every
    2025-26 row with playing time) while being explicit and updatable if FPL
    changes the rules again.

    Every feature is now reconstructed to mean the SAME THING it means at
    inference. It previously did not, and not subtly: `ep_next` was the
    player's PRICE (value/10), `xgi_per_90` was ict_index/10, and
    `opponent_strength` was the opponent's team id over 20 — while
    team_ppg, team_gd_pg, xgc_per_90, last_season_pts_per90 and avg_fdr were
    all frozen constants the model could never learn from. Of eleven features
    only `form` and `starts_pct` were approximately honest, which is why a
    starting keeper was being predicted 0.21 points.
    """
    by_player: dict[str, list[dict]] = {}
    for row in raw_rows:
        name = row.get("name") or row.get("element") or ""
        if not name:
            continue
        by_player.setdefault(name, []).append(row)

    X: list[list[float]] = []
    y: list[float] = []
    for name, rows in by_player.items():
        rows.sort(key=lambda r: _to_float(r.get("GW") or r.get("round"), 0))
        position = normalize_position(rows[0].get("position", ""))
        team_id = team_id_by_name.get((rows[0].get("team") or "").strip())

        history_points: list[float] = []
        history_starts: list[float] = []
        cum_xgi = cum_xgc = cum_minutes = 0.0

        for row in rows:
            gw = int(_to_float(row.get("GW") or row.get("round"), 0))
            actual_points = float(compute_points(position, row))

            prior_form = statistics.mean(history_points[-5:]) if history_points else _to_float(row.get("form"))
            starts_pct = (
                sum(history_starts) / len(history_starts) * 100.0 if history_starts else 50.0
            )
            # Per-90 rates from PRIOR gameweeks only — using this row's own
            # xGI to predict this row's points would be leakage.
            xgi_90 = (cum_xgi / cum_minutes * 90.0) if cum_minutes >= 90 else 0.0
            xgc_90 = (cum_xgc / cum_minutes * 90.0) if cum_minutes >= 90 else 0.0

            legs = schedule.get(team_id, []) if team_id else []
            this_leg = next((l for l in legs if l["event"] == gw), None)
            upcoming = [l for l in legs if l["event"] >= gw][:3]
            if upcoming:
                avg_fdr = statistics.mean(
                    _directional_fdr(position, team_id, l["opp_id"], l["venue"], strength, l["fdr"])
                    for l in upcoming
                )
            else:
                avg_fdr = 3.0

            inputs = {
                "form": prior_form,
                "team_ppg": team_form.get((team_id, gw), {}).get("points_per_game", 1.0),
                "team_gd_pg": team_form.get((team_id, gw), {}).get("goal_diff_per_game", 0.0),
                "opponent_strength": (
                    opponent_strength(strength.get(this_leg["opp_id"]), this_leg["venue"])
                    if this_leg else 3.0
                ),
                "last_season_pts_per90": last_season.get(name, 0.0),
                # Not reconstructable historically — the CSVs carry no injury
                # flag. Constant here, so the model never splits on it and an
                # unseen live value simply has no effect, rather than the model
                # having learned a relationship that doesn't hold.
                "chance_of_playing": 1.0,
                "xgi_per_90": xgi_90,
                "xgc_per_90": xgc_90,
                "avg_fdr": avg_fdr,
                "starts_pct": starts_pct,
                # FPL's own expected points for the gameweek — the historical
                # analogue of the live ep_next. Was value/10, i.e. the price.
                "ep_next": _to_float(row.get("xP")),
            }
            # Train only on gameweeks the player actually featured in. 61% of
            # rows are zero-minute players, and including them made the model
            # mostly a "did he play at all" classifier: it learned that low
            # form means a benched player means zero points, and inverted the
            # relationship for everyone else — sweeping `form` from 0 to 12
            # across a real Haaland vector moved the prediction DOWN, 7.78 to
            # 1.95. Availability is not the model's job; it is applied outside
            # (ranking.score_captain / order_bench weight by
            # chance_of_playing, and the XI is picked from players expected to
            # start). So the target here is "points GIVEN he features".
            if _to_float(row.get("minutes")) > 0:
                feat_row = build_feature_row(inputs)
                X.append(feature_vector(feat_row))
                y.append(actual_points)

            history_points.append(actual_points)
            history_starts.append(_to_float(row.get("starts")))
            cum_xgi += _to_float(row.get("expected_goal_involvements"))
            cum_xgc += _to_float(row.get("expected_goals_conceded"))
            cum_minutes += _to_float(row.get("minutes"))
    return X, y


def train() -> dict:
    X: list[list[float]] = []
    y: list[float] = []
    total_rows = 0

    for season in SEASONS:
        rows = _fetch_season_gws(season)
        total_rows += len(rows)
        if not rows:
            continue
        teams_rows = _fetch_season_csv(season, "teams.csv")
        fixtures_rows = _fetch_season_csv(season, "fixtures.csv")
        if not teams_rows or not fixtures_rows:
            print(f"[train] {season}: missing teams/fixtures, cannot build honest features", file=sys.stderr)
            continue
        sx, sy = _build_training_rows(
            rows,
            _build_strength_lookup(teams_rows),
            _build_team_schedule(fixtures_rows),
            _build_team_form(fixtures_rows),
            _last_season_pts_per90(season),
            {t["name"].strip(): int(t["id"]) for t in teams_rows if t.get("id")},
        )
        X.extend(sx)
        y.extend(sy)

    if total_rows < 200:
        print("[train] insufficient historical data fetched, aborting", file=sys.stderr)
        return {"trained": False, "reason": "insufficient_data"}
    if len(X) < 100:
        print("[train] insufficient training rows after feature build, aborting", file=sys.stderr)
        return {"trained": False, "reason": "insufficient_rows"}

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.15, random_state=42)

    # Domain knowledge as hard constraints, because with only ~120 training
    # rows above xP 7 the model was free to fit noise at the top of the range
    # and did: its response to ep_next was non-monotonic (6 -> 1.34, 8 -> 4.40)
    # and its response to form was monotonically BACKWARDS. Sign per feature,
    # in FEATURE_NAMES order; 0 where the sign is genuinely ambiguous
    # (xgc_per_90 hurts a defender but is neutral for a forward, and position
    # is not a feature) or the input is constant (chance_of_playing).
    monotonic_cst = {
        "form": 1, "team_ppg": 1, "team_gd_pg": 1, "opponent_strength": -1,
        "last_season_pts_per90": 1, "chance_of_playing": 0, "xgi_per_90": 1,
        "xgc_per_90": 0, "avg_fdr": -1, "starts_pct": 1, "ep_next": 1,
    }
    model = HistGradientBoostingRegressor(
        max_depth=6,
        random_state=42,
        monotonic_cst=[monotonic_cst[name] for name in FEATURE_NAMES],
    )
    model.fit(X_train, y_train)
    preds = model.predict(X_test)
    mae = mean_absolute_error(y_test, preds)

    prev_mae, prev_schema = None, None
    if META_PATH.exists():
        try:
            prev_meta = json.loads(META_PATH.read_text())
            prev_mae = prev_meta.get("holdout_mae")
            prev_schema = prev_meta.get("feature_schema_version", 1)
        except (json.JSONDecodeError, OSError):
            prev_mae = None

    # FPL's own expected points, scored on the same holdout — the benchmark the
    # model has to beat to be worth running at all.
    ep_idx = FEATURE_NAMES.index("ep_next")
    xp_mae = mean_absolute_error(y_test, [row[ep_idx] for row in X_test])

    schema_changed = prev_schema is not None and prev_schema != FEATURE_SCHEMA_VERSION
    if schema_changed:
        print(
            f"[train] feature schema v{prev_schema} -> v{FEATURE_SCHEMA_VERSION}; "
            f"deployed MAE {prev_mae} is not comparable, skipping the regression gate",
            file=sys.stderr,
        )
    elif prev_mae is not None and mae > prev_mae * 1.05:
        print(
            f"[train] new MAE {mae:.3f} worse than deployed {prev_mae:.3f} "
            "(>5% tolerance) — keeping existing model",
            file=sys.stderr,
        )
        return {"trained": True, "promoted": False, "holdout_mae": mae, "previous_mae": prev_mae}

    with open(MODEL_PATH, "wb") as f:
        pickle.dump(model, f)
    META_PATH.write_text(json.dumps({
        "feature_names": FEATURE_NAMES,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "holdout_mae": mae,
        "fpl_xp_baseline_mae": xp_mae,
        "training_rows": len(X_train),
        "holdout_rows": len(X_test),
        "seasons": SEASONS,
    }, indent=2))
    print(
        f"[train] promoted new model — holdout MAE {mae:.3f} vs FPL xP baseline "
        f"{xp_mae:.3f} (n={len(X_test)})"
    )
    return {"trained": True, "promoted": True, "holdout_mae": mae, "fpl_xp_baseline_mae": xp_mae}


if __name__ == "__main__":
    train()
