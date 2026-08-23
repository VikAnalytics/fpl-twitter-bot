---
tags: [component]
---

# Reliability and Escalation

The user was explicit: a missed transfer window is "never acceptable," and any failure needs escalation at least 1 hour before deadline. This shaped the whole timing model.

## Design
- Debate runs once fixtures/news are stable (~36h+ before deadline)
- Approval cutoff at deadline−3h — comfortably clears the ≥1h requirement even after retries
- `app/notify.py` sends a Telegram message (not just a tweet) via Telegram's official Bot API — a tweet isn't guaranteed to be seen in time
- `app/agents/escalation_check.py` fires alerts for (a) unapproved decisions past the cutoff, (b) a final failsafe check at deadline−1h if an approved decision still isn't executed
- `app/fpl_auth.py` retries login/submit with backoff before declaring failure
- `concurrency: { group: weekly-decision }` on the cron workflow prevents overlapping runs from racing (a debate taking >30 min could otherwise double-trigger)

## Why Telegram specifically (after two reversals)
Asked the user directly which channel; "tweet + badge only" was rejected as unreliable given the "never acceptable" requirement. The channel went through three iterations:
1. **WhatsApp via CallMeBot** — user's first choice, but later rejected for using an unofficial/reverse-engineered API.
2. **X/Twitter DM** — user's next request, but X's DM endpoints require a paid API tier ($200/mo+ Basic); user confirmed they don't have paid access, ruling this out too.
3. **Telegram Bot API** — landed here: official first-party API, genuinely free, no reverse-engineering, no paid tier. Matches both the "official API only" constraint and the original "utilize all free resources" instruction.

Full trail in [[Decisions Log]].

## Approval moved into Telegram itself
Originally the plan was: alerts via Telegram, approval via a web page. User asked directly to do approval in the Telegram chat too. Every decision now ships with inline Approve/Reject buttons (`app/notify.py`'s `send_decision_for_approval()`); `POST /telegram/webhook` handles the tap, verified via a secret header (same pattern as `CRON_SECRET`). The web page (`/decisions/{manager_id}`) still works as a secondary surface — not removed, just no longer the only way in.

## Related
[[Known Gaps]] · [[Captain and Lineup]] (execution now covers captain/lineup too, not just transfers)
