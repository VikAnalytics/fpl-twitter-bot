from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class DebateState(TypedDict):
    run_id: str
    decision_id: int
    gameweek: int
    context: dict
    messages: Annotated[list[dict], operator.add]
    round: int
    requires_extra_scrutiny: bool
    scrutiny_rounds_done: int
    candidates: Annotated[list[dict], operator.add]
    proposal: dict | None
