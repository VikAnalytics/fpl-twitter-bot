"""
Central scoring logic for transfer suggestions.

- `score_sell(player)`: urgency score for outgoing players (higher = sell now).
- `score_buy(candidate, vs_sold)`: attractiveness of a replacement relative to sold player.
- `build_sell_flags(player)`: deterministic list of human-readable signals per sell candidate.
- `build_buy_flags(candidate, vs_sold)`: same for buy candidate.
- `confidence_from_signals(signals)`: rule-based High/Medium/Low.
- `hit_breakeven_ok(...)`: 4pt-hit profitability gate.
- `season_phase(gw)`: EARLY/MID/LATE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .models import Fixture, PlayerSummary


# ─────────────────────── Phase + weights ───────────────────────

Phase = Literal["EARLY", "MID", "LATE"]


def season_phase(gw: int) -> Phase:
    if gw <= 5:
        return "EARLY"
    if gw >= 30:
        return "LATE"
    return "MID"


def _phase_weights(phase: Phase) -> dict[str, float]:
    """Signal weight multipliers per phase."""
    if phase == "EARLY":
        return {"form": 0.6, "fixtures": 0.8, "underlying": 1.2, "ownership": 1.0}
    if phase == "LATE":
        return {"form": 1.0, "fixtures": 1.4, "underlying": 0.9, "ownership": 1.0}
    return {"form": 1.0, "fixtures": 1.0, "underlying": 1.0, "ownership": 1.0}


# ─────────────────────── Form trend ───────────────────────

def form_trend(form_5gw: list[int]) -> str:
    if not form_5gw or len(form_5gw) < 3:
        return "UNKNOWN"
    recent = sum(form_5gw[:2]) / 2
    older = sum(form_5gw[2:]) / len(form_5gw[2:])
    diff = recent - older
    if diff <= -1.5:
        return "DECLINING ↓↓"
    if diff < -0.5:
        return "DIPPING ↓"
    if diff >= 1.5:
        return "RISING ↑↑"
    if diff > 0.5:
        return "IMPROVING ↑"
    return "STABLE →"


def _avg_fdr(fixtures: list[Fixture], directional: bool = True) -> float:
    if not fixtures:
        return 3.0
    if directional:
        vals = [f.directional_fdr if f.directional_fdr is not None else float(f.fdr) for f in fixtures]
    else:
        vals = [float(f.fdr) for f in fixtures]
    return sum(vals) / len(vals)


# ─────────────────────── Underlying perf ───────────────────────

def _xg_overperformance(p: PlayerSummary) -> float:
    """
    goals - xg. >0 means finishing lucky (regression risk).
    Only meaningful after ~5 GWs of data.
    """
    return p.goals_scored - p.xg


def _xa_overperformance(p: PlayerSummary) -> float:
    return p.assists - p.xa


# ─────────────────────── Sell scoring ───────────────────────

@dataclass
class SellReport:
    player: PlayerSummary
    score: float
    flags: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    trend: str = ""


def score_sell(p: PlayerSummary, gw: int = 20) -> SellReport:
    """
    Urgency score. Higher = more urgent to sell.
    Returns SellReport with score + human-readable flags + raw signals.
    """
    phase = season_phase(gw)
    w = _phase_weights(phase)
    score = 0.0
    flags: list[str] = []
    signals: list[str] = []

    # 1. Injury / doubt — highest priority
    if p.chance_of_playing_next_round is not None and p.chance_of_playing_next_round < 75:
        severity = 75 - p.chance_of_playing_next_round
        flags.append(f"injury doubt ({p.chance_of_playing_next_round}%)")
        signals.append(f"Chance of playing {p.chance_of_playing_next_round}%")
        score += 30 + severity

    # 2. Suspension risk
    if p.suspension_risk:
        flags.append(f"suspension risk ({p.yellow_cards} YC)")
        signals.append(f"{p.yellow_cards} yellow cards")
        score += 12

    # 3. Form trend
    trend = form_trend(p.recent_form_5gw)
    if "DECLINING" in trend:
        recent_avg = sum(p.recent_form_5gw[:2]) / 2 if len(p.recent_form_5gw) >= 2 else 0
        flags.append(f"form {trend} (last 2 GW avg: {recent_avg:.1f}pts)")
        signals.append(f"Form {trend}")
        score += 20 * w["form"]
    elif "DIPPING" in trend:
        recent_avg = sum(p.recent_form_5gw[:2]) / 2 if len(p.recent_form_5gw) >= 2 else 0
        flags.append(f"form {trend} (last 2 GW avg: {recent_avg:.1f}pts)")
        signals.append(f"Form {trend}")
        score += 10 * w["form"]

    # 4. ep_next
    if p.position != "GKP":
        if p.ep_next < 3.0:
            flags.append(f"low ep_next ({p.ep_next})")
            signals.append(f"ep_next {p.ep_next}")
            score += 15
        elif p.ep_next < 4.5:
            score += 5

    # 5. Fixtures (directional when available)
    avg_fdr = _avg_fdr(p.fixtures_next_3, directional=True)
    if avg_fdr >= 4.0:
        flags.append(f"very tough fixtures (avg FDR {avg_fdr:.1f})")
        signals.append(f"FDR avg {avg_fdr:.1f}")
        score += 15 * w["fixtures"]
    elif avg_fdr >= 3.3:
        flags.append(f"tough fixtures (avg FDR {avg_fdr:.1f})")
        signals.append(f"FDR avg {avg_fdr:.1f}")
        score += 8 * w["fixtures"]
    score += avg_fdr  # tiebreak

    # 6. Rotation / minutes risk
    if gw > 6 and p.starts_pct < 70 and p.now_cost >= 6.0:
        flags.append(f"rotation risk (starts {p.starts_pct:.0f}%)")
        signals.append(f"Starts {p.starts_pct:.0f}%")
        score += 10

    # 7. xG over-performance (regression risk) — only mid/late season, attacking players
    if gw >= 10 and p.position in ("MID", "FWD"):
        over_g = _xg_overperformance(p)
        over_a = _xa_overperformance(p)
        if over_g + over_a >= 3.0:
            flags.append(f"overperforming xG+xA by {over_g + over_a:.1f} (regression risk)")
            signals.append(f"Goals+Assists over xG+xA by {over_g + over_a:.1f}")
            score += 8 * w["underlying"]

    # 8. Price drop momentum
    net = p.transfers_in_event - p.transfers_out_event
    if p.cost_change_event < 0 or (net < -50_000 and p.now_cost >= 6.0):
        flags.append("price dropping (heavy transfers out)")
        signals.append("Mass transfers out")
        score += 6

    return SellReport(
        player=p,
        score=score,
        flags=flags,
        signals=signals,
        trend=trend,
    )


# ─────────────────────── Buy scoring ───────────────────────

@dataclass
class BuyReport:
    player: PlayerSummary
    score: float
    flags: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    trend: str = ""


def score_buy_report(c: PlayerSummary, vs_sold: PlayerSummary, gw: int = 20) -> BuyReport:
    phase = season_phase(gw)
    w = _phase_weights(phase)
    score = 0.0
    flags: list[str] = []
    signals: list[str] = []

    # 1. ep_next — anchor
    score += c.ep_next * 5

    # 2. Form trend
    trend = form_trend(c.recent_form_5gw)
    if "RISING" in trend:
        flags.append(f"form {trend}")
        signals.append(f"Form {trend}")
        score += 18 * w["form"]
    elif "IMPROVING" in trend:
        flags.append(f"form {trend}")
        signals.append(f"Form {trend}")
        score += 10 * w["form"]
    elif "DECLINING" in trend or "DIPPING" in trend:
        score -= 12 * w["form"]

    # 3. Underlying xGI_per_90
    if c.xgi_per_90 >= 0.6:
        flags.append(f"elite xGI/90 ({c.xgi_per_90:.2f})")
        signals.append(f"xGI/90 {c.xgi_per_90:.2f}")
        score += 14 * w["underlying"]
    elif c.xgi_per_90 >= 0.4:
        signals.append(f"xGI/90 {c.xgi_per_90:.2f}")
        score += 7 * w["underlying"]

    # 4. Fixtures (directional)
    avg_fdr = _avg_fdr(c.fixtures_next_3, directional=True)
    if avg_fdr <= 2.3:
        flags.append(f"dreamy fixtures (avg FDR {avg_fdr:.1f})")
        signals.append(f"FDR avg {avg_fdr:.1f}")
        score += 18 * w["fixtures"]
    elif avg_fdr <= 2.8:
        flags.append(f"good fixtures (avg FDR {avg_fdr:.1f})")
        signals.append(f"FDR avg {avg_fdr:.1f}")
        score += 10 * w["fixtures"]
    elif avg_fdr >= 3.7:
        score -= 10 * w["fixtures"]
    score -= avg_fdr  # tiebreak (lower FDR ranks higher)

    # 5. Set pieces
    sp_bonus = 0.0
    sp_signals = []
    if c.penalties_order == 1:
        sp_bonus += 12
        sp_signals.append("first-choice pen")
    if c.direct_freekicks_order and c.direct_freekicks_order <= 2:
        sp_bonus += 5
        sp_signals.append(f"DFK #{c.direct_freekicks_order}")
    if c.corners_order and c.corners_order <= 2 and c.position in ("MID", "FWD"):
        sp_bonus += 3
    if sp_bonus:
        flags.append("set-piece duties: " + ", ".join(sp_signals) if sp_signals else "set-piece involvement")
        score += sp_bonus

    # 6. Minutes reliability
    if c.starts_pct >= 85:
        signals.append(f"Starts {c.starts_pct:.0f}%")
        score += 6
    elif c.starts_pct < 60 and gw > 8:
        score -= 10

    # 7. Price change momentum (imminent rise)
    net = c.transfers_in_event - c.transfers_out_event
    if c.cost_change_event > 0 or net > 100_000:
        flags.append("rising price (heavy transfers in)")
        signals.append("Mass transfers in")
        score += 4

    # 8. Value vs sold — prefer not a pure downgrade
    value_delta = (c.ep_next - vs_sold.ep_next)
    if value_delta > 1.0:
        signals.append(f"+{value_delta:.1f} ep_next vs outgoing")
        score += 12
    elif value_delta > 0.3:
        score += 5
    elif value_delta < -0.5:
        score -= 8

    # 9. Role similarity (xGI axis) — penalize drastic role mismatch for DEF/MID
    if vs_sold.position in ("DEF", "MID") and vs_sold.role_score > 0.2:
        # sold is attacking; target should be too
        role_diff = abs(c.role_score - vs_sold.role_score)
        if role_diff > 0.35:
            score -= 6  # role mismatch

    # 10. Clean-sheet potential for DEF/GKP
    if c.position in ("DEF", "GKP") and c.xgc_per_90 and c.xgc_per_90 < 1.0:
        signals.append(f"xGC/90 {c.xgc_per_90:.2f}")
        score += 6

    # 11. Ownership — differential vs template depends on caller; surface as signal
    signals.append(f"{c.selected_by_percent}% owned")

    # 12. Injury doubt penalty
    if c.chance_of_playing_next_round is not None and c.chance_of_playing_next_round < 75:
        score -= 25

    return BuyReport(
        player=c,
        score=score,
        flags=flags,
        signals=signals,
        trend=trend,
    )


def score_buy(c: PlayerSummary, vs_sold: PlayerSummary, gw: int = 20) -> float:
    """Thin wrapper returning just the numeric score (used as sort key)."""
    return score_buy_report(c, vs_sold, gw).score


# ─────────────────────── Derived outputs ───────────────────────

def confidence_from_signals(signals: list[str], green_count: int) -> str:
    """
    Deterministic confidence based on green signal count.
    green_count = explicit count of positive signals (form↑, good fixtures, ep lift).
    """
    if green_count >= 4:
        return "High"
    if green_count >= 2:
        return "Medium"
    return "Low"


def hit_breakeven_ok(
    buy_report: BuyReport,
    sell_report: SellReport,
    hit_cost: int = 4,
) -> bool:
    """
    Rough expected gain over hit cost.
    gain ≈ (buy.ep_next - sell.ep_next) + fixture_swing + form_swing
    """
    ep_gain = buy_report.player.ep_next - sell_report.player.ep_next
    # fixture swing over 3 GWs: (sell_fdr - buy_fdr) * 0.5pts/fdr ≈ rough heuristic
    sell_fdr = _avg_fdr(sell_report.player.fixtures_next_3)
    buy_fdr = _avg_fdr(buy_report.player.fixtures_next_3)
    fixture_swing = (sell_fdr - buy_fdr) * 1.5  # 3 GW cumulative
    total_gain = ep_gain + fixture_swing
    return total_gain >= hit_cost


def recently_sold_ids(transfer_history: list[dict], current_gw: int, lookback: int = 3) -> set[int]:
    """Player IDs the manager has transferred out within the last `lookback` GWs."""
    cutoff = current_gw - lookback
    return {
        t["element_out"]
        for t in transfer_history
        if t.get("event", 0) >= cutoff and t.get("element_out")
    }


# ─────────────────────── Feedback loop ───────────────────────

def past_outcome_adjustment(past_outcomes: list) -> dict:
    """
    Summarize past suggestion performance to adjust confidence thresholds.
    Returns {"accuracy": 0.0-1.0, "avg_delta": float, "tighten_confidence": bool, "caveat": str}.
    """
    evaluated = [o for o in past_outcomes if o.delta is not None]
    if not evaluated:
        return {"accuracy": None, "avg_delta": 0.0, "tighten_confidence": False, "caveat": ""}
    deltas = [o.delta for o in evaluated]
    avg_delta = sum(deltas) / len(deltas)
    wins = sum(1 for d in deltas if d > 0)
    accuracy = wins / len(deltas)
    tighten = avg_delta < -2.0 or accuracy < 0.4
    caveat = ""
    if tighten:
        caveat = (
            f"Recent suggestion track record: {wins}/{len(deltas)} wins, avg delta {avg_delta:+.1f}. "
            "Be extra skeptical this week."
        )
    return {
        "accuracy": accuracy,
        "avg_delta": avg_delta,
        "tighten_confidence": tighten,
        "caveat": caveat,
    }
