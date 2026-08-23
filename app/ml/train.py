"""
Offline training script for the expected-points model.

Data source: vaastav/Fantasy-Premier-League (github.com/vaastav/Fantasy-Premier-League),
a free, MIT-licensed, community-standard mirror of multi-season FPL gameweek data —
the one free resource this app needed but didn't have (everything else comes from
the live FPL API already called elsewhere in the app).

Run: python -m app.ml.train

Promotion gate: a newly trained model only overwrites model.pkl if its holdout MAE
isn't meaningfully worse (>5%) than the currently deployed model's — prevents a bad
weekly training run from silently degrading prediction quality.

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

Known limitation: some features (team_ppg/team_gd_pg, last_season_pts_per90,
xgc_per_90, avg_fdr) can't be reconstructed exactly from the historical CSV alone
and are approximated/neutral during training; at live inference time
(app/ml/features.build_live_inputs) these are populated precisely from the current
FPL API data. This is a training/inference feature-parity gap worth tightening later
by joining in fixtures.csv/teams.csv per season rather than using neutral priors.
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

from .features import FEATURE_NAMES, build_feature_row, feature_vector
from .scoring_rules import compute_points, normalize_position

VAASTAV_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
# Most recently completed season only — see "Scoring-rule robustness" above.
# Update this each close season (e.g. to ["2026-27"] once that one finishes).
SEASONS = ["2025-26"]

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


def _build_training_rows(raw_rows: list[dict]) -> tuple[list[list[float]], list[float]]:
    """
    Group by player, sort by GW, use PRIOR rows' signals (lag features) to predict
    the CURRENT row's points — avoids same-GW leakage (predicting a GW's points
    from that same GW's outcome data would be circular).

    Labels come from scoring_rules.compute_points(), not the row's own
    `total_points` — validated to match exactly (0 mismatches across every
    2025-26 row with playing time) while being explicit and updatable if FPL
    changes the rules again.
    """
    by_player: dict[str, list[dict]] = {}
    for row in raw_rows:
        name = row.get("name") or row.get("element") or ""
        if not name:
            continue
        by_player.setdefault(name, []).append(row)

    X: list[list[float]] = []
    y: list[float] = []
    for _name, rows in by_player.items():
        rows.sort(key=lambda r: _to_float(r.get("GW") or r.get("round"), 0))
        position = normalize_position(rows[0].get("position", ""))
        history_points: list[float] = []
        for row in rows:
            actual_points = float(compute_points(position, row))
            prior_form = statistics.mean(history_points[-5:]) if history_points else _to_float(row.get("form"))
            starts_pct = (
                min(100.0, len([h for h in history_points if h > 0]) / max(len(history_points), 1) * 100)
                if history_points else 50.0
            )
            inputs = {
                "form": prior_form,
                "team_ppg": 1.0,
                "team_gd_pg": 0.0,
                "opponent_strength": _to_float(row.get("opponent_team"), 10) / 20.0,
                "last_season_pts_per90": 0.0,
                "chance_of_playing": 1.0,
                "xgi_per_90": _to_float(row.get("ict_index")) / 10.0,
                "xgc_per_90": 0.0,
                "avg_fdr": 3.0,
                "starts_pct": starts_pct,
                "ep_next": _to_float(row.get("value")) / 10.0,
            }
            feat_row = build_feature_row(inputs)
            X.append(feature_vector(feat_row))
            y.append(actual_points)
            history_points.append(actual_points)
    return X, y


def train() -> dict:
    all_rows: list[dict] = []
    for season in SEASONS:
        all_rows.extend(_fetch_season_gws(season))

    if len(all_rows) < 200:
        print("[train] insufficient historical data fetched, aborting", file=sys.stderr)
        return {"trained": False, "reason": "insufficient_data"}

    X, y = _build_training_rows(all_rows)
    if len(X) < 100:
        print("[train] insufficient training rows after feature build, aborting", file=sys.stderr)
        return {"trained": False, "reason": "insufficient_rows"}

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.15, random_state=42)

    model = HistGradientBoostingRegressor(max_depth=6, random_state=42)
    model.fit(X_train, y_train)
    preds = model.predict(X_test)
    mae = mean_absolute_error(y_test, preds)

    prev_mae = None
    if META_PATH.exists():
        try:
            prev_mae = json.loads(META_PATH.read_text()).get("holdout_mae")
        except (json.JSONDecodeError, OSError):
            prev_mae = None

    if prev_mae is not None and mae > prev_mae * 1.05:
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
        "holdout_mae": mae,
        "training_rows": len(X_train),
        "holdout_rows": len(X_test),
        "seasons": SEASONS,
    }, indent=2))
    print(f"[train] promoted new model — holdout MAE {mae:.3f} (n={len(X_test)})")
    return {"trained": True, "promoted": True, "holdout_mae": mae}


if __name__ == "__main__":
    train()
