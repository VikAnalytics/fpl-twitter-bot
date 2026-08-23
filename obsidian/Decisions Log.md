---
tags: [log]
---

# Decisions Log

Chronological record of major design decisions made during planning and implementation, and why.

## Planning phase
- **Execution mode**: semi-autonomous chosen over full autonomy — FPL transfer submission requires real login credentials against an unofficial API; user preferred a human-approval gate over blind full autonomy.
- **ML approach**: trained model (gradient-boosted trees) chosen over a bigger weighted-scoring formula — user explicitly wanted "ML," and the free `vaastav/Fantasy-Premier-League` historical dataset made real training feasible.
- **LLM provider**: kept OpenAI `gpt-4o-mini` over Groq/Gemini free tiers — already integrated, cheap, known-good, despite the "utilize all free resources" instruction (interpreted as applying most strongly to *data* feeds, where free options were genuinely missing, rather than displacing an already-working cheap LLM).
- **Orchestration**: LangGraph adopted for the transfer debate after a direct user question ("can we use langchain or langgraph") — StateGraph maps cleanly onto propose→critique→conditional-loop→decide.
- **Debate scope**: narrowed from "full debate for all 3 decision types" to "transfers only" after a design-review pass argued captain/lineup are near-deterministic. User initially pushed back ("why is it overengineered") before agreeing, once the content-value angle (debate transcripts as Twitter content) was surfaced and weighed against cost/latency. See [[Captain and Lineup]].
- **Escalation channel**: WhatsApp (via CallMeBot, free) chosen over tweet-only or email, after the user set a hard "never acceptable to miss, ≥1h notice" requirement.
  - **Reversed post-implementation**: user decided against the Chrome extension as an approval surface ("i dont want to use it") — the standalone `/decisions/{manager_id}` web page became the primary approval UI instead (extension code initially left in place as an optional secondary surface).
  - **Later removed entirely**: once Telegram approval was built and working, user asked to delete the extension outright rather than keep unused code around. `extension/` and `build-extension.sh` deleted; `app/main.py`'s CORS config and the extension-only polling route cleaned up to match.
  - **Reversed again**: user rejected CallMeBot for being an unofficial API, asked for Twitter DM instead. Twitter DMs require a paid X API tier (Basic, $200/mo+) — user confirmed no paid tier, ruling that out too.
  - **Landed on Telegram's Bot API** — official, free, no reverse-engineering, satisfies every constraint raised across the three attempts. `app/notify.py` rewritten accordingly; `CALLMEBOT_PHONE`/`CALLMEBOT_API_KEY` secrets replaced with `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` throughout the workflow and docs.
- **Feedback loop depth**: candidate-level + per-persona calibration chosen over candidate-level alone.
- **Docs**: `docs/plan.md` + `docs/architecture.md` + `docs/progress.md` in-repo, plus this Obsidian vault, so the reasoning trail survives independently of any single chat session.

## Post-implementation: XI selection, lineup execution, Telegram approval, cost tracking
Four asks in one message, addressed together:
1. **Approval via Telegram, not just the web page.** Inline Approve/Reject buttons on every decision; `/telegram/webhook` handles taps via a shared-secret header. Web page kept as a secondary surface.
2. **Clarified no tweets are involved.** Earlier phrasing ("not just a tweet," explaining why Telegram beats a tweet as a channel) was misread as tweets happening in this flow — they don't. The separate, pre-existing `bot.py` tweet feature (deadline/DGW/injury alerts) was confirmed to stay as-is, untouched.
3. **Wired lineup execution + caught a real gap: best-XI selection was completely missing.** User: "selecting the best XI is very important that we completely skipped over" — correct, the original build only computed captain and bench order against whatever XI FPL already had on record, never actually chose the 11. Fixed with `ranking.select_best_xi()` (formation-aware, provably optimal per formation, brute-forces all 8 legal formations). Also wired `_execute_decision()` to actually submit captain/lineup to FPL, not just transfers — they share one API call, so it waits for both decisions to be approved before submitting the combined payload.
4. **Token usage + cost logging**, verified against a real API call rather than guessed — pricing fetched via live search, response shape confirmed with one minimal real call, then the whole thing validated against a real 5-agent debate ($0.000746 total).

**Real near-miss during this batch, handled transparently**: a Telegram webhook test approved a transfer decision without mocking `fpl_login()`, triggering what looked like a real login attempt against the live FPL account before being killed. Reported directly to the user rather than glossed over — see [[Known Gaps]]. All subsequent tests mock every FPL network call explicitly.

## Post-implementation: observability
- User asked directly for "a lot of observability on how the agents act, their conversations, thinking process, what ML models gives out as transfer recommendation, each step of the process should be logged, easier to identify failures that way."
- Built `app/observability.py`'s `step()` context manager and wired it into every stage of the pipeline (not just the debate — data fetch, ML predictions, backstop validation, execution, escalation, evaluation too), grouped by a per-run `run_id`. See [[Observability]].
- Smoke-tested both the success path (structured detail persisted) and the failure path (error + traceback captured, exception still re-raised — a logging wrapper, not an error handler) before considering it done.

## Post-implementation: scoring-rule robustness
- User asked, correctly: "some scoring system may have changed from last year, that would affect the ML model wouldn't it" — the training pipeline was trusting historical `total_points` values, which reflect whatever scoring rules were live in that season, not necessarily current ones (FPL added defensive contribution points and raised keeper goal value to 10 in 2025-26).
- User's direction was specific: "have the perfect knowledge of the scoring system and track data of players according to how scoring works. Also focus on last 1 year data and not 3."
- Before writing any rule table from memory, fetched the actual current rules via WebSearch/WebFetch rather than guessing — memory alone would likely have gotten the goalkeeper goal value wrong (6, not the current 10).
- Built `app/ml/scoring_rules.py` and validated it by downloading the real 2025-26 CSV and checking all 11,498 played rows — caught two real bugs this way before trusting it (goalkeeper position label mismatch, goals-conceded penalty incorrectly gated behind 60+ minutes) and re-validated to 0 mismatches after fixing both.
- Actually ran `python -m app.ml.train` end-to-end rather than just writing the code and assuming it works — produced a real `model.pkl`, holdout MAE 0.641. See [[ML Model]].

## Post-implementation: hosting pivot
- User asked to replace GitHub Actions' `schedule:` trigger with cron-job.org — flagged as a known reliability gap in GitHub's own docs, in direct conflict with the "never miss a deadline" requirement.
- User then flagged they might not have Railway's free tier (it's gone — $5/mo minimum now) and asked about "our own VPC" as an alternative. Offered Google Cloud Run, Oracle Cloud Always Free, or existing infra; user chose **Cloud Run**.
- Surfaced a consequence before building anything: Cloud Run's filesystem is ephemeral, so the existing SQLite-file approach would silently lose state between cron ticks. Offered a migration to **Turso** (real network DB, fixes it properly, also resolves the long-standing split-brain gap) vs. a **GCS FUSE volume mount** (smaller change, weaker guarantees). User chose Turso.
- Rewrote `app/database.py` for `libsql_client`, verified the API by installing the package and inspecting it directly rather than guessing (found `.asdict()` is required instead of `dict(row)`, found `.batch()` is required for multi-statement schema creation, found `ClientSync` needs explicit `.close()` or the process hangs forever). See [[Hosting and Scheduling]] for the full picture.
- Result: `weekly_decision.yml` deleted, replaced by a `/internal/tick` webhook on the FastAPI app; `train_model.yml` kept on GitHub Actions since it's not deadline-critical and doesn't touch the DB.

## Implementation phase
- Found and fixed a real bug in `score_captain()`'s eligibility filter (see [[Captain and Lineup]]) via smoke testing before shipping.
- Discovered the split-brain SQLite issue (see [[Known Gaps]]) while wiring the approval route — flagged rather than silently shipped or silently "fixed" with an out-of-scope architecture change.
- Chose to execute transfers synchronously within the FastAPI approval route rather than via a second isolated GitHub Actions workflow, since the credential-isolation goal from the plan (keep FPL_EMAIL/PASSWORD away from the broad-dependency debate job) is naturally satisfied by Railway and GitHub Actions already being separate deployments/processes.
