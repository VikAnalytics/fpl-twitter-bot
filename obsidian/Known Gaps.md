---
tags: [risk]
---

# Known Gaps

Honest record of what's built but not yet trustworthy. See [[../docs/progress.md]] for the full checklist.

## ~~Split-brain SQLite state~~ — RESOLVED
Was: `data/fpl_intel.db` written independently by GitHub Actions (commits to git) and the Railway-hosted FastAPI app (local copy, never synced back), so approvals could go unseen by the next cron run.

Resolved as a side effect of the Cloud Run + Turso + cron-job.org migration (see [[Hosting and Scheduling]]): the decision pipeline no longer runs as a separate GitHub Actions job at all — it runs inside the same long-lived process that serves the approval routes, backed by one Turso database. No second copy exists to go stale.

## ~~Turso migration untested against a real database~~ — RESOLVED
Verified end-to-end (init, insert, read, update, close) against the real, deployed Turso database — not just the local libSQL file fallback. One real finding along the way: the `libsql://` (WebSocket/Hrana) connection scheme failed its handshake (`400`); the `https://` scheme against the same database worked immediately and is arguably the better choice anyway for a request/response server pattern rather than a persistent WebSocket. `TURSO_DATABASE_URL` must use `https://`.

Test rows were written under a fake manager id during verification and cleaned up afterward via a small one-off script — the real database wasn't left with test junk in it.

## Docker image unbuilt
No Docker daemon was available in the implementation session — the `Dockerfile` (pip install + copy + uvicorn, about as plain as it gets) has not actually been built or run. Build and smoke-test before deploying to Cloud Run.

## Cloud Run / cron-job.org not yet set up
Neither has been provisioned. See "Deployment Setup" in [[../docs/progress.md]] for the exact steps (Turso db create, `gcloud run deploy` with the full env var list, cron-job.org job pointing at `/internal/tick`).

## model.pkl goes stale on Cloud Run without a redeploy
Cloud Run bakes `model.pkl` into the image at build time. `train_model.yml` still commits a fresh model weekly, but nothing currently triggers a Cloud Run redeploy afterward — predictions will keep using an older model until someone manually redeploys. Automating this (Cloud Build trigger, or a follow-up `gcloud run deploy` step in the training workflow) is unbuilt.

## Never *successfully* tested against a real FPL account (but one real login attempt DID happen)
`app/fpl_auth.py`'s login flow is written against the community-documented unofficial endpoints. During Telegram webhook testing, a test script approved a transfer decision without mocking `fpl_login()` — the process hung consistent with a real login POST to `users.premierleague.com` with real credentials from `.env` before being killed. No transfer was submitted (never got past login), and this was flagged transparently to the user rather than glossed over. All subsequent tests explicitly mock every FPL network call. A genuine dry-run against a staging/test account, verified end-to-end through to submission, still hasn't happened — that's the real remaining gap.

## ~~No model trained yet~~ — RESOLVED
`app/ml/model.pkl` now exists — trained on real 2025-26 data, holdout MAE 0.641 points across 4,464 held-out rows (25,293 training rows total). See [[ML Model]] for the scoring-rules fix that shaped this. Note this was trained locally during the implementation session, not yet via `train_model.yml` in CI — the workflow should still be exercised at least once to confirm it reproduces the same result in that environment.

## ~~Captain/lineup execution not wired~~ — RESOLVED
`_execute_decision()` now handles `captain`/`lineup` decisions, not just `transfer` — see [[Captain and Lineup]] for how the pairing (wait for both, submit once) works. Verified with mocked FPL calls, not yet against a real account (see the gap above).
