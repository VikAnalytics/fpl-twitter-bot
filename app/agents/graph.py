"""
LangGraph state machine for the transfer debate:

  analyst -> fixture_form -> news_injury -> risk_scrutiny
                                                 |
                          (extra round if hit/high-risk, capped at 1 extra)
                                                 v
                                            rebuttal -> moderator -> END

The `rebuttal` node exists because risk_scrutiny used to speak LAST, and the
moderator was told to refuse any proposal whose objections "were not clearly
answered" — with no node after risk_scrutiny, no objection could ever be
answered, so `proceed=false` was structurally the default. GW3 lost a free
Maguire -> De Cuyper transfer 2-1 in favor that way. The analyst now gets a
right of reply, and the moderator weighs the exchange instead of deferring to
whoever spoke last (see MODERATOR_SYSTEM in personas.py).

Every node's message is logged to agent_conversations immediately (crash-safe,
human-readable transcript), AND every node's full structured LLM output
(the raw Pydantic object — including fields never shown in the prose message,
like requires_extra_scrutiny or alternates_considered), token usage, and
estimated cost, plus call duration, is logged to pipeline_log via
app/observability.py — this is the "what exactly did each agent's reasoning
produce, and what did it cost" trail, not just the summarized text. If the
OpenAI call itself fails or hangs, that's captured as a 'failed' step with a
traceback, not silently lost.

The LLM's decision is NOT trusted as final — app/agents/pipeline.py runs the
same deterministic budget/position/club/hit-breakeven backstop that
app/llm.py already applies to the single-agent brief, after this graph completes.
"""
from __future__ import annotations

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from .. import database as db
from .. import observability
from .. import pricing
from .personas import (
    ANALYST_SYSTEM,
    FIXTURE_FORM_SYSTEM,
    MODERATOR_SYSTEM,
    NEWS_INJURY_SYSTEM,
    REBUTTAL_SYSTEM,
    RISK_SCRUTINY_SYSTEM,
    AgentTurn,
    AnalystOutput,
    ModeratorDecision,
    RiskScrutinyOutput,
)
from .state import DebateState

_MODEL_NAME = "gpt-4o-mini"


def _llm(schema, temperature: float = 0.2):
    model = ChatOpenAI(model=_MODEL_NAME, temperature=temperature)
    # include_raw=True so token usage is available — with_structured_output()
    # alone only returns the parsed object, discarding the raw AIMessage's
    # usage_metadata that cost tracking needs.
    return model.with_structured_output(schema, include_raw=True)


def _fixture_for(player, gw: int) -> str:
    for f in player.fixtures_next_3:
        if f.event == gw:
            fdr = f.directional_fdr if f.directional_fdr is not None else float(f.fdr)
            return f"{f.opp}({f.venue}) FDR {fdr}"
    return "BLANK (no fixture)"


def _proposal_block(state: DebateState) -> str:
    """
    The proposal plus a precomputed FACTS table.

    gpt-4o-mini cannot be trusted to compare two numbers in prose: on a live
    run the moderator wrote "his expected points (ep_next 3.0) are higher than
    Groß's (ep_next 7.5)" and dropped two good free transfers on the strength
    of it. Every comparison the debate needs is arithmetic we already have, so
    compute the deltas here and tell the agents to use them rather than doing
    mental maths on the context dump.
    """
    proposal = state["proposal"] or {}
    transfers = proposal.get("transfers", [])
    if not transfers:
        return f"PROPOSAL UNDER DEBATE:\n{proposal}"

    index = state["context"]["player_index"]
    gw = state["gameweek"]
    lines = ["PROPOSAL UNDER DEBATE:"]
    for t in transfers:
        out_p = index.get(str(t.get("out", "")).lower())
        in_p = index.get(str(t.get("in", "")).lower())
        cost = "POINT HIT (-4)" if t.get("is_hit") else "FREE"
        lines.append(f"\n  {t.get('out')} -> {t.get('in')}  [{cost}]")
        lines.append(f"    rationale: {t.get('rationale', '')}")
        if not out_p or not in_p:
            continue

        def _avg(p):
            return sum(p.recent_form_5gw) / len(p.recent_form_5gw) if p.recent_form_5gw else 0.0

        lines.append(
            "    FACTS (authoritative — use these numbers, do not restate your own):\n"
            f"      ep_next:      OUT {out_p.ep_next} vs IN {in_p.ep_next}  "
            f"(delta {in_p.ep_next - out_p.ep_next:+.2f} in favour of {'IN' if in_p.ep_next > out_p.ep_next else 'OUT'})\n"
            f"      pts/GW so far: OUT {_avg(out_p):.2f} vs IN {_avg(in_p):.2f}  (delta {_avg(in_p) - _avg(out_p):+.2f})\n"
            f"      xGI/90:       OUT {out_p.xgi_per_90:.2f} vs IN {in_p.xgi_per_90:.2f}  "
            f"(delta {in_p.xgi_per_90 - out_p.xgi_per_90:+.2f})\n"
            f"      starts%:      OUT {out_p.starts_pct:.0f}% vs IN {in_p.starts_pct:.0f}%\n"
            f"      GW{gw} fixture: OUT {_fixture_for(out_p, gw)} vs IN {_fixture_for(in_p, gw)}\n"
            f"      price:        OUT £{out_p.now_cost}m vs IN £{in_p.now_cost}m"
        )
    return "\n".join(lines)


def _log(state: DebateState, agent_name: str, message: str) -> None:
    db.log_agent_message(state["decision_id"], state["gameweek"], state["round"], agent_name, message)


def _context_str(state: DebateState) -> str:
    return state["context"]["prompt_text"]


def _invoke_logged(state: DebateState, agent_name: str, schema, messages, temperature: float = 0.2):
    """
    Runs the LLM call inside an observability.step() so duration, token
    usage, estimated cost, and the full structured output (or the exact
    failure) land in pipeline_log, tagged to this decision/gameweek/run.
    """
    with observability.step(
        state["run_id"], f"debate.{agent_name}",
        gameweek=state["gameweek"], decision_id=state["decision_id"],
    ) as ctx:
        raw_result = _llm(schema, temperature).invoke(messages)
        result = raw_result["parsed"]
        if result is None:
            raise ValueError(f"structured output parsing failed: {raw_result.get('parsing_error')}")

        usage = getattr(raw_result["raw"], "usage_metadata", None) or {}
        tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
        cost = pricing.estimate_cost(_MODEL_NAME, tokens_in or 0, tokens_out or 0) if tokens_in is not None else None

        ctx["tokens_in"], ctx["tokens_out"], ctx["cost_usd"] = tokens_in, tokens_out, cost
        ctx["detail"] = {
            "round": state["round"], "output": result.model_dump(by_alias=True),
            "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost,
        }
        return result


def analyst_node(state: DebateState) -> dict:
    result: AnalystOutput = _invoke_logged(
        state, "analyst", AnalystOutput,
        [("system", ANALYST_SYSTEM), ("user", _context_str(state))],
    )
    # Whether a move costs a hit is arithmetic — transfers beyond the free
    # count — not something to ask the LLM to judge. Left to the model it got
    # this wrong in the obvious direction: with 2 free transfers it stamped
    # is_hit=true on BOTH proposals, which flipped risk_scrutiny into its
    # strict hit bar and killed two free upgrades. Stamp it deterministically
    # so every downstream agent argues about the real cost.
    free = state["context"].get("free_transfers", 0)
    for i, t in enumerate(result.transfers):
        t.is_hit = i >= free

    if result.transfers:
        msg = "; ".join(
            f"{t.out} -> {t.in_}{' (HIT)' if t.is_hit else ' (free)'}: {t.rationale}"
            for t in result.transfers
        )
    else:
        msg = "No transfer recommended this week."
    _log(state, "analyst", msg)

    candidates = []
    for t in result.transfers:
        candidates.append({"player_name": t.out, "role": "chosen_out", "source_agent": "analyst"})
        candidates.append({"player_name": t.in_, "role": "chosen_in", "source_agent": "analyst"})
    for alt in result.alternates_considered:
        candidates.append({"player_name": alt, "role": "alternate", "source_agent": "analyst"})

    proposal = {"transfers": [t.model_dump(by_alias=True) for t in result.transfers]}
    return {
        "messages": [{"agent": "analyst", "message": msg}],
        "proposal": proposal,
        "candidates": candidates,
    }


def _debate_turn(state: DebateState, agent_name: str, system: str) -> dict:
    result: AgentTurn = _invoke_logged(
        state, agent_name, AgentTurn,
        [("system", system), ("user", _context_str(state) + "\n\n" + _proposal_block(state))],
    )
    _log(state, agent_name, result.message)
    return {"messages": [{"agent": agent_name, "stance": result.stance, "message": result.message}]}


def fixture_form_node(state: DebateState) -> dict:
    return _debate_turn(state, "fixture_form", FIXTURE_FORM_SYSTEM)


def news_injury_node(state: DebateState) -> dict:
    return _debate_turn(state, "news_injury", NEWS_INJURY_SYSTEM)


def risk_scrutiny_node(state: DebateState) -> dict:
    round_note = (
        ""
        if state["scrutiny_rounds_done"] == 0
        else "\n\nESCALATED SECOND ROUND — this proposal takes a point hit or is high-risk. "
        "Hold it to a stricter bar than the first pass."
    )
    result: RiskScrutinyOutput = _invoke_logged(
        state, "risk_scrutiny", RiskScrutinyOutput,
        [
            ("system", RISK_SCRUTINY_SYSTEM),
            ("user", _context_str(state) + "\n\n" + _proposal_block(state) + round_note),
        ],
    )
    _log(state, "risk_scrutiny", result.message)
    return {
        "messages": [{"agent": "risk_scrutiny", "stance": result.stance, "message": result.message}],
        "requires_extra_scrutiny": result.requires_extra_scrutiny,
        "scrutiny_rounds_done": state["scrutiny_rounds_done"] + 1,
        "round": state["round"] + 1,
    }


def rebuttal_node(state: DebateState) -> dict:
    """
    The proposer's right of reply. Without this the moderator's "objections not
    clearly answered -> proceed=false" rule was unfalsifiable, since nothing
    ever followed risk_scrutiny.
    """
    objections = "\n".join(
        f"[{m['agent']}]: {m['message']}"
        for m in state["messages"] if m.get("stance") == "opposed"
    ) or "No agent opposed the proposal."
    result: AgentTurn = _invoke_logged(
        state, "rebuttal", AgentTurn,
        [
            ("system", REBUTTAL_SYSTEM),
            ("user", _context_str(state) + "\n\n" + _proposal_block(state)
                     + f"\n\nOBJECTIONS RAISED:\n{objections}"),
        ],
    )
    _log(state, "rebuttal", result.message)
    return {"messages": [{"agent": "rebuttal", "stance": result.stance, "message": result.message}]}


def _cost_note(state: DebateState) -> str:
    """
    Tells the moderator which bar to apply. A hit is asymmetric and deserves
    the strict line; a free transfer is bounded downside and shouldn't be held
    to "proven beyond doubt".
    """
    transfers = (state["proposal"] or {}).get("transfers", [])
    if state["requires_extra_scrutiny"] or any(t.get("is_hit") for t in transfers):
        return (
            "\n\nPROPOSAL COST: takes a POINT HIT or was flagged high-risk — apply the "
            "stricter bar; unanswered objections mean proceed=false."
        )
    return (
        f"\n\nPROPOSAL COST: free transfer ({state['context']['free_transfers']} free this "
        "week, no points spent) — bounded downside; decide on the balance of evidence."
    )


def moderator_node(state: DebateState) -> dict:
    transcript = "\n".join(
        f"[{m['agent']}]{(' ' + m['stance']) if 'stance' in m else ''}: {m['message']}"
        for m in state["messages"]
    )
    # temperature=0: this is the node that decides, and sampling noise was
    # flipping it. The same code and data produced proceed=true and
    # proceed=false on consecutive runs.
    result: ModeratorDecision = _invoke_logged(
        state, "moderator", ModeratorDecision,
        [("system", MODERATOR_SYSTEM),
         ("user", _context_str(state) + "\n\n" + _proposal_block(state)
                  + f"\n\nFULL DEBATE TRANSCRIPT:\n{transcript}" + _cost_note(state))],
        temperature=0.0,
    )
    _log(state, "moderator", result.summary)
    final = {
        "proceed": result.proceed,
        "transfers": [t.model_dump(by_alias=True) for t in result.transfers],
        "confidence": result.confidence,
        "summary": result.summary,
    }
    return {"messages": [{"agent": "moderator", "message": result.summary}], "proposal": final}


def _needs_extra_round(state: DebateState) -> str:
    if state["requires_extra_scrutiny"] and state["scrutiny_rounds_done"] < 2:
        return "extra_round"
    return "rebut"


def build_graph():
    g = StateGraph(DebateState)
    g.add_node("analyst", analyst_node)
    g.add_node("fixture_form", fixture_form_node)
    g.add_node("news_injury", news_injury_node)
    g.add_node("risk_scrutiny", risk_scrutiny_node)
    g.add_node("rebuttal", rebuttal_node)
    g.add_node("moderator", moderator_node)

    g.add_edge(START, "analyst")
    g.add_edge("analyst", "fixture_form")
    g.add_edge("fixture_form", "news_injury")
    g.add_edge("news_injury", "risk_scrutiny")
    g.add_conditional_edges(
        "risk_scrutiny",
        _needs_extra_round,
        {"extra_round": "risk_scrutiny", "rebut": "rebuttal"},
    )
    g.add_edge("rebuttal", "moderator")
    g.add_edge("moderator", END)
    return g.compile()
