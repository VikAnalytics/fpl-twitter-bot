# FPL Gaffer

Two independent systems for Fantasy Premier League:

1. **Autonomous multi-agent decision engine** — an ML model + a LangGraph multi-agent debate propose transfers, captain, and starting XI each gameweek; you approve or reject from Telegram; approved transfers execute automatically against your real FPL team. Deployed on Google Cloud Run, triggered by an external cron, backed by Turso.
2. **Twitter news bot** (`bot.py`) — a separate, simpler script that tweets deadline reminders, DGW/BGW alerts, injury updates, and "Kings of the Gameweek." Runs on GitHub Actions, deliberately independent of the decision engine (own trigger, own dependency footprint, shares only the underlying Turso database for state).

See [`docs/architecture.md`](docs/architecture.md) for the full system design, [`docs/progress.md`](docs/progress.md) for build/verification status, and the `obsidian/` vault for the design-decision reasoning trail (open the folder as an Obsidian vault).

## Stack

- Python 3.11, FastAPI, Jinja2
- **Turso** (libSQL, SQLite-wire-compatible) for all state — decisions, conversations, pipeline observability, bot state
- **LangGraph** + OpenAI `gpt-4o-mini` for the transfer debate (5 personas: analyst, fixture/form, news/injury, risk/scrutiny, moderator)
- **scikit-learn** (`HistGradientBoostingRegressor`) for the expected-points model, trained on [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League) historical data
- OpenAI Responses API + `web_search` tool (`gpt-4.1-mini`) for real news search (press conferences, training reports, outlet coverage)
- **Telegram Bot API** for approval (inline buttons) and hard escalation alerts
- **Google Cloud Run** hosting, deployed via `gcloud run deploy --source .` (Cloud Build, no local Docker needed)
- **cron-job.org** (external, free) triggers `/internal/tick` every 30 minutes — deliberately not GitHub Actions' `schedule:` trigger, which is known to be delayed/skipped under load
- Twitter via `tweepy` for `bot.py`, triggered by `cron-job.org` dispatching a GitHub Actions workflow

## Project layout

```
app/
  main.py           FastAPI routes: brief, decisions view, approve/reject,
                     /internal/tick webhook, /telegram/webhook, /runs observability
  fpl_client.py     FPL API calls, fixtures, team form/strength, replacements
  fpl_auth.py       Unofficial FPL login + transfer/lineup submission
  ranking.py        Sell/buy scoring, best-XI selection, captain, bench order
  llm.py            Legacy single-agent brief generator (prompt builders, validators)
  notify.py         Telegram: escalation alerts + decision approval messages
  observability.py  Structured per-run pipeline logging (step() context manager)
  pricing.py        OpenAI token/call cost estimation
  database.py       Turso-backed persistence (all tables)
  models.py         Pydantic models
  agents/           LangGraph transfer debate, XI/captain selection, evaluation,
                     escalation checks, weekly pipeline orchestration
  ml/               Feature engineering, model training, scoring-rules table,
                     inference wrapper
  templates/        Jinja views: /, /audit, /brief, /decisions, /runs
bot.py              Twitter news bot (standalone, independent of app/agents/)
docs/               Living architecture/plan/progress docs
obsidian/           Design-decision reasoning trail (Obsidian vault)
Dockerfile          Cloud Run build
```

## Autonomous decision engine

Runs once per gameweek, close to the deadline (`DEBATE_WINDOW_HOURS = 12` in
`app/agents/pipeline.py`) so the debate is grounded on fresh information:

1. **Data + ML** — fetches your squad, fixtures, team form; runs every squad
   player through the trained model (`app/ml/model.pkl`) for predicted points.
   Falls back to FPL's own `ep_next` if no model is trained yet.
2. **Real news search** — one batched web search (all squad + grounded-target
   names, ~$0.03/run) via `gpt-4.1-mini` + `web_search`, covering press
   conferences, training-ground reports, and outlet coverage — not just FPL's
   own terse `news` field.
3. **Transfer debate** (`app/agents/graph.py`) — LangGraph state machine:
   analyst proposes → fixture/form and news/injury argue for/against →
   risk/scrutiny challenges (looping for one extra round if the proposal
   takes a point hit) → moderator decides. A deterministic backstop
   (budget/position/club/hit-breakeven) re-validates the LLM's output before
   anything reaches approval — the LLM never has unchecked authority.
4. **Best XI + captain + bench** (`ranking.py`) — deterministic, no LLM debate
   (near-argmax problems don't need one): `select_best_xi()` brute-forces all
   8 legal FPL formations and keeps whichever maximizes total predicted
   points, then captain/vice and bench order are derived from that XI.
5. **Approval** — every decision (transfer, captain, lineup) is sent to
   Telegram with inline Approve/Reject buttons. `/decisions/{manager_id}`
   works as a secondary web surface. Unapproved decisions past deadline−3h,
   or approved-but-unexecuted ones past deadline−1h, trigger a hard Telegram
   escalation (`app/agents/escalation_check.py`).
6. **Execution** — on approval, `app/fpl_auth.py` logs into FPL (unofficial
   endpoints — FPL has no official transfer API) and submits. Captain/lineup
   share one `/my-team/` call, so approving one waits for its sibling before
   submitting the combined payload.
7. **Feedback loop** — after the gameweek plays out, every candidate the
   debate considered (not just the winner) is scored against actual points,
   and each persona's stance is marked correct/incorrect — feeding a
   calibration caveat into next week's debate.

Every stage is logged to `pipeline_log` with duration, token usage, and cost —
`GET /runs` and `GET /runs/{run_id}` surface the full trace for debugging a
bad decision or a silent failure.

## Legacy single-agent brief (`app/llm.py`)

The original read-only advisory pipeline still exists — `/brief/{manager_id}`
(web) and `/api/brief/{manager_id}` (JSON) generate a narrative + suggested
transfers for any manager ID, without executing anything:

1. **Fetch + enrich squad** (`build_squad_picks`) — xG, xA, xGI/90, starts %,
   set-piece orders, directional FDR per fixture.
2. **Score sell candidates** (`ranking.score_sell`) — injury doubt,
   suspension risk, form trend, ep_next, fixtures, rotation risk, xG
   over-performance, price momentum. Top 5 by urgency.
3. **Ground buy targets** (`find_valid_replacements`) — filtered by position/
   club/budget/minutes/availability, ranked by `ranking.score_buy`.
4. **Generate brief via LLM**, then **validate every suggestion** post-hoc in
   code (not prompt-side) — position/club/budget/hit-breakeven checks
   identical in spirit to the new pipeline's backstop.
5. **Feedback loop** — past suggestions matched against real outcomes,
   confidence thresholds tighten if the recent track record is poor.

## Twitter bot (`bot.py`)

Independent of the decision engine — own trigger, own minimal dependency set
(`requests`, `tweepy`, `libsql-client`), shares only the Turso database for
state (`bot_state` table).

| Tweet | Trigger |
|---|---|
| ⏰ Deadline incoming | Next deadline ≤ 12h away, once per GW |
| 🔥 DGW confirmed | Any team with 2+ fixtures in next event, once per GW |
| 🚫 BGW incoming | Team missing from next event; guarded by deadline ≤ 7 days AND ≥ 7 fixtures present |
| 🏥 Injury | Owned >5%, chance < 100%, status changed since last run |
| ✅ Return | Previously flagged player now at 100% |
| 👑 Kings of the Gameweek | Finished GW, top scorers from `/event/{id}/live/`, 💎 flags sub-5% differentials |

**Silent resync**: if the bot has been dormant more than `RESYNC_GAP_HOURS`
(6h, tracked via `last_run_at` in `bot_state`), the next run updates its
injury-state baseline without tweeting the accumulated backlog — prevents a
flood of stale "news" after a long gap (this happened for real: a broken
cron-job.org → GitHub Actions dispatch job left it dormant for 3 months,
and re-triggering it fired 7 tweets at once for changes that weren't
actually new).

Triggered by `cron-job.org` calling the GitHub Actions dispatch API directly
(`POST /repos/{owner}/{repo}/actions/workflows/run_bot.yml/dispatches`) —
requires a token with `Actions: Read and write` permission for the repo.

## Deployment

1. **Turso**: `turso db create <name>`, `turso db tokens create <name>`. Use
   the `https://` URL scheme, not `libsql://` (the WebSocket/Hrana scheme
   fails its handshake against a request/response server pattern).
2. **Cloud Run**: `gcloud run deploy fpl-gaffer --source . --region <region> --allow-unauthenticated --set-env-vars <see below>`
3. **cron-job.org**: a job hitting `POST https://<cloud-run-url>/internal/tick`
   every 30 minutes with header `X-Cron-Secret: <CRON_SECRET>`.
4. **Telegram webhook**: `GET https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<cloud-run-url>/telegram/webhook&secret_token=<TELEGRAM_WEBHOOK_SECRET>`
5. **GitHub Actions secrets** (for `run_bot.yml` and `train_model.yml`):
   `TWITTER_CONSUMER_KEY/SECRET`, `TWITTER_ACCESS_TOKEN/SECRET`,
   `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`.

## Running locally

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# .env
OPENAI_API_KEY=...
FPL_MANAGER_ID=...
FPL_EMAIL=...              # for autonomous execution — real FPL login
FPL_PASSWORD=...
TURSO_DATABASE_URL=...     # https:// scheme — omit to fall back to a local SQLite file (dev only)
TURSO_AUTH_TOKEN=...
CRON_SECRET=...            # shared secret for /internal/tick
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TELEGRAM_WEBHOOK_SECRET=...
# for bot.py (optional — dry-run without these):
TWITTER_CONSUMER_KEY=...
TWITTER_CONSUMER_SECRET=...
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_TOKEN_SECRET=...

uvicorn app.main:app --reload
python bot.py                              # dry-run if Twitter creds missing
python -m app.agents.pipeline --dry-run    # preview the debate context without running it
python -m app.agents.pipeline --force      # run the full pipeline regardless of deadline window
python -m app.ml.train                     # (re)train the expected-points model
```

## Configuration

- Brief cache: 2 hours per (manager_id, GW)
- Daily LLM brief limit per manager: 5 (`DAILY_BRIEF_LIMIT`)
- Bootstrap/fixtures in-memory cache: 5 minutes
- Debate window: 12h before deadline (`DEBATE_WINDOW_HOURS`)
- Approval cutoff: deadline−3h; failsafe alert: deadline−1h (`app/agents/escalation_check.py`)
- bot.py resync threshold: 6h (`RESYNC_GAP_HOURS`)
