# Autonomous Multi-Agent FPL Decision Engine

_Committed copy of the approved implementation plan. See [docs/architecture.md](architecture.md) for the living system-design doc and [docs/progress.md](progress.md) for build status._

## Context

The repo currently ships a **read-only advisory** system: `app/ranking.py` scores transfer candidates with hand-tuned heuristics, a single `gpt-4o-mini` call (`app/llm.py`) turns that into a narrative + suggested transfers, and a human reads it in the Chrome extension / web brief and acts manually. There is no captain/XI/bench logic at all — `bot.py` just tweets "Captain sorted? Bench order?" as a reminder. There is no ML model (no sklearn/xgboost in the repo), no FPL login (all endpoints are unauthenticated `GET`s), no scheduled trigger (cron was removed, only `workflow_dispatch` remains), and no structured logging (just `print()`).

The user wants this system to become a **weekly autonomous decision-maker**: an ML model scores players, LLM agents debate the best moves (with extra scrutiny on point-hit transfers), the debate is fully logged, and the final decision is executed against the user's real FPL team — gated by one human approval tap (semi-autonomous). Missing an execution window is **never acceptable**, so the design includes a hard reliability/escalation layer with real lead time. The decision quality itself must be provably tracked over time — not just "did the suggested transfer score more points" but "how did the alternates we considered but didn't pick perform, and which debate personas tend to be right."

Decisions locked in with the user (including a design-review pass that stress-tested the first draft):
- **Execution**: semi-autonomous — agent prepares the exact payload, human approves with one tap, then it executes automatically.
- **ML**: a real trained model (gradient-boosted trees) trained on historical gameweek data, retrained periodically, model artifact committed to the repo.
- **LLM**: OpenAI `gpt-4o-mini`, orchestrated via **LangGraph** for the transfer debate specifically (not uniformly across all decision types — see below).
- **Debate scope**: full adversarial multi-agent debate is reserved for **transfers only** (and escalates further for point-hit transfers). Captain and starting XI/bench are near-deterministic argmax/sort problems — computed deterministically via `ranking.py` functions and still logged for the audit trail, just without an argued debate.
- **Reliability**: a missed transfer window is unacceptable, so execution runs with real lead time before deadline, retries, and a hard Telegram escalation if anything is off-track.
- **Feedback loop**: candidate-level outcome tracking plus per-persona calibration.
- **Free resources**: FPL's own API already provides most signals. The one missing free resource — a multi-season historical training set — is covered by the `vaastav/Fantasy-Premier-League` GitHub repo. Escalation uses Telegram's official free Bot API.

## Architecture Overview

```
GitHub Actions cron (every 30min, concurrency-guarded)
  -> Data ingestion (FPL API + vaastav historical CSVs for training)
  -> Features + ML model (GradientBoostingRegressor -> expected_points)
  -> Transfer debate (LangGraph, 5 personas, hit-scrutiny loop)
     + Captain/Lineup (deterministic, no LLM)
  -> Approval gate (deadline-3h cutoff; Telegram escalation if unapproved)
  -> Execution (FPL login + submit, retries, Telegram escalation on failure)
  -> Feedback & verification loop (after GW plays out)
```

See [docs/architecture.md](architecture.md) for the full component breakdown and file map — it reflects the **current** hosting/scheduling design, which evolved after this plan was approved (Railway's free tier disappeared; GitHub Actions' `schedule:` trigger proved unreliable enough to route around). Currently: Google Cloud Run + Turso + cron-job.org, not Railway + GitHub Actions cron. The split-brain SQLite gap this plan's first draft flagged was resolved as a side effect of that migration — see "Resolved: split-brain DB" in architecture.md.

## Full plan detail

The complete, section-by-section plan (data feeds, ML model, transfer debate engine, captain/lineup, logging schema, feedback loop, reliability/escalation, execution, scheduling, docs/Obsidian, verification) is preserved in this session's plan file and mirrored into [docs/architecture.md](architecture.md) and [docs/progress.md](progress.md) rather than duplicated here in full — those two documents are kept current as implementation proceeds; this file is the frozen record of what was approved.
