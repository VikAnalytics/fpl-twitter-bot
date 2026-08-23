---
tags: [component]
---

# Feedback Loop

`app/agents/evaluate.py` — extends the pre-existing `past_outcome_adjustment()` pattern (single-agent, sold-vs-bought only) into a full counterfactual + calibration system.

## What it tracks
- **Candidate-level**: every player the [[Transfer Debate Engine]]'s Analyst persona considered (chosen_in, chosen_out, and named alternates) gets scored against actual gameweek points after the fact — not just the winner. Answers "how did the alternates perform."
- **Persona-level**: each debate persona's stance (favored/opposed the final pick) is marked correct/incorrect once the outcome is known, aggregated into a rolling accuracy score injected into next week's debate context as a calibration caveat (e.g. "risk_scrutiny correct on 70% of its last 10 stances").

## Why this depth
User's own words: "feedback loop would be good, but we will have to have a verification system of the agents' transfer suggestion and how it panned out, how alternates performed and how player transferred out suggested—" (cut off, but the direction was clear). Offered two depths; user chose candidate-level + per-persona calibration over candidate-level alone.

## Related
[[Transfer Debate Engine]] (produces the candidates this evaluates) · [[ML Model]] (candidate outcome data is a future training signal, not yet wired into `train.py`)
