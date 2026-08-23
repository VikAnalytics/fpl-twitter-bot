---
tags: [component]
---

# Observability

User's ask, verbatim: "a lot of observability on how the agents act, their conversations, thinking process, what ML models gives out as transfer recommendation, each step of the process should be logged, easier to identify failures that way."

## What existed before this
[[Transfer Debate Engine]] already logged a human-readable transcript to `agent_conversations`, and [[Feedback Loop]] tracked candidate outcomes. But there was no visibility into: the ML model's actual per-player predictions for a given run, an agent's *full* structured output (vs. just its summarized message — e.g. `risk_scrutiny`'s `requires_extra_scrutiny` boolean was used internally but never surfaced), which stages of the pipeline ran and how long each took, or what exactly failed when something did (errors just went to a `print()` in Cloud Run's log stream, disconnected from which gameweek/decision they belonged to).

## What this adds
`app/observability.py`'s `step()` context manager wraps every stage of the pipeline — data fetch, ML prediction, each debate agent's full structured LLM output, backstop validation, captain/lineup scoring, execution, escalation checks, evaluation — logging a timestamped started/succeeded/failed record with duration and structured JSON detail to a new `pipeline_log` table. Every stage within one tick (or one approval-triggered execution) shares a single `run_id`.

`GET /runs` and `GET /runs/{run_id}` (`app/templates/runs.html`) surface this: recent runs at a glance, or the full timeline for one run with exact durations and payloads per stage.

## Design choice: structured detail, not just text
The debate transcript in `agent_conversations` stays as the human-readable prose version (good for reading what happened). `pipeline_log` captures the underlying structured objects instead — e.g. the Analyst's full `AnalystOutput` including `alternates_considered`, or `RiskScrutinyOutput`'s `requires_extra_scrutiny` flag — which is the "thinking process" data a developer actually needs to debug why a decision went a particular way, not just the summarized sentence a human reads.

## Design choice: never swallow errors
`step()` always re-raises after logging a 'failed' record — it's a logging wrapper, not an error handler. A failed stage still fails the pipeline (and the existing try/except blocks in `app/main.py`'s `_run_tick` still stop that stage from blocking the others), but now there's a queryable record of exactly what broke and where, with a truncated traceback attached.

## Token usage and cost
Follow-up ask: "can we also log tokens and price used." `app/pricing.py` encodes gpt-4o-mini's per-token rate (verified via live web search — $0.15/1M input, $0.60/1M output — not trusted from memory, which could be stale). Each debate node's LLM call now uses `include_raw=True` (verified the exact `usage_metadata` field shape with one real, minimal API call before wiring it in) so token counts and estimated cost land in `pipeline_log` alongside everything else, visible per-stage in `/runs/{run_id}` and totaled per-run in `/runs`.

Validated against a real full 5-agent debate, not just a mock: 2,777 input + 548 output tokens, **$0.000746 total** for one complete transfer debate — and the moderator's actual decision (declined to proceed) confirmed the debate logic produces genuine reasoning from real model output, not a scripted result.

## Related
[[Transfer Debate Engine]] · [[ML Model]] · [[Reliability and Escalation]] · [[Feedback Loop]]
