"""
Structured, per-run pipeline logging — every stage of the decision pipeline
(data fetch, ML predictions, each debate agent's full structured output,
backstop validation, execution, escalation, evaluation) gets a timestamped
started/succeeded/failed record in the `pipeline_log` table, grouped by a
single `run_id` per /internal/tick invocation (or per approval-triggered
execution). This exists specifically so a failure can be traced to the exact
stage and gameweek it happened in, instead of an unstructured print() in
Cloud Run's log stream.

Usage:
    run_id = new_run_id()
    with step(run_id, "ml_predict", gameweek=gw) as ctx:
        preds = compute_predictions(...)
        ctx["detail"] = {"predictions": preds}   # optional structured payload

On success, `detail` (if set) is persisted. On any exception, the stage is
logged as 'failed' with the error message + a truncated traceback, and the
exception is re-raised — this never swallows errors, it only makes them
easier to find afterward.
"""
from __future__ import annotations

import contextlib
import time
import traceback
import uuid

from . import database as db

_TRACEBACK_CHARS = 4000  # cap so one bad stack trace doesn't bloat the log row


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


@contextlib.contextmanager
def step(
    run_id: str,
    stage: str,
    *,
    gameweek: int | None = None,
    manager_id: int | None = None,
    decision_id: int | None = None,
):
    """
    Context manager wrapping one pipeline stage. Yields a dict the caller
    can set `ctx["detail"] = {...}` on before the block ends, to persist a
    structured payload alongside the 'succeeded' record.
    """
    start = time.monotonic()
    db.log_pipeline_step(run_id, stage, "started", gameweek=gameweek, manager_id=manager_id, decision_id=decision_id)
    ctx: dict = {}
    try:
        yield ctx
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        db.log_pipeline_step(
            run_id, stage, "failed",
            detail={"error": str(e), "traceback": traceback.format_exc()[-_TRACEBACK_CHARS:]},
            gameweek=gameweek, manager_id=manager_id, decision_id=decision_id,
            duration_ms=duration_ms,
        )
        raise
    else:
        duration_ms = int((time.monotonic() - start) * 1000)
        db.log_pipeline_step(
            run_id, stage, "succeeded",
            detail=ctx.get("detail"),
            gameweek=gameweek, manager_id=manager_id, decision_id=decision_id,
            duration_ms=duration_ms,
            tokens_in=ctx.get("tokens_in"), tokens_out=ctx.get("tokens_out"), cost_usd=ctx.get("cost_usd"),
        )
