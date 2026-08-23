"""
LangGraph state machine for the transfer debate:

  analyst -> fixture_form -> news_injury -> risk_scrutiny
                                                 |
                          (extra round if hit/high-risk, capped at 1 extra)
                                                 v
                                             moderator -> END

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
    RISK_SCRUTINY_SYSTEM,
    AgentTurn,
    AnalystOutput,
    ModeratorDecision,
    RiskScrutinyOutput,
)
from .state import DebateState

_MODEL_NAME = "gpt-4o-mini"


def _llm(schema):
    model = ChatOpenAI(model=_MODEL_NAME, temperature=0.2)
    # include_raw=True so token usage is available — with_structured_output()
    # alone only returns the parsed object, discarding the raw AIMessage's
    # usage_metadata that cost tracking needs.
    return model.with_structured_output(schema, include_raw=True)


def _log(state: DebateState, agent_name: str, message: str) -> None:
    db.log_agent_message(state["decision_id"], state["gameweek"], state["round"], agent_name, message)


def _context_str(state: DebateState) -> str:
    return state["context"]["prompt_text"]


def _invoke_logged(state: DebateState, agent_name: str, schema, messages):
    """
    Runs the LLM call inside an observability.step() so duration, token
    usage, estimated cost, and the full structured output (or the exact
    failure) land in pipeline_log, tagged to this decision/gameweek/run.
    """
    with observability.step(
        state["run_id"], f"debate.{agent_name}",
        gameweek=state["gameweek"], decision_id=state["decision_id"],
    ) as ctx:
        raw_result = _llm(schema).invoke(messages)
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
    if result.transfers:
        msg = "; ".join(f"{t.out} -> {t.in_}: {t.rationale}" for t in result.transfers)
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
        [("system", system), ("user", _context_str(state) + f"\n\nPROPOSAL UNDER DEBATE:\n{state['proposal']}")],
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
            ("user", _context_str(state) + f"\n\nPROPOSAL UNDER DEBATE:\n{state['proposal']}" + round_note),
        ],
    )
    _log(state, "risk_scrutiny", result.message)
    return {
        "messages": [{"agent": "risk_scrutiny", "stance": result.stance, "message": result.message}],
        "requires_extra_scrutiny": result.requires_extra_scrutiny,
        "scrutiny_rounds_done": state["scrutiny_rounds_done"] + 1,
        "round": state["round"] + 1,
    }


def moderator_node(state: DebateState) -> dict:
    transcript = "\n".join(
        f"[{m['agent']}]{(' ' + m['stance']) if 'stance' in m else ''}: {m['message']}"
        for m in state["messages"]
    )
    result: ModeratorDecision = _invoke_logged(
        state, "moderator", ModeratorDecision,
        [("system", MODERATOR_SYSTEM), ("user", _context_str(state) + f"\n\nFULL DEBATE TRANSCRIPT:\n{transcript}")],
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
    return "moderate"


def build_graph():
    g = StateGraph(DebateState)
    g.add_node("analyst", analyst_node)
    g.add_node("fixture_form", fixture_form_node)
    g.add_node("news_injury", news_injury_node)
    g.add_node("risk_scrutiny", risk_scrutiny_node)
    g.add_node("moderator", moderator_node)

    g.add_edge(START, "analyst")
    g.add_edge("analyst", "fixture_form")
    g.add_edge("fixture_form", "news_injury")
    g.add_edge("news_injury", "risk_scrutiny")
    g.add_conditional_edges(
        "risk_scrutiny",
        _needs_extra_round,
        {"extra_round": "risk_scrutiny", "moderate": "moderator"},
    )
    g.add_edge("moderator", END)
    return g.compile()
