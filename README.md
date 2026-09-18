# FPL Gaffer

Two independent systems for Fantasy Premier League:

1. **Autonomous decision engine** — every gameweek, inside the last 12 hours before the deadline, a trained expected-points model, rule-based transfer scoring and a LangGraph multi-agent debate propose transfers, captain and starting XI. You approve or reject from Telegram; approved decisions execute against your real FPL team through FPL's OAuth-protected API. Runs on Google Cloud Run, triggered by an external cron, backed by Turso.
2. **Twitter news bot** (`bot.py`) — a separate script that tweets deadline reminders, DGW/BGW alerts, injury updates and "Kings of the Gameweek". Runs on GitHub Actions and shares only the Turso database with the engine.

`docs/` and `obsidian/` hold the design reasoning and are deliberately gitignored (local process notes). This README is the tracked description of how the thing works.

## Stack

- Python 3.11, FastAPI, Jinja2
- **Turso** (libSQL over HTTPS) for all state — decisions, debate transcripts, candidates, calibration, pipeline observability, bot state
- **scikit-learn** `HistGradientBoostingRegressor` for expected points, trained on [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League) with labels recomputed from FPL's current scoring rules
- **LangGraph** + OpenAI `gpt-4o-mini` for the transfer debate (analyst, fixture/form, news/injury, risk/scrutiny, rebuttal, moderator)
- OpenAI Responses API + `web_search` on `gpt-4.1-mini` for one batched news search per run
- **Telegram Bot API** for approvals (inline buttons) and escalation alerts
- **Google Cloud Run** (`us-central1`), deployed automatically by GitHub Actions on every push to `main` that touches `app/`, `Dockerfile` or `requirements.txt`
- **cron-job.org** POSTs `/internal/tick` every 30 minutes
- Weekly model retrain on GitHub Actions (Tuesday 06:00 UTC); a promoted model is committed to `app/ml/` and that push triggers a deploy

## Project layout

```
app/
  main.py               FastAPI: /, /decisions/{id}, /runs, /runs/{run_id},
                        /api/decisions/{id}/approve|reject, /telegram/webhook, /internal/tick
  fpl_client.py         FPL API, fixtures, directional FDR, team form/strength, budget,
                        replacement search
  fpl_auth.py           PingOne OIDC refresh-token exchange, /my-team/, transfer + lineup submit
  ranking.py            Sell/buy scoring, clean-sheet + defensive-contribution estimates,
                        hit gate, lineup points, best XI, captain, bench order
  llm.py                Debate context formatting, name resolution, transfer validation, news search
  notify.py             Telegram approval messages + escalation
  observability.py      step() context manager -> pipeline_log
  pricing.py            OpenAI cost estimation
  database.py           Turso persistence (6 tables)
  models.py             Pydantic models (PlayerSummary, Fixture, BudgetInfo, ...)
  agents/
    pipeline.py         Weekly orchestration: data -> predictions -> sell/buy -> debate ->
                        backstop -> projected squad -> lineup
    graph.py            LangGraph debate + the precomputed FACTS table each proposal carries
    personas.py         System prompts + structured-output schemas
    evaluate.py         Post-gameweek scoring of every candidate + persona calibration
    escalation_check.py Unapproved / unexecuted decision alerts
  ml/
    features.py         Feature schema (v4), clean-sheet probability, live feature builder
    train.py            Training from vaastav CSVs, monotonic constraints, promotion gate
    scoring_rules.py    FPL's current points table, validated to 0 mismatches on real rows
    model.py            Inference wrapper, falls back to ep_next
    model.pkl / model_meta.json
  templates/            home, decisions, runs
bot.py                  Twitter bot (independent)
scripts/fpl_capture_refresh_token.py   one-off local login to seed the OAuth refresh token
.github/workflows/      deploy_cloud_run.yml, train_model.yml, run_bot.yml
```

## How a gameweek is decided

`POST /internal/tick` runs synchronously (Cloud Run only gives CPU during a request; the timeout is 1800s) and does three things: the weekly pipeline (once per gameweek, only within `DEBATE_WINDOW_HOURS = 12` of the deadline), the escalation check, and evaluation of the last finished gameweek. Every stage logs to `pipeline_log` under one `run_id`, visible at `/runs`.

### 1. Data and budget
Squad comes from FPL's picks endpoint for the *current* (locked) gameweek, since FPL 404s picks for the upcoming one; everything else targets the *next* gameweek. Free transfers and transfers made come from the authenticated `/my-team/` `transfers` block (FPL's own `limit` / `made`), with a derived fallback that no longer counts last week's transfers against this week.

### 2. Expected points
`app/ml/model.pkl` predicts next-gameweek points for the squad and, later, every verified transfer target. Schema v4 features: form, team ppg and goal difference, opponent strength for this fixture, last-season points/90, xGI/90, xGC/90, next-3 directional FDR, starts%, FPL's `ep_next`, position one-hots, this fixture's clean-sheet probability, saves/90 and defensive contributions/90. Holdout MAE 1.96 against FPL's own `ep_next` at 2.60. `ep_next` is still the dominant feature.

### 3. Sell candidates and verified targets
`ranking.score_sell` ranks all 15 (XI guaranteed 5 of 8 slots) on injury, form level and trend, `ep_next`, clean-sheet outlook for defenders, fixtures, minutes share and price momentum. For each candidate `find_valid_replacements` returns 8 affordable, same-position, different-club, not-recently-sold targets ranked by `ranking.score_buy`, which anchors on minutes-weighted xGI/90 for attackers and on expected clean-sheet points (Poisson on xGC/90 scaled by the fixture) plus defensive contributions for defenders, with `ep_next` at a reduced weight and the fixture swing against the outgoing player.

### 4. Debate
One batched news search, then the LangGraph debate: analyst proposes up to the free-transfer count → fixture/form, news/injury and risk/scrutiny argue → an extra scrutiny round if a hit is involved → the analyst rebuts → the moderator decides at temperature 0. Each proposal carries a precomputed FACTS table (model xP, xGI/90, minutes share, next-3 FDR, clean-sheet outlook and record for defenders, `ep_next`, price) because gpt-4o-mini cannot be trusted to compare two numbers in prose. `is_hit` is stamped from the free-transfer count, not by the LLM.

### 5. Backstop
The moderator's output is re-validated deterministically: names, position, club, budget, and for any move beyond the free count a breakeven gate (model xP delta × 3 gameweeks × 0.5 regression + fixture swing ≥ 4).

### 6. Lineup
`ranking.build_lineup` computes one number per player for this gameweek — model prediction × fixtures this week (0 on a blank, 2 on a double), blended 50/50 with a scoring-rules estimate for defenders and keepers, × chance of playing — and the XI (all 8 legal formations brute-forced), captain (plus a ceiling bonus for penalty takers and high xGI/90) and bench order (weighted by which starters each sub can legally replace under FPL's auto-sub rules) all rank on it. The XI is chosen for the squad the transfer would leave you with; picks are held until the transfer decision is terminal and reconciled against the live squad at submission.

### 7. Approval, execution, feedback
Every decision goes to Telegram with Approve/Reject buttons (`/decisions/{manager_id}` is the backup surface). Unapproved past deadline−3h or unexecuted past deadline−1h triggers an alert. Execution exchanges the stored OAuth refresh token for an access token and submits; captain and lineup share one `/my-team/` call so both must be approved first. After the gameweek, every candidate the debate considered is scored against actual points and each persona's stance marked right or wrong, feeding a calibration caveat into the next debate.

## Operations

**Rerun a gameweek after a code change.** The pipeline is idempotent per gameweek. Order matters: deploy first (wait for the workflow), then purge the gameweek from Turso in FK order (`persona_calibration`, `decision_candidates` by decision id; `agent_conversations`, `pipeline_log`, `agent_decisions` by gameweek), then trigger. The cron fires at :00 and :30, so a purge just before a boundary is picked up by the cron on whatever is deployed.

```bash
curl -X POST https://fpl-gaffer-283700541620.us-central1.run.app/internal/tick -H "X-Cron-Secret: $CRON_SECRET"
```

**Query production Turso.** Use the HTTP pipeline API rather than `libsql_client` from a local script: `POST $TURSO_DATABASE_URL/v2/pipeline` with `Authorization: Bearer $TURSO_AUTH_TOKEN` and `{"requests":[{"type":"execute","stmt":{"sql":"..."}},{"type":"close"}]}`. If you do use `libsql_client` in a script, call `db.close()` or the process never exits.

**Local scripts and the production DB.** `app/main.py` calls `load_dotenv()` on import, so pop `TURSO_DATABASE_URL` *after* importing it and assert `db._client is None` before touching the DB, or your test writes go to production.

## FPL authentication

FPL login is PingOne OIDC. Log in once yourself:

```bash
pip install playwright && playwright install chromium
python scripts/fpl_capture_refresh_token.py
```

The script reads the refresh token FPL's own site stores in `localStorage` and saves it to Turso. `app/fpl_auth.py` exchanges it for a fresh ~8h access token before every execution (PingOne rotates the refresh token on each use; the new one is persisted). Playwright is not a production dependency.

## Running locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# .env
OPENAI_API_KEY=...
FPL_MANAGER_ID=...
TURSO_DATABASE_URL=https://...   # omit to use a local SQLite file (dev only)
TURSO_AUTH_TOKEN=...
CRON_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TELEGRAM_WEBHOOK_SECRET=...
TWITTER_*=...                     # bot.py only; dry-run without them

uvicorn app.main:app --reload
python -m app.agents.pipeline --dry-run    # builds the debate context, no LLM calls
python -m app.agents.pipeline --force      # full run regardless of the deadline window
python -m app.ml.train                     # retrain (gated unless the feature schema changed)
python bot.py
```

## Twitter bot (`bot.py`)

| Tweet | Trigger |
|---|---|
| ⏰ Deadline incoming | Next deadline ≤ 12h away, once per GW |
| 🔥 DGW confirmed | Any team with 2+ fixtures in next event |
| 🚫 BGW incoming | Team missing from next event (guarded) |
| 🏥 Injury / ✅ Return | Owned >5%, chance changed since last run |
| 👑 Kings of the Gameweek | Finished GW top scorers, 💎 for sub-5% owned |

If dormant more than `RESYNC_GAP_HOURS` (6h) it resyncs its injury baseline silently instead of tweeting the backlog. Triggered by cron-job.org dispatching `run_bot.yml`.

## Configuration

- Debate window: 12h before deadline (`DEBATE_WINDOW_HOURS`)
- Approval cutoff alert: deadline−3h; failsafe: deadline−1h
- Bootstrap/fixtures cache: 5 minutes
- Model retrain: Tuesday 06:00 UTC, promotion gate 5% MAE tolerance, skipped when `FEATURE_SCHEMA_VERSION` changes
