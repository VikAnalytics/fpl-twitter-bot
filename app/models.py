from __future__ import annotations

from pydantic import BaseModel
from typing import Optional


class Fixture(BaseModel):
    opp: str
    venue: str   # "H" or "A"
    fdr: int     # 1–5, 5 = hardest


class PlayerSummary(BaseModel):
    id: int
    web_name: str
    team_name: str
    position: str
    total_points: int
    form: float
    selected_by_percent: float
    now_cost: float = 0.0          # £m price
    ep_next: float = 0.0           # FPL ML expected points next GW
    points_per_game: float = 0.0
    recent_form_5gw: list[int] = []  # last 5 GW points, newest first
    chance_of_playing_next_round: Optional[int] = None
    news: str = ""
    fixtures_next_3: list[Fixture] = []


class SquadPick(BaseModel):
    player: PlayerSummary
    position: int
    multiplier: int
    is_captain: bool
    is_vice_captain: bool


class ManagerInfo(BaseModel):
    id: int
    name: str
    team_name: str
    overall_rank: int
    total_points: int
    current_gameweek: int


class BudgetInfo(BaseModel):
    itb: float           # £m in the bank
    team_value: float    # £m team value
    transfers_made: int  # this GW
    hit_cost: int        # points deducted
    free_transfers: int  # remaining free transfers this GW


class LeagueStanding(BaseModel):
    name: str
    rank: int
    total_managers: int


class AuditResult(BaseModel):
    manager: ManagerInfo
    squad: list[SquadPick]
    vibe_check_narrative: str
    injury_flags: list[PlayerSummary]
    captain_score: str


class TransferRecommendation(BaseModel):
    out: str                  # player being sold
    out_club: str
    out_price: str            # e.g. "£8.5m"
    in_: str                  # player being bought (in_ to avoid Python keyword clash)
    in_club: str
    in_price: str
    sell_reasoning: str       # why sell: form data, injury, fixtures
    buy_reasoning: str        # why buy: form, ep_next, fixtures, DGW
    budget_check: str         # explicit budget arithmetic
    confidence: str           # High / Medium / Low
    signals: list[str]        # e.g. ["Form DECLINING ↓↓", "FDR avg 4.3"]
    external_context: str = ""  # press conferences, European fixtures, international impacts


class TransferOutcome(BaseModel):
    gameweek: int
    out_name: str
    in_name: str
    implemented: bool
    out_points: Optional[int]  # points the "out" player scored that GW
    in_points: Optional[int]   # points the "in" player scored that GW
    delta: Optional[int]       # in_points - out_points. positive = good call


class BriefResult(BaseModel):
    manager: ManagerInfo
    squad: list[SquadPick]
    deadline_str: str
    brief_narrative: str
    transfer_recommendations: list[TransferRecommendation]
    injury_flags: list[PlayerSummary]
    dgw_players: list[PlayerSummary]
    bgw_players: list[PlayerSummary]
    budget: BudgetInfo
    league_standings: list[LeagueStanding]
    past_outcomes: list[TransferOutcome] = []
