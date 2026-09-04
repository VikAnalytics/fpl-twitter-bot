"""
Cron-driven escalation checks — no FPL credentials involved (execution itself
lives behind the FastAPI approval route, deployed separately), safe to run
alongside the debate pipeline's broad dependency install.

    python -m app.agents.escalation_check --manager-id <id>

Fires a Telegram alert (app/notify.py) when:
- a decision is still unapproved with <= APPROVAL_CUTOFF_HOURS left, or
- an approved transfer is still not executed with <= FAILSAFE_HOURS left
  (the last-chance alert, in case the first one was missed).
"""
from __future__ import annotations

import argparse
import os
import sys

from .. import database as db
from .. import observability
from ..fpl_client import fetch_bootstrap
from ..notify import escalate_failsafe, escalate_unapproved_decision
from .pipeline import _hours_to_deadline

APPROVAL_CUTOFF_HOURS = 3
FAILSAFE_HOURS = 1


def check(manager_id: int, run_id: str | None = None) -> dict:
    run_id = run_id or observability.new_run_id()
    with observability.step(run_id, "escalation.check", manager_id=manager_id) as ctx:
        bootstrap = fetch_bootstrap()
        hours_left = _hours_to_deadline(bootstrap)
        if hours_left is None or hours_left < 0:
            result = {"skipped": True, "reason": "no upcoming deadline"}
            ctx["detail"] = result
            return result

        alerts = []

        if hours_left <= APPROVAL_CUTOFF_HOURS:
            for d in db.get_pending_decisions(manager_id):
                escalate_unapproved_decision(d["gameweek"], hours_left, d["id"], manager_id)
                alerts.append(("unapproved", d["id"]))

        if hours_left <= FAILSAFE_HOURS:
            # Any approved-but-unexecuted decision, not just transfers: captain
            # and lineup now park behind the gameweek's transfer decision, so an
            # unresolved transfer can leave approved picks unsubmitted too.
            approved_not_executed = [
                d for d in db.get_decisions(manager_id) if d["status"] == "approved"
            ]
            for d in approved_not_executed:
                escalate_failsafe(d["gameweek"], d["id"])
                alerts.append(("failsafe", d["id"]))

        result = {"hours_left": hours_left, "alerts": alerts, "run_id": run_id}
        ctx["detail"] = result
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager-id", type=int, default=int(os.environ.get("FPL_MANAGER_ID", 0) or 0))
    args = parser.parse_args()
    if not args.manager_id:
        print("No manager id provided (--manager-id or FPL_MANAGER_ID env var)", file=sys.stderr)
        sys.exit(1)
    db.init_db()
    try:
        print(check(args.manager_id))
    finally:
        db.close()  # ClientSync's background thread otherwise hangs the process


if __name__ == "__main__":
    main()
