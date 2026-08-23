# Progress — Autonomous Multi-Agent FPL Decision Engine

Living checklist. Update at the end of each implementation session. See [plan.md](plan.md) and [architecture.md](architecture.md).

## Data Feeds
- [x] `build_team_form()` — team form derived from finished fixtures (`app/fpl_client.py`)
- [x] `fetch_player_history_past()` — last-season form exposed (`app/fpl_client.py`)
- [x] Opponent strength — reused existing `build_team_strength_lookup()`
- [x] Latest news — reused existing `news`/`chance_of_playing_next_round` fields

## ML Model
- [x] `app/ml/features.py` — feature schema + live/training adapters
- [x] `app/ml/model.py` — inference wrapper with ep_next fallback
- [x] `app/ml/train.py` — vaastav historical data, HistGradientBoostingRegressor, promotion gate
- [x] `app/ml/scoring_rules.py` — explicit encoding of FPL's current scoring table (goals by position, assists, clean sheets, saves, goals conceded, penalties, cards, own goals, and the new 2025-26 defensive contribution rule), **validated with 0 mismatches against all 11,498 real 2025-26 rows with playing time**. Training labels are recomputed from raw match stats via this table instead of trusted from a historical row's own `total_points` — makes the model robust to FPL changing scoring rules between seasons (verified two real bugs this way: goalkeeper label is `"GK"` not `"GKP"` in the vaastav CSV, and the goals-conceded penalty isn't gated behind 60+ minutes the way clean sheets are — both fixed and re-verified to 0 mismatches).
- [x] `SEASONS` narrowed to `["2025-26"]` (most recently completed season only) rather than 3 seasons — avoids diluting training on scoring-rule regimes and playing styles that may no longer be representative.
- [x] **Model actually trained** — `app/ml/model.pkl` exists, holdout MAE **0.641 points** across 4,464 held-out rows (25,293 training rows total). Live predictions no longer need to fall back to `ep_next`.
- [ ] Known limitation: some training-set features (team_ppg, last_season_pts_per90, xgc_per_90, avg_fdr) are still approximated/neutral from the vaastav CSV alone — see caveat in `train.py` docstring. Tightening this (joining fixtures.csv/teams.csv per season) is future work, unaffected by the scoring-rules fix above.
- [ ] Known gap introduced by the Cloud Run migration: `model.pkl` is baked into the Docker image at deploy time, so a new model from the weekly retrain doesn't take effect until Cloud Run is redeployed — not yet automated. (The model that WILL be baked into the first deploy is the one just trained, so this is fine for the initial deploy — it's future retrains that need this.)

## Transfer Debate Engine
- [x] `app/agents/state.py`, `personas.py`, `graph.py` — LangGraph 5-persona debate + hit-scrutiny loop
- [x] `app/agents/pipeline.py` — context assembly + deterministic backstop (reuses `app/llm.py` validators)
- [x] Verified: graph compiles, conditional loop caps at exactly 1 extra round (smoke-tested with a mocked LLM)
- [ ] **Not yet run against a real OpenAI key or real squad data end-to-end**

## Captain & Lineup (deterministic)
- [x] `ranking.score_captain()`, `ranking.order_bench()`
- [x] Bug found + fixed during implementation: the rotation-risk `starts_pct` floor was being bypassed whenever `recent_form_5gw` was empty — fixed and re-verified with a smoke test
- [x] **`ranking.select_best_xi()`** — user caught a real gap: the original build computed captain and bench *order* but never actually selected which 11 of the 15 squad players should start. Now brute-forces all 8 legal FPL formations (3-4-3 through 5-4-1) and picks whichever maximizes total predicted points — provably optimal per formation (top-N by predicted points, since points are additive with no synergy term), not a heuristic. Hand-verified against a synthetic squad by computing all 8 formation totals manually and confirming the code picked the true max.
- [x] Captain is now scored from the *selected* XI, not the FPL-recorded one — matters when our own model disagrees with what's currently set

## Lineup Execution
- [x] `_execute_decision()` in `app/main.py` now handles `captain`/`lineup` decisions, not just `transfer` — builds the full 15-pick `/my-team/{id}/` payload (captain/vice flags, starting XI order, bench priority order with the backup GK correctly placed last) via `fpl_auth.set_lineup()`
- [x] Captain and lineup are separate decisions but map to one FPL API call — approving one just waits; only once BOTH are approved does it submit the combined payload and mark both executed. Verified with mocked FPL calls (payload structure, captain/vice flag placement, bench-GK-last positioning, and the wait-then-execute-together pairing logic all confirmed correct)

## Approval via Telegram
- [x] User wanted approval from Telegram chat directly instead of the web page — `app/notify.py`'s `send_decision_for_approval()` sends each new decision with inline Approve/Reject buttons
- [x] `POST /telegram/webhook` (`app/main.py`) receives the button-tap callback, verified via `X-Telegram-Bot-Api-Secret-Token` header (same shared-secret pattern as `CRON_SECRET`), and calls the same `_approve()`/`_reject()` logic the HTTP route and this webhook now both share
- [x] Verified end-to-end with mocked FPL/Telegram network calls: wrong secret → 401, non-button updates ignored, approve callback → decision executed, reject callback → decision rejected
- [x] **Caught a real near-miss during testing**: an earlier test run of the webhook approve path did NOT mock `fpl_login()`, meaning it attempted a real login against the live FPL account with real credentials from `.env` before being killed. No transfer was submitted (it never got past login), but this was flagged transparently rather than silently ignored. All subsequent tests explicitly mock every FPL/Telegram network call.
- [x] `/decisions/{manager_id}` web page still works as a secondary approval surface, not removed
- [x] Clarified for the user: no tweets are sent anywhere in this decision/approval flow — the only tweeting in the repo is the separate, pre-existing `bot.py` (deadline/DGW/injury alerts), left untouched per explicit choice

## Token Usage & Cost Logging
- [x] `app/pricing.py` — gpt-4o-mini pricing ($0.15/1M input, $0.60/1M output), verified via live web search rather than trusted from memory
- [x] `app/agents/graph.py`'s LLM calls switched to `include_raw=True` so `usage_metadata` (input/output token counts) is available — verified the exact response shape with one real, minimal (~$0.0001) API call before wiring it in, rather than guessing the field names
- [x] `pipeline_log` gained `tokens_in`/`tokens_out`/`cost_usd` columns; `GET /runs` shows total cost per run, `GET /runs/{run_id}` shows tokens+cost per stage
- [x] **Validated against a real full 5-agent debate**: 2,777 input + 548 output tokens, total cost **$0.000746** for one complete transfer debate. The moderator's real decision (declined to proceed, citing unresolved risk concerns) confirms the debate logic produces genuine reasoning, not a scripted outcome.

## Logging
- [x] `agent_decisions`, `agent_conversations`, `decision_candidates`, `persona_calibration` tables + accessors (`app/database.py`)
- [x] `/decisions/{manager_id}` view + `app/templates/decisions.html`

## Feedback & Verification Loop
- [x] `app/agents/evaluate.py` — candidate-level counterfactual scoring + per-persona calibration
- [ ] **Not yet run** — needs at least one completed decision + finished gameweek to produce real output

## Reliability & Escalation
- [x] `app/notify.py` — Telegram Bot API (official, free), dry-run fallback if unset
- [x] Telegram bot created (`@AI_SirAlex_bot`), chat ID confirmed (`8685866725`), test message delivered successfully
- [x] `app/agents/escalation_check.py` — unapproved-by-cutoff + last-chance failsafe alerts
- [x] In-process `threading.Lock` around `_run_tick()` in `app/main.py` (replaces the old GitHub Actions `concurrency:` guard now that scheduling is a single long-lived process)
- [x] **Split-brain DB gap resolved** — see "Resolved: split-brain DB" in architecture.md. Moving the pipeline off GitHub Actions onto the same process that serves approvals, backed by one Turso DB, removes the split entirely.

## Execution
- [x] `app/fpl_auth.py` — login/submit/set_lineup with retry+backoff
- [x] `/api/decisions/{id}/approve` wired to execute transfers synchronously on approval
- [ ] Captain/lineup execution (the actual `/my-team/{id}/` POST) is **not yet wired** — `_execute_decision()` in `app/main.py` currently only executes `transfer` decisions; captain/lineup decisions are logged and approvable but nothing calls `fpl_auth.set_lineup()` yet
- [ ] **Never tested against a real or staging FPL account** — per the plan's verification section, this must be dry-run tested before pointing at a live team

## Extension — REMOVED
- [x] The Chrome extension (`extension/`, `build-extension.sh`) that originally shipped with this repo, and its Approve/Reject buttons that were built during this work, have been deleted entirely per explicit request — not kept around as a secondary surface. `app/main.py` cleaned up accordingly: `chrome-extension://*` dropped from CORS origins, the extension-only `/api/decisions/{manager_id}/pending` polling route removed, `/api/brief/{manager_id}`'s docstring de-referenced.
- [x] Telegram (inline buttons) and `/decisions/{manager_id}` (web page) are the only approval surfaces now.

## Database (Turso migration)
- [x] `app/database.py` rewritten from raw `sqlite3` to `libsql_client` — same SQL, same `?` placeholders, same row-by-name access; every accessor function's external signature unchanged
- [x] Falls back to a local SQLite file when `TURSO_DATABASE_URL` is unset (dev convenience, not usable in production on Cloud Run)
- [x] Found + fixed during implementation: `libsql_client.ClientSync` runs a background thread that keeps the process alive indefinitely unless `.close()` is called — added `db.close()` to every CLI entry point's `finally` block (`pipeline.py`, `evaluate.py`, `escalation_check.py` `main()` functions); the long-lived FastAPI process intentionally never closes it
- [x] Found + fixed: `evaluate.py`'s CLI `main()` originally called `db.close()`, which would have killed the shared DB client if called from within the FastAPI process — split into a pure `run_evaluate()` (safe to call from the server) and a CLI-only `main()` that owns the DB lifecycle
- [x] End-to-end smoke test passed against a local file, then **against the real Turso database** — init, insert, read, update, close all verified
- [x] Found + fixed at connection time: the `libsql://` (WebSocket/Hrana) scheme failed a WebSocket handshake (`400`) against the real Turso instance — switched to the `https://` scheme for the same database, which works and is a better fit for a request/response server anyway. `TURSO_DATABASE_URL` should use `https://`, not `libsql://`.
- [x] Test rows written during verification were cleaned up afterward (not left in the real database)

## Scheduling
- [x] `/internal/tick` webhook endpoint in `app/main.py` — replaces `weekly_decision.yml` entirely (file deleted)
- [x] Protected by `X-Cron-Secret` header, checked against `CRON_SECRET` env var
- [x] Runs pipeline → escalation check → evaluate sequentially as a `BackgroundTask`, returns 202 immediately (cron-job.org's request timeout otherwise wouldn't survive a multi-LLM-call debate)
- [x] `.github/workflows/train_model.yml` — kept on GitHub Actions (weekly, doesn't touch the DB, low-stakes if occasionally delayed)
- [x] **cron-job.org configured and verified** — firing `/internal/tick` every 30 min, confirmed via a real trigger reaching the deployed app and logging a real run
- [x] **Cloud Run deployed** — `https://fpl-gaffer-283700541620.us-central1.run.app` (project `fpl-gaffer-2026`), built via `gcloud run deploy --source .` (Cloud Build, no local Docker needed)

## Observability
- [x] `app/observability.py` — `step()` context manager: started/succeeded/failed timestamped records with duration + structured detail, per `pipeline_log` row
- [x] Wired into every pipeline stage: `pipeline.fetch_data`, `pipeline.ml_predict` (full per-player predictions), `pipeline.sell_reports`, `pipeline.grounded_targets`, `debate.analyst`/`debate.fixture_form`/`debate.news_injury`/`debate.risk_scrutiny`/`debate.moderator` (each agent's FULL structured LLM output, not just the prose transcript), `debate.graph` (overall), `debate.backstop_validate` (accepted/rejected transfers + reasons), `captain.score`, `lineup.order_bench`, `execution.login`, `execution.submit_transfers`, `escalation.check`, `evaluate.score_candidates`, `evaluate.persona_calibration`
- [x] One `run_id` ties together every stage within a single `/internal/tick` invocation or a single approval-triggered execution
- [x] `GET /runs` (recent runs, failure counts) and `GET /runs/{run_id}` (full timeline with durations + structured detail) — `app/templates/runs.html`
- [x] Smoke-tested end-to-end: verified the success path captures structured detail, the failure path captures the error + full traceback and correctly re-raises (doesn't swallow errors), and both new routes render correctly via FastAPI's TestClient

## Docs / Obsidian
- [x] `docs/plan.md`, `docs/architecture.md`, `docs/progress.md`
- [x] `obsidian/` vault seeded

## Real News Search (post-deployment fix)
- [x] User caught a real gap after asking "where are we utilizing news in this": the whole pipeline only ever used `chance_of_playing_next_round` as a numeric proxy — the actual `news` text field, and any broader web/press-conference signal, was never used anywhere despite the `news_injury` persona's prompt claiming to consider "any news text in context"
- [x] Added FPL's own `news` text into `_sell_candidates_str`/`_format_grounded_targets` (`app/llm.py`) as a free baseline
- [x] Wired up real web search for actual news content (press conferences, "not seen training" reports, outlet coverage) — user's own words: "imagine the coach mentions something about a player or some news outlet mention player not seen training, we need information and context"
- [x] **Found the existing `fetch_player_context()` function was built on deprecated models** — `gpt-4o-search-preview`/`gpt-4o-mini-search-preview` were deprecated July 2026, confirmed live via an actual 404 rather than assumed from docs. Migrated to the current mechanism: Responses API with a `web_search` tool attached to `gpt-4.1-mini`, verified with a real call before committing to it (returned genuinely current, dated, cited results)
- [x] One batched search call per debate (all squad + grounded-target names together, ~35 players), not one per player — the tool bills a flat ~$0.025/call fee on top of token cost, so batching is what keeps this affordable (~$0.03/run total)
- [x] Cost tracked the same way as the debate's LLM calls — `pipeline.news_search` stage in `pipeline_log`, pricing verified via live search ($0.40/1M input, $1.60/1M output, $25/1,000 calls tool fee)
- [x] Verified end-to-end: real search returned dated (Aug 2026), sourced results; full pipeline run with the search wired in succeeded on all 30 steps, zero failures, total run cost $0.0345

## bot.py hardening (kept fully independent from the AI pipeline, per explicit instruction)
- [x] Diagnosed a real production incident: `bot.py` hadn't run since 2026-05-27 (a stale `cron-job.org` → GitHub API dispatch job was 404ing — root cause: fine-grained PAT missing "Actions: Read and write" permission for the repo). On manual re-trigger, it flooded 7 tweets at once (backlog of injury/return diffs accumulated over the 3-month gap).
- [x] Fixed the cron-job.org → GitHub dispatch 404 (PAT permission fix, confirmed via a real 204 response).
- [x] Added silent resync: `bot.py` now tracks `last_run_at` in `bot_state`; if dormant >6h (`RESYNC_GAP_HOURS`), the next run updates its injury-state baseline WITHOUT tweeting the backlog — normal diff-based tweeting resumes on the next run. Verified with two real local runs (Twitter creds unset, forced dry-run): first run correctly resynced silently (captured 2 real injuries as baseline, zero tweets), second immediate run correctly stayed in normal mode with no false resync trigger.
- [x] Caught and fixed a real operator-precedence bug in my own edit before it shipped (`chance == 100 or chance is None and not is_resync` silently ignored the resync gate for the `chance == 100` case due to Python's `and`/`or` precedence — fixed to `(chance == 100 or chance is None) and not is_resync`).
- [x] `bot.py` now needs a `db.close()` at exit (same `libsql_client` background-thread-hangs-the-process issue hit earlier with other CLI scripts) — added.
- [x] **Decision on separateness**: user wants bot.py kept separate from the AI pipeline in *logic/orchestration*, but explicitly chose to share the same Turso database rather than maintain a second separate store. `run_bot.yml` updated accordingly: minimal dependency install (`requests tweepy libsql-client` — NOT the full `requirements.txt`, keeping this job's footprint distinct from the pipeline's), `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` added as env vars, and the now-dead "commit `data/fpl_intel.db` back to git" step removed (state persists in Turso directly now) along with the `contents: write` permission it required (now `contents: read`).
- [ ] **Not yet live** — none of this is committed/pushed yet (see the broader "nothing this session is committed" note); `run_bot.yml` also needs `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` added as GitHub Actions repo secrets (separate from Cloud Run's env vars) before a real CI run will work.

## Debate window moved to T-12h (single pass, simplified)
- [x] User's ask: "can we run the pipeline 12 hrs before deadline? just so we don't miss any important last minute news." First implementation was a two-pass design (T-36h initial debate + T-12h final check that supersedes it if still unapproved) — built and verified working, then the user asked to simplify: "let's just keep 12 hr one, why have multiple? remove 36h."
- [x] Simplified to a single pass: `DEBATE_WINDOW_HOURS = 12` in `app/agents/pipeline.py` (was 36). The pipeline runs exactly once per gameweek, deliberately close to the deadline so the news search and ML predictions are grounded on fresh information — no supersede logic, no `is_final_check` plumbing, no second Telegram message. All of that two-pass machinery was built, verified working (32/32 steps, correct supersede behavior), then removed again per the simplification — noted here since the git history/diffs will show it appearing and disappearing.
- [x] Idempotency check reverted to the original simple form: one transfer decision per gameweek, full stop.

## Gameweek-targeting bug (found + fixed post-deployment)
- [x] User asked "was this run for gw1? based on what data points?" — surfaced a real bug: the pipeline used `get_current_gameweek()` (`is_current` — the already-locked, in-progress gameweek) for everything, while the debate-window check (`_hours_to_deadline`) correctly used `is_next`. An approved transfer would have tried to submit against a gameweek whose deadline had already passed.
- [x] Verified the fix carefully rather than assuming — confirmed live that FPL's picks endpoint 404s for a gameweek that isn't `is_current` yet (`/event/2/picks/` → `{"detail":"Not found."}` while `/event/1/picks/` returns real data), so the fix keeps the picks-fetch on the current gameweek while everything else (fixture lookahead, budget/free-transfer math, decision labeling, transfer submission's `event` field) now correctly targets the *next* gameweek (`_next_gameweek_id()`, `app/agents/pipeline.py`)
- [x] Re-ran the full pipeline after the fix — correctly resolved to GW2 (previously incorrectly GW1), squad still fetched successfully, all 28 steps succeeded

## Deployment — DONE, live in production

- **Service**: `https://fpl-gaffer-283700541620.us-central1.run.app` (GCP project `fpl-gaffer-2026`, region `us-central1`)
- **Turso**: connected via `https://` scheme (not `libsql://` — that failed its WebSocket handshake during testing), verified end-to-end including from Cloud Run itself
- **cron-job.org**: configured, firing `POST /internal/tick` every 30 min with `X-Cron-Secret` header, verified reaching the app and producing real logged runs
- **Telegram webhook**: registered via `setWebhook`, verified rejecting wrong secrets and correctly routing button taps to `_approve()`/`_reject()`
- **GitHub Actions** (`train_model.yml`): unaffected by any of this, still handles weekly retraining independently

**Remaining before this can be fully trusted unattended**: (1) a full pipeline run has only been *forced* (bypassing the 36h window) — the automatic trigger during a real deadline window hasn't happened yet, though there's now real reason for confidence given the forced runs succeeded completely; (2) `model.pkl` is baked into the image at deploy time — a future `train_model.yml` run needs a Cloud Run redeploy to actually take effect, not yet automated; (3) FPL login/execution has still never been exercised against the real account (deliberately, per the plan).

## What's verified so far (this session)
- All new/modified Python files compile (`py_compile`)
- New dependency versions (`scikit-learn==1.5.2`, `langgraph==0.2.53`, `langchain-openai==0.2.9`, `libsql-client==0.3.1`) resolve and install cleanly
- FastAPI app imports cleanly with all new routes registered, including `/internal/tick`
- LangGraph debate graph compiles and runs correctly end-to-end with a mocked LLM, including the hit-scrutiny conditional loop (capped at exactly 1 extra round) and crash-safe per-turn DB logging
- `score_captain()`/`order_bench()` smoke-tested; one real bug found and fixed (rotation-risk floor bypass)
- Extension JS files pass `node --check`
- Rewritten `app/database.py` smoke-tested end-to-end against a local libSQL file (init, insert, read, update, close) — table schema, row access, and every accessor function's signature confirmed compatible with the sqlite3 → libsql_client swap
- `/internal/tick`'s `X-Cron-Secret` auth check verified (rejects wrong secret, accepts correct one)
- Telegram escalation channel verified live — real bot created, real message delivered to a real chat ID

## What's explicitly NOT verified yet
- No real OpenAI API call has been made (all agent testing used a mocked LLM)
- No FPL login has been attempted (would require real credentials)
- No end-to-end run against a live manager's squad
- ~~Never tested against real Turso~~ — **now verified** (see Database section above)
- Docker image has not been built (no Docker daemon available in this session) — Dockerfile is straightforward (pip install + copy + uvicorn) but unverified
- Cloud Run has not been deployed; cron-job.org has not been configured

**Do not point this at a real FPL account/deadline until: (1) the execution path has been dry-run tested per the plan's verification checklist, and (2) the Docker image has been built and deployed successfully.** (Turso is now verified — see above.)
