"""
Entry point for the weekly autonomous decision pipeline. `run_weekly_pipeline()`
is invoked from app/main.py's `/internal/tick` webhook (hit by cron-job.org
every 30 minutes — see docs/architecture.md). Also runnable directly for
local testing:

    python -m app.agents.pipeline --manager-id <id> [--dry-run] [--force]

No-ops unless the next deadline is within DEBATE_WINDOW_HOURS, and is
idempotent against overlapping ticks (skips if a transfer decision already
exists for this gameweek) — `app/main.py`'s `threading.Lock` around
`_run_tick()` guards the other half of that race.

Runs ONCE per gameweek, deliberately close to the deadline (12h out, not
36h) — the closer to the deadline, the fresher the news/team-news signal
the debate is grounded on, which matters more than giving extra review time
beyond what the escalation cutoffs already provide (approval cutoff at
deadline-3h, last-chance failsafe at deadline-1h — see
app/agents/escalation_check.py).
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

from .. import database as db
from .. import llm as llm_module
from .. import observability
from .. import pricing
from .. import ranking
from ..notify import format_decision_summary, send_decision_for_approval
from ..fpl_client import (
    build_budget_info,
    build_player_lookup,
    build_squad_picks,
    build_team_form,
    build_team_lookup,
    build_team_strength_lookup,
    fetch_bootstrap,
    fetch_current_picks,
    fetch_fixtures,
    fetch_player_history_past,
    fetch_squad_recent_forms,
    fetch_transfer_history,
    find_valid_replacements,
    get_current_gameweek,  # only used for the picks fetch — see _next_gameweek_id's docstring
)
from ..ml import model as ml_model
from ..ml.features import build_live_inputs, opponent_strength
from .evaluate import build_calibration_context
from .graph import build_graph
from .state import DebateState

DEBATE_WINDOW_HOURS = 12  # only start debating once deadline is within this window —
# close enough that the news search/predictions are grounded on fresh information,
# with the escalation cutoffs (deadline-3h, deadline-1h) still providing real
# lead time to review/approve after this fires.


# ── ML predictions for the current squad ─────────────────────────────────────

def _predicted_points_for(players, team_form_lookup, strength_lookup, team_id_by_name, gw: int | None = None) -> dict[int, float]:
    """
    `gw` picks which fixture the opponent_strength feature describes; without
    it the player's next fixture is used. Previously this fed the model the
    player's OWN team strength under the name opponent_strength — see
    app/ml/features.opponent_strength, which both this and training now use.
    """
    inputs, ids = [], []
    for p in players:
        team_id = team_id_by_name.get(p.team_name)
        tf = team_form_lookup.get(team_id, {})
        fixture = next(
            (f for f in p.fixtures_next_3 if gw is None or f.event == gw),
            p.fixtures_next_3[0] if p.fixtures_next_3 else None,
        )
        opp_strength_val = (
            opponent_strength(strength_lookup.get(fixture.opp_id), fixture.venue) if fixture else 3.0
        )
        history_past = fetch_player_history_past(p.id)
        inputs.append(build_live_inputs(p, tf, opp_strength_val, history_past))
        ids.append(p.id)
    preds = ml_model.predict_points(inputs)
    return dict(zip(ids, preds))


# ── Transfer debate context ───────────────────────────────────────────────────

def _fetch_news_context(squad, grounded_targets: dict, gw: int, run_id: str) -> str:
    """
    ONE real web search per debate (not per player — the search model bills
    a flat ~$0.025/call tool fee, so batching all names into one call is
    what makes this affordable), covering the full squad plus every grounded
    buy target. This is the actual "coach said X in a press conference" /
    "not seen training" signal — FPL's own `news` field (already included in
    the sell/buy formatting above) is only ever a terse official blurb.
    """
    names = {p.player.web_name for p in squad}
    for targets in grounded_targets.values():
        names.update(t.web_name for t in targets)

    with observability.step(run_id, "pipeline.news_search", gameweek=gw) as ctx:
        text, usage = llm_module.fetch_player_context(sorted(names), enabled=True, return_usage=True)
        tokens_in, tokens_out = usage.get("tokens_in"), usage.get("tokens_out")
        cost = pricing.estimate_cost("gpt-4.1-mini", tokens_in or 0, tokens_out or 0) if tokens_in is not None else None
        ctx["tokens_in"], ctx["tokens_out"], ctx["cost_usd"] = tokens_in, tokens_out, cost
        ctx["detail"] = {"players_searched": len(names), "result": text, "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost}
        return text


def build_transfer_context(squad, sell_reports, grounded_targets, budget, gw, calibration_caveat: str, run_id: str) -> dict:
    sell_by_name = {r.player.web_name: r.player for r in sell_reports}
    squad_by_name_lc = {r.player.web_name.lower(): r.player for r in sell_reports}
    player_index = dict(squad_by_name_lc)
    for targets in grounded_targets.values():
        for t in targets:
            player_index[t.web_name.lower()] = t

    news_context = _fetch_news_context(squad, grounded_targets, gw, run_id)

    prompt_text = (
        f"GW{gw} TRANSFER DEBATE CONTEXT\n\n"
        f"BUDGET: ITB £{budget.itb}m | Free transfers: {budget.free_transfers} | "
        f"Transfers already made this GW: {budget.transfers_made}\n\n"
        f"SELL CANDIDATES (ranked by urgency):\n{llm_module._sell_candidates_str(sell_reports)}\n\n"
        f"{llm_module._format_grounded_targets(grounded_targets, sell_by_name, gw)}\n\n"
        + (f"LATEST NEWS (web search — press conferences, training reports, outlet coverage):\n{news_context}\n\n" if news_context else "")
        + "RULES: Only propose sells from SELL CANDIDATES. Only propose buys from that "
        "sell's VERIFIED TRANSFER TARGETS.\n"
        f"You have {budget.free_transfers} free transfer(s) this week, so the first "
        f"{budget.free_transfers} move(s) you propose cost NOTHING. Consider EVERY sell "
        "candidate that has a clearly better alternate in its target list, not just the most "
        "urgent one, and propose up to that many free moves. Free transfers do roll over, so "
        "an unused one is not wasted — but do not leave a clearly profitable upgrade unmade "
        "purely to bank it. Order your proposals best-first: anything past the free count "
        "costs 4 points, so only propose it if the gain clearly beats that. Do not set "
        "is_hit yourself — it is computed from the free transfer count.\n\n"
        f"CALIBRATION (recent agent track record):\n{calibration_caveat or 'No calibration history yet.'}"
    )
    return {
        "prompt_text": prompt_text,
        "sell_by_name": sell_by_name,
        "squad_by_name_lc": squad_by_name_lc,
        "player_index": player_index,
        "grounded_targets": grounded_targets,
        "budget_itb": budget.itb,
        "free_transfers": budget.free_transfers,
    }


def run_transfer_debate(
    context: dict, manager_id: int, gw: int, sell_reports, player_predictions: dict[int, float], run_id: str,
) -> dict:
    decision_id = db.create_agent_decision(manager_id, gw, "transfer", {}, "pending")
    graph = build_graph()
    initial_state: DebateState = {
        "run_id": run_id,
        "decision_id": decision_id,
        "gameweek": gw,
        "context": context,
        "messages": [],
        "round": 1,
        "requires_extra_scrutiny": False,
        "scrutiny_rounds_done": 0,
        "candidates": [],
        "proposal": None,
    }
    with observability.step(run_id, "debate.graph", gameweek=gw, decision_id=decision_id) as graph_ctx:
        final_state = graph.invoke(initial_state)
        graph_ctx["detail"] = {
            "rounds": final_state["round"],
            "scrutiny_rounds_done": final_state["scrutiny_rounds_done"],
            "raw_proposal": final_state.get("proposal"),
        }
    raw_decision = final_state.get("proposal") or {}

    # ── Deterministic backstop — the LLM never has unchecked authority ────────
    validated_transfers = []
    rejected = []
    with observability.step(run_id, "debate.backstop_validate", gameweek=gw, decision_id=decision_id) as backstop_ctx:
        if raw_decision.get("proceed"):
            for t in raw_decision.get("transfers", []):
                in_p = llm_module._validate_transfer(
                    {"out": t.get("out"), "in": t.get("in")},
                    context["player_index"],
                    context["squad_by_name_lc"],
                    context["sell_by_name"],
                    context["grounded_targets"],
                    context["budget_itb"],
                    set(),
                )
                if in_p is None:
                    rejected.append({"out": t.get("out"), "in": t.get("in"), "reason": "failed name/position/club/budget validation"})
                    continue
                out_p = llm_module._resolve_name(str(t.get("out", "")), context["squad_by_name_lc"])
                if out_p is None:
                    rejected.append({"out": t.get("out"), "in": t.get("in"), "reason": "sell player not resolved"})
                    continue

                # A transfer costs a hit once the free ones are spent. This used
                # to be gated on `free_transfers == 0`, so a third move on two
                # free transfers skipped the breakeven gate entirely — count the
                # moves actually accepted so far instead.
                if len(validated_transfers) >= context["free_transfers"]:
                    sell_report = next((r for r in sell_reports if r.player.id == out_p.id), None)
                    buy_report = ranking.score_buy_report(in_p, out_p, gw)
                    if sell_report and not ranking.hit_breakeven_ok(buy_report, sell_report):
                        db.log_agent_message(
                            decision_id, gw, final_state["round"], "backstop",
                            f"REJECTED {out_p.web_name}->{in_p.web_name}: hit not breakeven-profitable per deterministic check.",
                        )
                        rejected.append({"out": out_p.web_name, "in": in_p.web_name, "reason": "hit not breakeven-profitable"})
                        continue

                validated_transfers.append({
                    "out": out_p.web_name, "out_id": out_p.id,
                    "in": in_p.web_name, "in_id": in_p.id,
                    "rationale": t.get("rationale", ""),
                })
        backstop_ctx["detail"] = {"accepted": validated_transfers, "rejected": rejected}

    final_decision = {
        "proceed": bool(validated_transfers),
        "transfers": validated_transfers,
        "confidence": raw_decision.get("confidence", "Low"),
        "summary": raw_decision.get("summary", "No decision reached."),
    }
    db.update_decision_proposal(decision_id, final_decision, final_decision["confidence"])
    send_decision_for_approval(decision_id, format_decision_summary("transfer", gw, final_decision))

    # ── Persist every candidate considered, not just the winner ───────────────
    candidate_rows, seen_ids = [], set()
    for c in final_state.get("candidates", []):
        p = (
            llm_module._resolve_name(c["player_name"], context["player_index"])
            or llm_module._resolve_name(c["player_name"], context["squad_by_name_lc"])
        )
        if p is None or p.id in seen_ids:
            continue
        seen_ids.add(p.id)
        candidate_rows.append({
            "player_id": p.id, "player_name": p.web_name, "role": c["role"],
            "source_agent": c["source_agent"],
            "predicted_points": player_predictions.get(p.id, p.ep_next),
        })
    if candidate_rows:
        db.save_decision_candidates(decision_id, candidate_rows)

    stance_rows = [
        {"persona_name": m["agent"], "stance": m["stance"]}
        for m in final_state["messages"] if "stance" in m
    ]
    if stance_rows:
        db.save_persona_stances(decision_id, stance_rows)

    return {"decision_id": decision_id, **final_decision}


# ── Deterministic XI / captain / bench — no LLM debate, near-argmax decisions ─

def project_squad_after_transfers(squad, transfers: list[dict], grounded_targets: dict):
    """
    Applies the pending transfer proposal to the squad IN MEMORY, so the XI /
    captain / bench are chosen for the team you would actually field if you
    approve it.

    Without this the lineup was computed on the PRE-transfer squad while the
    transfer proposal was sent for approval seconds earlier — an approval batch
    that said "sell Tzolis" and "start Tzolis" thirteen seconds apart, and a
    picks payload naming a player who would no longer be in the squad once the
    transfer executed. The submit-time reconciliation in app/main.py is the
    backstop for when the transfer is then REJECTED and this projection turns
    out not to hold.

    Returns (projected_squad, incoming_players).
    """
    if not transfers:
        return list(squad), []

    incoming_by_id = {t.id: t for targets in grounded_targets.values() for t in targets}
    replacement_for, incoming = {}, []
    for t in transfers:
        new_player = incoming_by_id.get(t.get("in_id"))
        if new_player is None:
            continue  # can't project this one — leave the outgoing player in place
        replacement_for[t["out_id"]] = new_player
        incoming.append(new_player)

    projected = [
        pk.model_copy(update={"player": replacement_for[pk.player.id]})
        if pk.player.id in replacement_for else pk
        for pk in squad
    ]
    return projected, incoming


def run_lineup_selection(
    manager_id: int, gw: int, squad, player_predictions: dict[int, float], run_id: str,
    assumes_transfer_id: int | None = None,
) -> tuple[dict, dict]:
    """
    Picks the best starting XI from the full 15-man squad (formation-aware,
    see ranking.select_best_xi — this was skipped in the original build:
    only captain and bench ORDER were computed, never which 11 actually
    start), then captain/vice from that XI, then bench order for the rest.
    """
    all_players = [p.player for p in squad]

    # The XI is a ONE-WEEK decision, so weight predictions by this gameweek's
    # fixture rather than letting the model's (dead) 3-GW average decide it.
    with observability.step(run_id, "lineup.fixture_weighting", gameweek=gw, manager_id=manager_id) as ctx:
        weighted = ranking.apply_fixture_weighting(all_players, player_predictions, gw)
        ctx["detail"] = {
            "players": [
                {
                    "player": p.web_name,
                    "raw": player_predictions.get(p.id, p.ep_next),
                    "weight": ranking.gameweek_fixture_weight(p, gw),
                    "weighted": weighted[p.id],
                    "fixture": next(
                        (f"{f.opp}({f.venue}) fdr{f.fdr}/d{f.directional_fdr}"
                         for f in p.fixtures_next_3 if f.event == gw),
                        "BLANK",
                    ),
                }
                for p in all_players
            ]
        }

    with observability.step(run_id, "lineup.select_xi", gameweek=gw, manager_id=manager_id) as ctx:
        selection = ranking.select_best_xi(all_players, weighted)
        ctx["detail"] = {
            "formation": selection.formation,
            "starting": [p.web_name for p in selection.starting],
            "bench": [p.web_name for p in selection.bench],
            "starting_expected_points": selection.starting_expected_points,
        }

    with observability.step(run_id, "captain.score", gameweek=gw, manager_id=manager_id) as ctx:
        cap = ranking.score_captain(selection.starting, weighted)
        ctx["detail"] = {"captain": cap.player.web_name, "vice": cap.vice.web_name, "expected_points": cap.expected_points}

    with observability.step(run_id, "lineup.order_bench", gameweek=gw, manager_id=manager_id) as ctx:
        bench_order = ranking.order_bench(selection.bench, weighted)
        ctx["detail"] = {"bench_order": [{"player": b.player.web_name, "order": b.order} for b in bench_order]}

    cap_proposal = {
        "captain": cap.player.web_name, "captain_id": cap.player.id,
        "vice": cap.vice.web_name, "vice_id": cap.vice.id, "rationale": cap.rationale,
        "assumes_transfer_id": assumes_transfer_id,
    }
    cap_decision_id = db.create_agent_decision(manager_id, gw, "captain", cap_proposal, "High")
    db.log_agent_message(cap_decision_id, gw, 1, "deterministic", cap.rationale)
    send_decision_for_approval(cap_decision_id, format_decision_summary("captain", gw, cap_proposal))

    lineup_proposal = {
        "formation": selection.formation,
        "starting_xi": [{"player": p.web_name, "player_id": p.id} for p in selection.starting],
        "bench_order": [{"player": b.player.web_name, "player_id": b.player.id, "order": b.order} for b in bench_order],
        # Which transfer decision this XI assumes went through. app/main.py
        # blocks the picks submission until that decision is terminal, and
        # reconciles the XI against the live squad before submitting.
        "assumes_transfer_id": assumes_transfer_id,
    }
    lineup_decision_id = db.create_agent_decision(manager_id, gw, "lineup", lineup_proposal, "High")
    db.log_agent_message(
        lineup_decision_id, gw, 1, "deterministic",
        f"Formation {selection.formation} ({selection.starting_expected_points} pred pts). "
        "Bench order: " + ", ".join(f"{b.order}. {b.player.web_name}" for b in bench_order),
    )
    send_decision_for_approval(lineup_decision_id, format_decision_summary("lineup", gw, lineup_proposal))
    return (
        {"decision_id": cap_decision_id, "captain": cap.player.web_name, "vice": cap.vice.web_name},
        {"decision_id": lineup_decision_id, "formation": selection.formation,
         "starting_xi": [p.web_name for p in selection.starting], "bench_order": [b.player.web_name for b in bench_order]},
    )


# ── Orchestration ─────────────────────────────────────────────────────────────

def _hours_to_deadline(bootstrap: dict) -> float | None:
    for event in bootstrap["events"]:
        if event["is_next"]:
            deadline = datetime.datetime.strptime(
                event["deadline_time"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            return (deadline - now).total_seconds() / 3600.0
    return None


def _next_gameweek_id(bootstrap: dict) -> int | None:
    """
    The gameweek being DECIDED FOR — FPL's `is_next` flag (open deadline).
    Everything except the picks fetch itself should use this, not
    `get_current_gameweek()`'s `is_current` (the gameweek whose matches are
    already being played, deadline already passed). Using `is_current`
    throughout was a real bug caught during testing: it made the pipeline
    label decisions and — critically — would have submitted transfers
    against an already-locked gameweek, while the debate-window check
    (`_hours_to_deadline`, which correctly uses `is_next`) was gating
    against a DIFFERENT, later gameweek.
    """
    for event in bootstrap["events"]:
        if event["is_next"]:
            return event["id"]
    return None


def run_weekly_pipeline(manager_id: int, dry_run: bool = False, force: bool = False, run_id: str | None = None) -> dict:
    run_id = run_id or observability.new_run_id()
    bootstrap = fetch_bootstrap()

    # Two different gameweeks, deliberately: `gw_picks` is the only one FPL's
    # picks endpoint will actually answer for (verified — /event/{id}/picks/
    # 404s "Not found." for a gameweek that hasn't become current yet), so
    # it's used ONLY to fetch your existing squad. `gw` (is_next) is the
    # gameweek this decision is actually FOR — used for fixture lookahead,
    # budget/free-transfer math, decision labeling, and the transfer
    # submission's `event` field.
    gw_picks = get_current_gameweek(bootstrap)
    gw = _next_gameweek_id(bootstrap)
    if gw is None:
        db.log_pipeline_step(run_id, "pipeline.skip", "succeeded", detail={"reason": "no_upcoming_gameweek"}, manager_id=manager_id)
        return {"skipped": True, "reason": "no_upcoming_gameweek", "run_id": run_id}

    hours_left = _hours_to_deadline(bootstrap)
    if not force and (hours_left is None or hours_left > DEBATE_WINDOW_HOURS or hours_left < 0):
        db.log_pipeline_step(run_id, "pipeline.skip", "succeeded", detail={"reason": "outside_debate_window", "hours_left": hours_left}, gameweek=gw, manager_id=manager_id)
        return {"skipped": True, "reason": "outside_debate_window", "hours_left": hours_left, "run_id": run_id}

    if not force and db.get_decisions(manager_id, gw, "transfer"):
        db.log_pipeline_step(run_id, "pipeline.skip", "succeeded", detail={"reason": "already_run_this_gw"}, gameweek=gw, manager_id=manager_id)
        return {"skipped": True, "reason": "already_run_this_gw", "gameweek": gw, "run_id": run_id}

    with observability.step(run_id, "pipeline.fetch_data", gameweek=gw, manager_id=manager_id) as ctx:
        fixtures = fetch_fixtures()
        player_lookup = build_player_lookup(bootstrap)
        team_lookup = build_team_lookup(bootstrap)
        team_name_lookup = {t["id"]: t["name"] for t in bootstrap["teams"]}
        team_id_by_name = {t["name"]: t["id"] for t in bootstrap["teams"]}
        strength_lookup = build_team_strength_lookup(bootstrap)
        team_form_lookup = build_team_form(fixtures, gw)

        raw_picks, entry_history, active_chip = fetch_current_picks(manager_id, gw_picks)
        transfer_history = fetch_transfer_history(manager_id)
        player_ids = [p["element"] for p in raw_picks]
        recent_forms = fetch_squad_recent_forms(player_ids)
        # `gw` (not gw_picks) so fixture lookahead ("next 3 fixtures") starts
        # from the gameweek this decision is FOR, not the already-locked one.
        squad = build_squad_picks(
            raw_picks, player_lookup, team_lookup, gw, fixtures, bootstrap, recent_forms, strength_lookup
        )
        budget = build_budget_info(entry_history, transfer_history, gw)
        ctx["detail"] = {
            "squad_size": len(squad), "active_chip": active_chip,
            "itb": budget.itb, "free_transfers": budget.free_transfers,
            "gw_picks": gw_picks, "gw_target": gw,
        }

    with observability.step(run_id, "pipeline.ml_predict", gameweek=gw, manager_id=manager_id) as ctx:
        player_predictions = _predicted_points_for(
            [p.player for p in squad], team_form_lookup, strength_lookup, team_id_by_name, gw
        )
        name_by_id = {p.player.id: p.player.web_name for p in squad}
        ctx["detail"] = {
            "model_used": "model.pkl" if ml_model.model_is_available() else "ep_next_fallback",
            "predictions": [
                {"player_id": pid, "player_name": name_by_id.get(pid, "?"), "predicted_points": pts}
                for pid, pts in player_predictions.items()
            ],
        }

    with observability.step(run_id, "pipeline.sell_reports", gameweek=gw, manager_id=manager_id) as ctx:
        # Score the whole 15, not just the XI — bench dead weight (0 minutes,
        # price bleeding) was previously unsellable by construction. But a
        # failing STARTER costs points every single week while a bad bench
        # player only costs squad value, so the XI is guaranteed 5 of the 8
        # slots: a 0-minute 4th sub can't crowd out the real problems.
        #
        # The bench keeper is scored too, with his not-playing penalties
        # suppressed (see ranking.score_sell's is_backup_gk) — excluding him
        # outright meant the keeper slot could never be fixed at all.
        by_score = lambda r: r.score

        def _score(pk):
            is_backup_gk = pk.position > 11 and pk.player.position == "GKP"
            return ranking.score_sell(pk.player, gw, is_backup_gk=is_backup_gk)

        xi_reports = sorted((_score(pk) for pk in squad if pk.position <= 11), key=by_score, reverse=True)
        bench_reports = sorted((_score(pk) for pk in squad if pk.position > 11), key=by_score, reverse=True)
        sell_reports = xi_reports[:5] + sorted(xi_reports[5:] + bench_reports, key=by_score, reverse=True)[:3]
        bench_ids = {pk.player.id for pk in squad if pk.position > 11}
        ctx["detail"] = {
            "top_sell_candidates": [
                {
                    "player": r.player.web_name, "urgency_score": r.score, "flags": r.flags,
                    "on_bench": r.player.id in bench_ids,
                }
                for r in sell_reports
            ]
        }

    with observability.step(run_id, "pipeline.grounded_targets", gameweek=gw, manager_id=manager_id) as ctx:
        recent_sold_ids = ranking.recently_sold_ids(transfer_history, gw, lookback=3)
        grounded_targets: dict[str, list] = {}
        for report in sell_reports:
            sell_p = report.player
            replacements = find_valid_replacements(
                sell_player=sell_p, budget_max=round(sell_p.now_cost + budget.itb, 1),
                squad=squad, player_lookup=player_lookup, team_lookup=team_lookup,
                team_name_lookup=team_name_lookup, current_gw=gw, fixtures=fixtures,
                strength_lookup=strength_lookup, recently_sold_ids=recent_sold_ids,
            )
            if replacements:
                grounded_targets[sell_p.web_name] = replacements
        ctx["detail"] = {sell_name: [t.web_name for t in targets] for sell_name, targets in grounded_targets.items()}

    calibration_caveat = build_calibration_context()
    context = build_transfer_context(squad, sell_reports, grounded_targets, budget, gw, calibration_caveat, run_id)

    if dry_run:
        return {"dry_run": True, "gameweek": gw, "run_id": run_id, "context_preview": context["prompt_text"][:2000]}

    transfer_decision = run_transfer_debate(context, manager_id, gw, sell_reports, player_predictions, run_id)

    # Pick the XI for the squad the transfer proposal would LEAVE you with, not
    # the one you currently hold — see project_squad_after_transfers.
    with observability.step(run_id, "lineup.project_squad", gameweek=gw, manager_id=manager_id) as ctx:
        projected_squad, incoming = project_squad_after_transfers(
            squad, transfer_decision.get("transfers", []), grounded_targets
        )
        if incoming:
            player_predictions.update(
                _predicted_points_for(incoming, team_form_lookup, strength_lookup, team_id_by_name, gw)
            )
        ctx["detail"] = {
            "assumes_transfer_id": transfer_decision["decision_id"],
            "incoming": [p.web_name for p in incoming],
            "projected_squad": [pk.player.web_name for pk in projected_squad],
        }

    captain_decision, lineup_decision = run_lineup_selection(
        manager_id, gw, projected_squad, player_predictions, run_id,
        assumes_transfer_id=transfer_decision["decision_id"],
    )

    return {
        "gameweek": gw,
        "run_id": run_id,
        "transfer": transfer_decision,
        "captain": captain_decision,
        "lineup": lineup_decision,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager-id", type=int, default=int(os.environ.get("FPL_MANAGER_ID", 0) or 0))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="ignore the deadline window / idempotency check")
    args = parser.parse_args()

    if not args.manager_id:
        print("No manager id provided (--manager-id or FPL_MANAGER_ID env var)", file=sys.stderr)
        sys.exit(1)

    db.init_db()
    try:
        result = run_weekly_pipeline(args.manager_id, dry_run=args.dry_run, force=args.force)
        print(result)
    finally:
        db.close()  # ClientSync's background thread otherwise hangs the process


if __name__ == "__main__":
    main()
