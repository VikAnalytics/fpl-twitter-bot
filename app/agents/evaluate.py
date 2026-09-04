"""
Feedback & verification loop: after a gameweek finishes, score every candidate
the transfer debate considered (not just the winner — the chosen buy, the
chosen sell, and any rejected alternates) against actual points, and mark
each debate persona's stance as vindicated or not. Extends the single-agent
past_outcome_adjustment() pattern in app/ranking.py into a full counterfactual
+ per-persona calibration system.
"""
from __future__ import annotations

from .. import database as db
from .. import observability
from ..fpl_client import fetch_bootstrap, fetch_player_gw_points

_DEBATE_PERSONAS = ("fixture_form", "news_injury", "risk_scrutiny", "rebuttal")


def evaluate_gameweek(finished_gw: int, run_id: str | None = None) -> dict:
    run_id = run_id or observability.new_run_id()
    with observability.step(run_id, "evaluate.score_candidates", gameweek=finished_gw) as ctx:
        candidates = db.get_unevaluated_candidates(before_gw=finished_gw + 1)
        candidates = [c for c in candidates if c["gameweek"] == finished_gw]

        touched_decisions: set[int] = set()
        scored = []
        for c in candidates:
            pts = fetch_player_gw_points(c["player_id"], finished_gw)
            if pts is not None:
                db.save_candidate_outcome(c["id"], float(pts))
                touched_decisions.add(c["decision_id"])
                scored.append({"player_name": c["player_name"], "role": c["role"], "predicted": c["predicted_points"], "actual": pts})
        ctx["detail"] = {"scored": scored}

    evaluated = 0
    with observability.step(run_id, "evaluate.persona_calibration", gameweek=finished_gw) as ctx:
        calibration_detail = []
        for decision_id in touched_decisions:
            rows = db.get_candidates_for_decision(decision_id)
            chosen_in = next((r for r in rows if r["role"] == "chosen_in" and r["actual_points"] is not None), None)
            chosen_out = next((r for r in rows if r["role"] == "chosen_out" and r["actual_points"] is not None), None)
            alternates = [r for r in rows if r["role"] == "alternate" and r["actual_points"] is not None]

            if chosen_in is None or chosen_out is None:
                continue

            best_alt = max((a["actual_points"] for a in alternates), default=None)
            transfer_correct = chosen_in["actual_points"] > chosen_out["actual_points"]
            if best_alt is not None:
                transfer_correct = transfer_correct and chosen_in["actual_points"] >= best_alt

            stances = db.get_stances_for_decision(decision_id)
            outcomes = {}
            for s in stances:
                favored = s["stance"] == "favored"
                outcomes[s["persona_name"]] = (favored and transfer_correct) or (not favored and not transfer_correct)
            if outcomes:
                db.mark_persona_outcomes(decision_id, outcomes)
            evaluated += 1
            calibration_detail.append({"decision_id": decision_id, "transfer_correct": transfer_correct, "persona_outcomes": outcomes})
        ctx["detail"] = {"decisions": calibration_detail}

    return {"finished_gw": finished_gw, "candidates_scored": len(candidates), "decisions_evaluated": evaluated, "run_id": run_id}


def build_calibration_context(limit: int = 20) -> str:
    """
    Rolling per-persona accuracy, formatted as a caveat to inject into next
    week's debate context — e.g. "risk_scrutiny correctly opposed 7/10 recent
    calls, weight its objections accordingly."
    """
    lines = []
    for persona in _DEBATE_PERSONAS:
        acc = db.get_persona_accuracy(persona, limit=limit)
        if acc["sample_size"] == 0:
            continue
        lines.append(
            f"- {persona}: correct on {acc['accuracy']*100:.0f}% of its last {acc['sample_size']} "
            f"stances — weight its argument accordingly."
        )
    return "\n".join(lines)


def _last_finished_gw(bootstrap: dict) -> int | None:
    for event in bootstrap["events"]:
        if event.get("is_previous") and event.get("finished"):
            return event["id"]
    return None


def run_evaluate(run_id: str | None = None) -> dict:
    """
    Pure entry point — safe to call from a long-lived process (the FastAPI
    webhook handler). Does NOT touch db.init_db()/db.close(); the caller
    owns that lifecycle.
    """
    bootstrap = fetch_bootstrap()
    gw = _last_finished_gw(bootstrap)
    if gw is None:
        return {"skipped": True, "reason": "no finished gameweek to evaluate"}
    return evaluate_gameweek(gw, run_id=run_id)


def main():
    """CLI entry point (`python -m app.agents.evaluate`) — owns the DB lifecycle."""
    db.init_db()
    try:
        print(run_evaluate())
    finally:
        db.close()  # ClientSync's background thread otherwise hangs the process


if __name__ == "__main__":
    main()
