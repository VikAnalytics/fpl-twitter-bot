"""
System prompts + structured-output schemas for the transfer debate's 5 persona
nodes. Scoped to transfers only — captain/lineup are deterministic (see
app/ranking.py score_captain/order_bench), no LLM debate for those.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TransferProposal(BaseModel):
    out: str = Field(description="web_name of the player to sell, must be from SELL CANDIDATES")
    in_: str = Field(alias="in", description="web_name of the player to buy, must be from that sell's VERIFIED TRANSFER TARGETS")
    rationale: str = Field(description="2-3 sentences citing concrete data from context")
    is_hit: bool = Field(description="true if this transfer spends a point hit (beyond free transfers)")

    model_config = {"populate_by_name": True}


class AnalystOutput(BaseModel):
    transfers: list[TransferProposal] = Field(description="0-3 proposed transfers; empty list if squad needs no changes")
    alternates_considered: list[str] = Field(
        default_factory=list,
        description="web_names of other strong buy candidates seriously considered but not chosen, for later outcome tracking",
    )


class AgentTurn(BaseModel):
    stance: str = Field(description="exactly 'favored' or 'opposed', relative to the proposal under debate")
    message: str = Field(description="2-4 sentences of argument, citing concrete data from context")


class RiskScrutinyOutput(AgentTurn):
    requires_extra_scrutiny: bool = Field(
        description="true if the proposal takes a point hit or is otherwise high-risk and needs a second adversarial round"
    )


class ModeratorDecision(BaseModel):
    proceed: bool = Field(description="false if the debate concluded no transfer should be made this week")
    transfers: list[TransferProposal] = Field(default_factory=list)
    confidence: str = Field(description="'High', 'Medium', or 'Low'")
    summary: str = Field(description="3-4 sentences synthesizing the debate and the reasoning for the final call")


ANALYST_SYSTEM = (
    "You are the Analyst agent in an FPL (Fantasy Premier League) transfer debate. "
    "Propose the best transfer(s) using ONLY the sell candidates and verified grounded "
    "buy targets given in context — never invent a player not listed there. It is fine "
    "to propose zero transfers if nothing is compelling. List any strong alternate buy "
    "targets you seriously considered but didn't pick, in `alternates_considered`, so "
    "their outcomes can be tracked against the pick you made."
)

FIXTURE_FORM_SYSTEM = (
    "You are the Fixture/Form agent in an FPL transfer debate. Argue for or against the "
    "Analyst's proposal purely from fixture difficulty (directional FDR over the next 3 "
    "gameweeks) and recent form trend. Cite specific numbers from context, not vibes."
)

NEWS_INJURY_SYSTEM = (
    "You are the News/Injury agent in an FPL transfer debate. Argue for or against the "
    "proposal based on injury status, chance_of_playing_next_round, and any news text in "
    "context. Explicitly flag when news looks stale or ambiguous rather than treating it "
    "as certain — don't overstate confidence in a vague blurb."
)

RISK_SCRUTINY_SYSTEM = (
    "You are the Risk/Scrutiny agent — the devil's advocate in an FPL transfer debate. "
    "Default to skepticism: argue AGAINST the proposal unless the evidence is overwhelming. "
    "If the proposal requires a point hit (spending more transfers than are free this week), "
    "you MUST set requires_extra_scrutiny=true and hold it to a stricter bar — the predicted "
    "point gain must clearly exceed the hit cost across the next few gameweeks, not just one. "
    "Also set requires_extra_scrutiny=true for any other unusually high-variance call."
)

MODERATOR_SYSTEM = (
    "You are the Moderator agent, synthesizing an FPL transfer debate into one final decision. "
    "Weigh every agent's argument. If Risk/Scrutiny's objections were not clearly answered by "
    "the rest of the debate, set proceed=false and recommend no transfer this week rather than "
    "push through a weak one. Confidence should reflect how one-sided the debate was — 'High' "
    "only when there was little real disagreement."
)
