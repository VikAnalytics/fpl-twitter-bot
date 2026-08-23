"""
Inference wrapper around the trained expected-points model.

Falls back to the FPL-provided `ep_next` heuristic if model.pkl hasn't been
trained yet (first deploy) or fails to load — the app must never hard-fail
because the ML layer is unavailable, per app/ranking.py's existing
fallback-friendly design philosophy.
"""
from __future__ import annotations

import pickle
from pathlib import Path

from .features import FEATURE_NAMES, build_feature_row, feature_vector

MODEL_PATH = Path(__file__).parent / "model.pkl"
META_PATH = Path(__file__).parent / "model_meta.json"

_model = None
_load_attempted = False


def _load_model():
    global _model, _load_attempted
    if _load_attempted:
        return _model
    _load_attempted = True
    if MODEL_PATH.exists():
        try:
            with open(MODEL_PATH, "rb") as f:
                _model = pickle.load(f)
        except Exception:
            _model = None
    return _model


def model_is_available() -> bool:
    return _load_model() is not None


def predict_points(rows: list[dict]) -> list[float]:
    """
    rows: list of feature-input dicts (see features.build_live_inputs).
    Returns predicted next-GW points per row, same order.
    """
    if not rows:
        return []
    model = _load_model()
    if model is None:
        return [round(float(r.get("ep_next") or 0.0), 2) for r in rows]
    X = [feature_vector(build_feature_row(r)) for r in rows]
    preds = model.predict(X)
    return [max(0.0, round(float(p), 2)) for p in preds]
