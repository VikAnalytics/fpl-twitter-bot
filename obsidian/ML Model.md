---
tags: [component]
---

# ML Model

Predicts expected points per player per gameweek. Lives in `app/ml/`.

## What
- `HistGradientBoostingRegressor` (scikit-learn), trained on the [[vaastav historical dataset]] — currently just the 2025-26 season (see "Why one season" below)
- Features: `player_form`, `team_form`, `opponent_strength`, `last_season_form`, `chance_of_playing` (news proxy), plus supporting signals reused from `app/ranking.py` (xGI/90, xGC/90, avg FDR, starts_pct, ep_next)
- Inference falls back to FPL's own `ep_next` if `model.pkl` doesn't exist — no longer the common case, since a real model is now trained (holdout MAE 0.641 points, see below)

## Why a trained model, not just a bigger heuristic
The user explicitly wanted an "ML algorithm," and the existing `app/ranking.py` heuristic scoring, while effective, is hand-tuned additive weights — not learned from data. A gradient-boosted regressor trained on multi-season history is the more literal interpretation, and the free `vaastav/Fantasy-Premier-League` GitHub dataset made this feasible without paid data.

## Why promotion-gated retraining
A weekly retrain job that blindly overwrites `model.pkl` risks silently degrading prediction quality if a given week's training run is bad (data glitch, degenerate fit) with nobody watching. The gate: only promote if holdout MAE isn't >5% worse than the currently-deployed model's.

## Why one season, and why labels are recomputed from scratch (app/ml/scoring_rules.py)
User asked directly: "some scoring system may have changed from last year, that would affect the ML model wouldn't it" — correct catch. FPL changes scoring rules between seasons (2025-26 added defensive contribution points and raised goalkeeper goals to 10 points), so a historical row's own `total_points` reflects whatever rules were live at the time it was recorded, not necessarily now.

Fix, per explicit user direction ("have the perfect knowledge of the scoring system... focus on last 1 year data and not 3"): `app/ml/scoring_rules.py` encodes the exact current point-per-action table as code, and training labels are recomputed from raw match stats through it rather than trusted as-is. Validated by fetching the real 2025-26 CSV and checking every one of 11,498 rows with playing time — **0 mismatches** after fixing two real bugs the validation caught: (1) the CSV labels goalkeepers `"GK"`, this app uses `"GKP"` everywhere else — silent scoring error if not normalized; (2) the goals-conceded penalty for GKP/DEF isn't gated behind 60+ minutes the way the clean-sheet bonus is — a keeper who conceded 2 in 4 minutes before a red card still took the -1 hit in real data, which the first draft of the rule table missed.

`SEASONS` was also narrowed from 3 seasons to just `["2025-26"]` (the most recently completed one), so playing styles and tactical trends stay representative of the current game, not diluted by older eras — separate from, but complementary to, the scoring-rule fix.

## Known limitation
Some features can't be reconstructed precisely from the vaastav CSV at training time (team form, last-season points/90, xGC/90, avg FDR) and are approximated/neutral during training, while being populated precisely at live inference time from the FPL API. This is a train/inference feature-parity gap — see the caveat in `app/ml/train.py`'s docstring. Related: [[Known Gaps]].

## Related
[[Transfer Debate Engine]] consumes this model's predictions as grounding for the Analyst persona. [[Captain and Lineup]] uses it directly as the deterministic scoring input.
