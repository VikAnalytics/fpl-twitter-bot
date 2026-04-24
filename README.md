# FPL Intel

FPL (Fantasy Premier League) assistant with three surfaces:

1. **Chrome extension** — shadow-DOM sidebar that injects into `fantasy.premierleague.com` and shows a grounded pre-deadline brief for the logged-in manager.
2. **FastAPI backend** — JSON API (`/api/brief/{manager_id}`) plus a Jinja web view. Caches briefs for 2 hours and rate-limits LLM calls.
3. **Twitter bot** — scheduled GitHub Action that posts deadline alerts, DGW/BGW detection, injury updates, recoveries, and Kings-of-the-Gameweek.

## Stack

- Python 3.10+, FastAPI, Jinja2, SQLite at `data/fpl_intel.db`
- OpenAI `gpt-4o-mini` for transfer briefs
- Railway hosting (`railway.toml` + `Procfile`)
- Chrome MV3 extension (shadow DOM, configurable backend URL)
- Twitter via `tweepy`, scheduled by `.github/workflows/run_bot.yml`

## Project layout

```
app/
  main.py          FastAPI routes, _build_brief orchestrator, brief cache
  fpl_client.py    FPL API calls, fixture + team strength builders, replacements
  ranking.py       Sell/buy scoring, phase weighting, hit-breakeven, outcomes
  llm.py           Prompt builders, validators, deterministic confidence/budget
  database.py      SQLite: managers, brief_cache, bot_state, rate_limits, outcomes
  models.py        Pydantic models (PlayerSummary with xG/xA/set-pieces/etc)
  cache.py         5-min in-memory cache for bootstrap + fixtures
  templates/       Jinja views for /, /audit, /brief
bot.py             Twitter bot entry point (standalone script)
extension/         Chrome MV3 extension (content.js sidebar, background.js)
```

## Transfer suggestion pipeline

How the app picks who to transfer OUT and who to transfer IN:

1. **Fetch + enrich squad** (`build_squad_picks`). Each `PlayerSummary` carries
   xG, xA, xGI/90, starts %, yellow cards, set-piece orders, price-change
   momentum, plus directional FDR per fixture (position-aware — uses opponent
   defence strength for MID/FWD, attack strength for GKP/DEF).

2. **Score sell candidates** (`ranking.score_sell`). Urgency factors:
   injury doubt, suspension risk, form trend (last 5 GWs), ep_next, directional
   FDR, rotation risk (starts_pct), xG over-performance (regression risk),
   and price drop momentum. Top 5 by urgency become sell candidates.

3. **Ground buy targets per sell** (`find_valid_replacements`). For each sell
   candidate:
   - Filter: same FPL position, not same club, not owned, not sold within last
     3 GWs (flip-flop guard), price ≤ sell + ITB, minutes floor, >50% chance of
     playing.
   - First-pass rank: `ranking.score_buy` — ep_next, form trend, xGI/90,
     directional FDR, set pieces (pen/FK/corner orders), minutes reliability,
     price rise momentum, ep delta vs sold, role similarity (xGI axis),
     clean-sheet potential (DEF/GKP), ownership.
   - Top ~15 candidates enriched with 5-GW form history (parallel fetch).
   - Re-rank with form trend available, return top 8.

4. **Generate brief via LLM**. Prompt fed sell candidates (with flags),
   grounded targets (with scores), injury/DGW/BGW context, chip state, season
   phase (EARLY ≤ GW5 / MID / LATE ≥ GW30), past-outcome caveat if recent
   track record poor. Model returns JSON transfers using web_name only.

5. **Validate every suggestion** (post-hoc, code-side, not prompt-side):
   - Player resolves (handles `F.Kadıoğlu` vs `Kadıoğlu` initial prefix).
   - `out` in sell candidates. `in` in grounded list for that sell.
   - Same FPL position. Different club. Price ≤ sell + ITB.
   - Not in recently-sold set.
   - If `free_transfers == 0`, passes `hit_breakeven_ok` (projected gain ≥ 4pts
     factoring ep delta + fixture swing).
   - Budget arithmetic and confidence computed deterministically in code, not
     by the LLM. Confidence derived from `buy_report.flags` count.

6. **Feedback loop**. Past suggestions are matched against `transfer_history`
   and their actual GW outcomes stored in `transfer_outcomes`. If recent
   win-rate < 40% or avg delta < −2, confidence thresholds tighten and the
   narrative gets a track-record caveat.

### Chip-aware behavior

- `wildcard` / `freehit` → up to 5 suggested moves
- `freehit` → prioritize next-GW ep_next only, ignore long-term fixture ticker
- `bboost` / `3xc` → standard rules, narrative emphasizes premium upside

## Twitter bot

`bot.py` runs via GitHub Actions on a cron schedule. State persists in
`bot_state` (SQLite) committed back to the repo. Dry-run mode activates
automatically when Twitter env vars are missing.

Tweet types:

| Tweet | Trigger |
|---|---|
| ⏰ Deadline incoming | Next deadline ≤ 12h away, once per GW |
| 🔥 DGW confirmed | Any team with 2+ fixtures in next event, once per GW |
| 🚫 BGW incoming | Team missing from next event; guarded by deadline ≤ 7 days AND ≥ 7 fixtures present (prevents false positives on pre-schedule data) |
| 🏥 Injury | Owned >5%, chance < 100%, status changed since last run. Severity icon: 🔴 ruled out / 🟠 doubtful / 🟡 knock |
| ✅ Return | Previously flagged player now at 100% |
| 👑 Kings of the Gameweek | Finished GW, top scorers pulled from `/event/{id}/live/` (authoritative — bootstrap's `event_points` resets between GWs), ties preserved up to 5 entries, 💎 flags sub-5% differentials |

All tweets go through `_fit_tweet()` which caps at 280 chars and truncates
trailing data lines gracefully.

## Running locally

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# .env
OPENAI_API_KEY=...
# for bot.py (optional — bot runs in dry-run without these):
TWITTER_CONSUMER_KEY=...
TWITTER_CONSUMER_SECRET=...
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_TOKEN_SECRET=...

uvicorn app.main:app --reload
python bot.py   # dry-run if Twitter creds missing
```

## Environment

- Default brief cache: 2 hours per (manager_id, GW)
- Daily LLM brief limit per manager: 5 (see `DAILY_BRIEF_LIMIT`)
- Bootstrap/fixtures in-memory cache: 5 minutes
