# Architecture — Autonomous Multi-Agent FPL Decision Engine

Living design doc. Update this whenever a component's shape changes — see [progress.md](progress.md) for build status and [plan.md](plan.md) for the frozen original plan.

## System diagram

```
┌───────────────────────────────────────────────────────────────────────┐
│ cron-job.org (external, every 30min) --POST--> /internal/tick         │
│   header: X-Cron-Secret: <shared secret>                              │
│   (replaces GitHub Actions' unreliable `schedule:` trigger)           │
└───────────────────────────────┬─────────────────────────────────────┘
                                 ▼
   FastAPI app (Cloud Run, long-lived — see "Hosting" below)
   returns 202 immediately, runs the tick as a BackgroundTask,
   guarded by an in-process lock against overlapping triggers
                                 ▼
1. DATA INGESTION (app/fpl_client.py)
   FPL API: bootstrap-static, fixtures, element-summary, entry/picks
   + vaastav/Fantasy-Premier-League historical CSVs (training only)
                                 ▼
2. FEATURES + ML MODEL (app/ml/)
   features.py: player_form, team_form, opponent, last_season_form, news
   model.py: predict_points() — loads model.pkl, falls back to ep_next
   train.py: HistGradientBoostingRegressor, promotion-gated on holdout MAE
   (retrained weekly by a SEPARATE GitHub Actions cron — see below)
                    ┌────────────┴────────────┐
                    ▼                          ▼
3a. TRANSFER DEBATE (app/agents/)      3b. XI + CAPTAIN + LINEUP (app/ranking.py)
    LangGraph: analyst -> fixture_form     select_best_xi() — formation-aware,
    -> news_injury -> risk_scrutiny        brute-forces all 8 legal FPL
    (loop once more if hit/high-risk)      formations, then score_captain()
    -> moderator                           + order_bench() on that XI.
                                            Deterministic, no LLM call.
    deterministic backstop after graph
    (budget/position/club/hit-breakeven,
     reused from app/llm.py)
                    └───────────────┬───────────────────┘
                                 ▼
4. APPROVAL GATE (app/main.py, deadline-3h cutoff)
   Every decision (transfer, captain, lineup) sent to Telegram with inline
   Approve/Reject buttons — tap one, done. POST /telegram/webhook handles
   the button tap; /decisions/{manager_id} web page works too as a backup.
   escalation_check.py fires a Telegram alert if unapproved past cutoff
                                 ▼ (on approval)
5. EXECUTION (app/fpl_auth.py, invoked from the approval route/webhook)
   Transfers: login() -> session cookie -> submit_transfers()
   Captain+Lineup: share ONE /my-team/ call — approving one waits until
   BOTH are approved, then submits the combined 15-pick payload via
   set_lineup() (captain/vice flags, XI order, bench priority w/ GK last)
   retries w/ backoff; failure -> immediate Telegram escalation
                                 ▼ (after GW plays out, same tick)
6. FEEDBACK & VERIFICATION LOOP (app/agents/evaluate.py)
   decision_candidates: chosen vs sold vs alternates, actual points
   persona_calibration: was each agent's stance vindicated?
   -> injected as context into next week's debate + informs ML training

   All decision state (agent_decisions, agent_conversations,
   decision_candidates, persona_calibration, bot_state, etc.) lives in
   ONE Turso database — no more split-brain between GitHub Actions and
   the app (see "Resolved: split-brain DB" below).
```

## Hosting

- **App**: Google Cloud Run, deployed from the repo's `Dockerfile`. Free-tier eligible at this project's traffic (~1,440 requests/month from cron-job.org, well under Cloud Run's 2M free requests/month). Scales to zero between requests.
- **Scheduling**: cron-job.org (external, free) POSTs to `/internal/tick` every 30 minutes, replacing GitHub Actions' `schedule:` trigger, which is known to be delayed or skipped under load. Request is authenticated via a shared-secret header (`X-Cron-Secret`), checked against the `CRON_SECRET` env var.
- **Approval surface**: Telegram (inline Approve/Reject buttons on every decision, via `/telegram/webhook`) and the standalone `/decisions/{manager_id}` web page (`app/templates/decisions.html`) as a secondary surface. The Chrome extension that originally shipped with this repo was removed entirely — user opted out of using it as an approval surface, then asked for it to be deleted rather than kept around unused.
- **DB**: Turso (libSQL, SQLite-wire-compatible), reached via `app/database.py`'s `libsql_client.ClientSync`. Falls back to a local SQLite file (`data/fpl_intel.db`) when `TURSO_DATABASE_URL` is unset — dev-only, not persistent on Cloud Run.
- **Model retraining**: stays on GitHub Actions (`.github/workflows/train_model.yml`), a weekly job unaffected by the `schedule:` reliability concern since occasional delay there is low-stakes (unlike a missed transfer deadline). Doesn't touch the DB at all — writes `app/ml/model.pkl` and commits it. **Note**: Cloud Run bakes `model.pkl` into the image at build time, so a new model requires a Cloud Run redeploy to take effect — not yet automated (see [[Known Gaps]]).

## File map

| Area | Files |
|---|---|
| Data ingestion | `app/fpl_client.py` (extended: `build_team_form`, `fetch_player_history_past`) |
| ML | `app/ml/features.py`, `app/ml/model.py`, `app/ml/train.py` |
| Deterministic scoring | `app/ranking.py` (extended: `score_captain`, `order_bench`) |
| Transfer debate | `app/agents/state.py`, `personas.py`, `graph.py`, `pipeline.py` |
| Feedback loop | `app/agents/evaluate.py` (`run_evaluate()` — pure, callable from the server; `main()` — CLI wrapper that owns DB lifecycle) |
| Escalation | `app/agents/escalation_check.py`, `app/notify.py` (Telegram) |
| Execution | `app/fpl_auth.py` |
| Observability | `app/observability.py` (`step()` context manager), `app/pricing.py` (token cost), `app/templates/runs.html`, `GET /runs`, `GET /runs/{run_id}` |
| Web/API | `app/main.py` (approve/reject routes, `/decisions/{manager_id}`, `/internal/tick` webhook, `/telegram/webhook`, `/runs`), `app/templates/decisions.html` |
| Storage | `app/database.py` — Turso-backed (`libsql_client`), local-file fallback for dev |
| Deploy | `Dockerfile` (Cloud Run) |
| Automation | `.github/workflows/train_model.yml` (weekly retrain only — the decision pipeline itself is no longer a GitHub Actions workflow) |

## Why these choices (design-review highlights)

- **Captain/lineup are deterministic, not debated.** Captain selection is effectively `argmax(predicted_points × chance_of_playing)` over nailed starters — a 5-agent LLM debate adds cost/latency for near-zero decision-quality gain versus a sort. Full debate is reserved for transfers, where budget/fixture/risk trade-offs are genuinely multi-sided.
- **Telegram over tweet-only for escalation.** A tweet isn't guaranteed to be seen within the "≥1h before deadline" requirement. Telegram's official Bot API sends directly to a phone, is genuinely free, and needs no reverse-engineered integration (WhatsApp via CallMeBot and X/Twitter DMs were both considered and ruled out — see [[Decisions Log]] in the Obsidian vault).
- **Promotion-gated model retraining.** A weekly retrain that silently overwrites a working model with a worse one (bad data week, degenerate fit) would regress prediction quality with no signal. `model.pkl` is only replaced if holdout MAE isn't >5% worse than the currently-deployed model's.
- **Deterministic backstop after every LLM debate.** The LangGraph moderator's decision is never trusted directly — `app/llm.py`'s existing `_validate_transfer`/`hit_breakeven_ok` checks (budget, position, club, real-player, profitability) run as a hard gate before anything reaches approval, exactly as they already do for the single-agent brief.
- **cron-job.org over GitHub Actions' `schedule:` trigger.** GitHub's own docs note scheduled workflows can be delayed or dropped under load — unacceptable given the "never miss a deadline" requirement. An external cron hitting a webhook on an always-reachable app is more reliable, and as a side effect collapses scheduling and execution onto one long-lived process instead of two independent ones.
- **In-process lock instead of a workflow `concurrency:` guard.** Since the pipeline now runs inside a single FastAPI process rather than as independent GitHub Actions jobs, a `threading.Lock` around `_run_tick()` does the same job GitHub Actions' `concurrency: { group: ... }` used to.
- **Turso over Railway/plain SQLite files.** Cloud Run's filesystem is ephemeral (doesn't persist across scale-to-zero), so a local SQLite file silently loses state. Turso is SQLite-wire-compatible (same SQL, same `?` placeholders, same row access pattern) reached over the network, so `app/database.py`'s rewrite was a connection-layer swap, not a schema/query rewrite.

## Resolved: split-brain DB (previously an open gap)

Earlier drafts of this doc flagged that GitHub Actions and the previously-planned Railway deployment would each hold their own SQLite copy, with approvals made on one side invisible to the other. Moving the decision pipeline itself off GitHub Actions and onto the same long-lived process that serves the approval routes — backed by one Turso database instead of git-committed files — removes the split entirely: there is now exactly one process, one database, one source of truth for decision state.

## Observability

Every stage of a run — data fetch, ML prediction (with every player's predicted points), each debate agent's full structured LLM output (not just the human-readable transcript in `agent_conversations`), backstop validation (which transfers were accepted/rejected and why), captain/lineup scoring, execution (login/submit), escalation checks, and evaluation — is wrapped in `app/observability.step()`, which logs a `started` row immediately and a `succeeded` (with structured detail) or `failed` (with the error + traceback) row to the `pipeline_log` table.

All stages within one `/internal/tick` invocation (or one approval-triggered execution) share a single `run_id`, so a failure can be traced end-to-end: `GET /runs` lists recent runs with step counts and failure flags, `GET /runs/{run_id}` shows the full timeline with durations and structured detail per stage. This exists specifically so a bad decision or a silent failure can be diagnosed from the exact stage it happened in, not just an unstructured `print()` in Cloud Run's log stream.

## Known gap: model artifact staleness on Cloud Run

`train_model.yml` commits a new `app/ml/model.pkl` weekly, but Cloud Run's image is built once at deploy time — it won't pick up a new model until redeployed. Not yet automated (e.g. via a Cloud Build trigger on push, or a follow-up step in `train_model.yml` that calls `gcloud run deploy`). Until then, predictions keep using whichever model was baked in at the last manual deploy. See [[Known Gaps]].
