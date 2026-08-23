---
tags: [component]
---

# Captain and Lineup

Deterministic scoring in `app/ranking.py` (`select_best_xi`, `score_captain`, `order_bench`) — no LLM debate, unlike [[Transfer Debate Engine]].

## The gap that was originally missed: best XI selection
The first build only computed captain and bench *order* — it never actually chose which 11 of the 15 squad players should start. User caught this directly: "we need to take into account substitutions also, selecting the best XI is very important that we completely skipped over." Correct catch — captain/bench order were being computed against whatever XI FPL had on record, not a re-optimized one.

Fixed with `select_best_xi()`: for a fixed formation, the optimal XI is just the top-N predicted-points players per position (points are additive, no synergy term, so an exchange argument makes top-N provably optimal within that formation) — so the function brute-forces all 8 legal FPL formations (3-4-3 through 5-4-1) and keeps whichever totals highest. Cheap (8 sums over pre-sorted lists) and exact, not a heuristic. Captain is now scored from *this* selected XI, not the FPL-recorded one.

## Execution wired up too
Originally only transfers auto-executed on approval; captain/lineup were computed and loggable but nothing submitted them. User asked directly ("can we also line it up"). Now `_execute_decision()` in `app/main.py` handles captain/lineup too — since both share one `/my-team/{id}/` FPL API call, approving one just waits until its sibling is also approved, then submits the combined 15-pick payload (captain/vice flags, XI order, bench priority with the backup GK correctly last).

## Why deterministic
Raised during a design-review pass on the original plan: captain selection is effectively `argmax(predicted_points × chance_of_playing)` over nailed starters, and bench order is a sort by predicted points + playing-chance. Running a 5-agent adversarial debate for a near-argmax problem adds LLM cost/latency/failure-mode risk for a decision a formula gets right the vast majority of the time.

The user pushed back — "why is it overengineered" — and after the tradeoff was laid out (including that debate transcripts have standalone Twitter-content value, since this is also a Twitter bot), the user still chose deterministic-only for cost/latency. Decisions are still logged to `agent_decisions`/`agent_conversations` for the audit trail even without an argued debate.

## Bug found during implementation
The rotation-risk `starts_pct` floor was originally written as `p.starts_pct >= floor or not p.recent_form_5gw` — intended as "don't filter on missing data" but it actually bypassed the floor entirely whenever recent form was empty, defeating the filter's purpose. Fixed: eligibility now falls back to the full XI only if *nobody* clears the floor, not whenever form data happens to be empty. Verified with a smoke test before/after.

## Related
[[ML Model]] (predictions feed both functions) · [[Decisions Log]]
