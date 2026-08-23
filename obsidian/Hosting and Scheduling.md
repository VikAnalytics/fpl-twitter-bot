---
tags: [component]
---

# Hosting and Scheduling

How the system is deployed and triggered — this shifted significantly from the original plan after real-world constraints surfaced during implementation.

## Current design
- **App**: Google Cloud Run, deployed from `Dockerfile`. Long-lived within a request burst, scales to zero between cron ticks.
- **Trigger**: cron-job.org (external, free) POSTs to `/internal/tick` every 30 minutes, authenticated via `X-Cron-Secret` header.
- **DB**: Turso (libSQL) — see [[Known Gaps]] for the migration details.
- **Model training**: stays on GitHub Actions (`train_model.yml`), unaffected by the scheduling concerns below since it's not deadline-critical.

## Why not GitHub Actions `schedule:` for the decision pipeline
The original plan used a GitHub Actions cron (`*/30 * * * *`) for the whole pipeline. User asked to swap it for cron-job.org — GitHub's own documentation acknowledges scheduled workflows can be delayed or dropped under platform load, which is a direct conflict with the "never acceptable to miss a deadline" requirement from [[Reliability and Escalation]]. An external cron hitting an always-reachable HTTP endpoint doesn't have that failure mode.

## Why not Railway
The plan originally assumed Railway (free tier) for hosting, per [[../docs/plan.md]]'s frozen context section (`app/database.py`'s SQLite file lived there). Mid-implementation, the user flagged they might not have Railway's free tier anymore (Railway removed it, now $5/mo minimum). Asked for alternatives, including "our own VPC."

## Why Google Cloud Run over other options
Considered: Oracle Cloud Always Free (a real persistent VM — closest to "our own VPC"), an existing VPC/VM the user might already have, and Cloud Run. User picked Cloud Run — official GCP product, genuinely free-forever tier at this project's traffic (~1,440 webhook calls/month vs. 2M free), no server to patch/maintain.

## The catch: Cloud Run's ephemeral filesystem
Cloud Run containers don't persist local disk across scale-to-zero — the original SQLite-file approach would silently lose all decision state between cron ticks. This forced the DB question that had been deferred: migrate to a real network DB. Two options were weighed (Turso migration vs. GCS FUSE volume mount); user chose Turso, which also happened to permanently resolve the split-brain state gap that had been flagged as an open risk since the very first draft of this feature. See [[Known Gaps]] and [[Decisions Log]] for the full reasoning trail.

## Related
[[Reliability and Escalation]] · [[Known Gaps]] · [[Decisions Log]]
