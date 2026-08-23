---
tags: [component]
---

# Transfer Debate Engine

LangGraph state machine (`app/agents/`) — 5 personas debate the best transfer(s) each week.

## Flow
`analyst -> fixture_form -> news_injury -> risk_scrutiny -> [loop once more if hit/high-risk] -> moderator`

Every node's message is logged to `agent_conversations` immediately (crash-safe). The moderator's decision is **not trusted directly** — a deterministic backstop (budget/position/club/hit-breakeven, reused from `app/llm.py`'s existing `_validate_transfer`/`hit_breakeven_ok`) runs after the graph completes.

## Why LangGraph
Considered early via a "can we use langchain/langgraph" question mid-planning. A `StateGraph` maps directly onto propose → critique → conditional extra-scrutiny loop → decide, and gives a natural place to hang persistence — though see [[Known Gaps]] for why the checkpointer wasn't ultimately relied on for that (manual `agent_conversations` writes are the source of truth instead, to avoid coupling the audit UI to LangGraph's internal checkpoint format).

## Why transfers only (not captain/lineup)
See [[Captain and Lineup]] for the full argument — short version: transfers have genuine multi-sided trade-offs (budget, fixtures, hit cost, role fit) where an adversarial panel earns its cost; captain/lineup are close to deterministic argmax/sort problems where debate is theater.

## Why the extra scrutiny round on hits
The user's original ask: "extra transfers which would require taking on negative points should be scrutinized more." Implemented as a LangGraph conditional edge — `risk_scrutiny` sets `requires_extra_scrutiny=true` and the graph routes back to it once (capped) before the moderator gets to decide, holding hit transfers to a stricter bar than free ones.

## Related
[[ML Model]] · [[Feedback Loop]] (candidate-level tracking of what this engine considered) · [[Reliability and Escalation]] (what happens after a decision is proposed)
