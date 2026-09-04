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
    """
    Recent-vs-older points delta. The split is 2-vs-rest once there are 3+
    gameweeks; with exactly 2 it falls back to 1-vs-1 rather than returning
    UNKNOWN, which used to blank out every form signal for the whole squad
    through GW3 (a <3-length list is all anyone has that early).

    NOTE this measures CHANGE, not LEVEL — a player scoring 1,1,2,1,2 is
    "STABLE" here and always will be. Absolute level is scored separately in
    score_sell (see the returns-floor signal).
    """
    if not form_5gw or len(form_5gw) < 2:
        return "UNKNOWN"
    split = 2 if len(form_5gw) >= 3 else 1
    recent = sum(form_5gw[:split]) / split
    older = sum(form_5gw[split:]) / len(form_5gw[split:])
    diff = recent - older
    # A 2-GW sample is one gameweek against one gameweek, where a single goal
    # swings the delta by 4+ — the 1.5/0.5 bands that work on averaged samples
    # would call 11-then-9 a collapse. Widen them so single-GW noise can't read
    # as a trend; level at that sample size is score_sell's returns floor.
    steep, shallow = (1.5, 0.5) if split == 2 else (4.0, 2.0)
    if diff <= -steep:
        return "DECLINING ↓↓"
    if diff < -shallow:
        return "DIPPING ↓"
    if diff >= steep:
        return "RISING ↑↑"
    if diff > shallow:
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


# Points-per-gameweek below which a player is simply not returning. A starter
# who plays 60+ minutes and does nothing banks 2 (appearance) plus the odd
# bonus/CS, so these sit just above "turned up and contributed nothing".
# Defenders/keepers are held to a lower bar than attackers by design.
_RETURNS_FLOOR = {"GKP": 2.5, "DEF": 2.8, "MID": 3.2, "FWD": 3.2}


def score_sell(p: PlayerSummary, gw: int = 20, is_backup_gk: bool = False) -> SellReport:
    """
    Urgency score. Higher = more urgent to sell.
    Returns SellReport with score + human-readable flags + raw signals.

    `is_backup_gk` marks the bench keeper FPL forces you to carry. He does not
    play, and that is the plan — scoring him on minutes and returns like an
    outfield player put him top of the sell list every single week on 0
    minutes, which is why he was previously excluded from the pool outright.
    Excluding him was too blunt (the keeper slot could then never be fixed), so
    instead the not-playing penalties are suppressed and he is judged on what
    actually matters for a backup: how much money he ties up, and whether he is
    injured or losing value.
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

    # 3b. Absolute return level — form_trend above only measures CHANGE, so a
    #     player who has been poor every single week reads as "STABLE →" and
    #     scores nothing from it. That is the gap that let consistent
    #     non-returners sit in the squad untouched. Score the level itself.
    sample = list(p.recent_form_5gw or [])
    if len(sample) >= 2:
        avg_return, sample_desc = sum(sample) / len(sample), f"last {len(sample)} GW"
    elif p.points_per_game > 0:
        avg_return, sample_desc = p.points_per_game, "season ppg"
    else:
        avg_return, sample_desc = None, ""

    poor_returns = False
    floor = _RETURNS_FLOOR.get(p.position, 3.0)
    if is_backup_gk:
        avg_return = None  # zero returns from a bench keeper is the arrangement, not a fault
    if avg_return is not None and avg_return < floor:
        poor_returns = True
        deficit = floor - avg_return
        flags.append(f"consistently poor returns ({avg_return:.1f} pts/GW over {sample_desc}, floor {floor})")
        signals.append(f"{avg_return:.1f} pts/GW ({sample_desc})")
        score += (12 + 6 * deficit) * w["form"]

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

    # 6. Minutes — the base rate under every other signal: a player who isn't
    #    on the pitch cannot return, whatever his price or the gameweek. This
    #    replaces a starts_pct-only check that was gated to `gw > 6 and
    #    now_cost >= 6.0`, so it never fired early season and never fired at
    #    all on cheap dead weight.
    gws_played = max(gw - 1, 1)
    minutes_share = p.minutes / (gws_played * 90.0)
    if is_backup_gk:
        # Judge him on the money he ties up instead. 4.0-4.5 is the enabler
        # band; anything above that is budget sitting on the bench all season.
        if p.now_cost > 4.5:
            excess = p.now_cost - 4.5
            flags.append(f"backup keeper tying up £{p.now_cost}m (£{excess:.1f}m above enabler price)")
            signals.append(f"Backup GK at £{p.now_cost}m")
            score += 12 + excess * 10
    elif gws_played >= 2:
        if p.minutes == 0:
            flags.append("no minutes played this season")
            signals.append("0 minutes")
            score += 35
        elif minutes_share < 0.45:
            flags.append(f"barely playing ({p.minutes} mins, {minutes_share * 100:.0f}% of available)")
            signals.append(f"{minutes_share * 100:.0f}% of available minutes")
            score += 22
        elif minutes_share < 0.70:
            flags.append(f"limited minutes ({p.minutes} mins, {minutes_share * 100:.0f}% of available)")
            signals.append(f"{minutes_share * 100:.0f}% of available minutes")
            score += 10
        elif p.starts_pct < 70:
            # Racks up the minutes but often from the bench — rotation risk proper.
            flags.append(f"rotation risk (starts {p.starts_pct:.0f}%)")
            signals.append(f"Starts {p.starts_pct:.0f}%")
            score += 8

    # 7. xG over-performance (regression risk) — only mid/late season, attacking players
    if gw >= 10 and p.position in ("MID", "FWD"):
        over_g = _xg_overperformance(p)
        over_a = _xa_overperformance(p)
        if over_g + over_a >= 3.0:
            flags.append(f"overperforming xG+xA by {over_g + over_a:.1f} (regression risk)")
            signals.append(f"Goals+Assists over xG+xA by {over_g + over_a:.1f}")
            score += 8 * w["underlying"]

    # 8. Price drop momentum — the market marking the asset down. A confirmed
    #    drop weighs more than an anticipated one, and both weigh double on a
    #    player already failing the returns floor: a falling price on someone
    #    who is delivering is noise, on a non-returner it is corroboration plus
    #    real team-value bleed. The old `now_cost >= 6.0` gate on the
    #    transfers-out branch is dropped — cheap dead weight gets churned too,
    #    and `poor_returns` is what separates signal from enabler churn.
    net = p.transfers_in_event - p.transfers_out_event
    price_dropped = p.cost_change_event < 0
    if price_dropped or net < -50_000:
        drop_score = 10 if price_dropped else 6
        if poor_returns:
            drop_score *= 2
            flags.append("price dropping while not returning (team value bleeding)")
        else:
            flags.append("price dropping" if price_dropped else "price drop imminent (heavy transfers out)")
        signals.append("Price fell this GW" if price_dropped else "Mass transfers out")
        score += drop_score

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


# ─────────────────────── Best XI selection ───────────────────────

# All 8 formations legal under FPL's squad rules: exactly 1 GK, and
# DEF/MID/FWD each within [3,5]/[2,5]/[1,3] summing to 10 outfield players.
_VALID_FORMATIONS = [
    (d, m, 10 - d - m)
    for d in range(3, 6)
    for m in range(2, 6)
    if 1 <= 10 - d - m <= 3
]


def gameweek_fixture_weight(p: PlayerSummary, gw: int) -> float:
    """
    Multiplier on a player's predicted points for THIS gameweek's fixture.

    The XI is a one-week decision, but the only fixture signal reaching it was
    the model's `avg_fdr` over the next THREE gameweeks — and that feature is
    dead anyway (app/ml/train.py hardcodes avg_fdr to 3.0 in training, and
    `opponent_strength` was trained on opponent_team id / 20, so the model has
    no working fixture input at all). The three-week average is what started
    van Ewijk at home to nobody while Coventry played Man City away: his
    MCI(A) FDR 5 averaged with FDR 2 and 3 into a healthier 3.33 than
    Ballard's 4.0, whose hard games are LATER.

    Scoring this gameweek's fixture directly, deterministically, keeps the
    lineup honest without another train/serve skew. Returns 0.0 for a blank
    (no fixture in `gw` at all) and sums both legs of a double.
    """
    legs = [f for f in p.fixtures_next_3 if f.event == gw]
    if not legs:
        return 0.0

    # Keepers and defenders live on clean sheets, which are close to binary and
    # almost entirely opponent-driven — a defender away at the best team in the
    # league is a different asset from the same defender at home to the worst.
    # Attackers travel better: a premium forward scores against anyone, so his
    # fixture matters but nothing like as much.
    sensitivity = 0.16 if p.position in ("GKP", "DEF") else 0.10

    total = 0.0
    for f in legs:
        fdr = f.directional_fdr if f.directional_fdr is not None else float(f.fdr)
        # DEF: FDR 1 -> 1.32, 3 -> 1.0, 5 -> 0.68.  MID/FWD: 1.2 / 1.0 / 0.8.
        total += max(0.5, min(1.4, 1.0 + (3.0 - fdr) * sensitivity))
    return round(total, 3)


def apply_fixture_weighting(
    players: list[PlayerSummary], predicted_points: dict[int, float], gw: int
) -> dict[int, float]:
    """Predicted points scaled by this gameweek's fixture — see gameweek_fixture_weight."""
    return {
        p.id: round(predicted_points.get(p.id, p.ep_next) * gameweek_fixture_weight(p, gw), 3)
        for p in players
    }


@dataclass
class LineupSelection:
    starting: list[PlayerSummary]
    bench: list[PlayerSummary]
    formation: str  # e.g. "4-4-2"
    starting_expected_points: float


def select_best_xi(squad_15: list[PlayerSummary], predicted_points: dict[int, float]) -> LineupSelection:
    """
    Deterministic best-XI selection — no LLM, and deliberately not folded
    into the transfer debate. For a FIXED formation, the optimal XI is just
    the top-N predicted-points players per position (points are additive
    across players with no synergy term, so an exchange argument makes
    top-N provably optimal within that formation). This brute-forces all 8
    legal FPL formations and keeps whichever maximizes total predicted
    points — cheap (8 sums over pre-sorted lists) and exact, not a heuristic.
    """
    def _pred(p: PlayerSummary) -> float:
        return predicted_points.get(p.id, p.ep_next)

    by_pos: dict[str, list[PlayerSummary]] = {"GKP": [], "DEF": [], "MID": [], "FWD": []}
    for p in squad_15:
        by_pos.setdefault(p.position, []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=_pred, reverse=True)

    gk = by_pos["GKP"][:1]
    best: tuple[float, list[PlayerSummary], str] | None = None
    for d, m, f in _VALID_FORMATIONS:
        if d > len(by_pos["DEF"]) or m > len(by_pos["MID"]) or f > len(by_pos["FWD"]):
            continue
        xi = gk + by_pos["DEF"][:d] + by_pos["MID"][:m] + by_pos["FWD"][:f]
        total = sum(_pred(p) for p in xi)
        if best is None or total > best[0]:
            best = (total, xi, f"{d}-{m}-{f}")

    if best is None:
        # Should be unreachable for a legal 2-5-5-3 squad, but degrade to
        # "whatever's there" rather than crash the pipeline over a formation
        # edge case (e.g. an incomplete squad mid-transfer-window).
        xi = squad_15[:11]
        total = sum(_pred(p) for p in xi)
        best = (total, xi, "unknown")

    total, xi, formation = best
    starting_ids = {p.id for p in xi}
    bench = [p for p in squad_15 if p.id not in starting_ids]
    return LineupSelection(
        starting=xi, bench=bench, formation=formation, starting_expected_points=round(total, 2),
    )


# ─────────────────────── Captain & bench ───────────────────────

@dataclass
class CaptainPick:
    player: PlayerSummary
    vice: PlayerSummary
    expected_points: float
    vice_expected_points: float
    rationale: str


def score_captain(
    xi: list[PlayerSummary],
    predicted_points: dict[int, float],
    starts_pct_floor: float = 60.0,
) -> CaptainPick:
    """
    Deterministic captain pick — no LLM debate, this is an argmax problem.
    Score = predicted_points * chance_of_playing, restricted to nailed starters.
    Fixture (directional FDR) is the tiebreak. Vice = runner-up.
    """
    def _weighted(p: PlayerSummary) -> float:
        chance = (p.chance_of_playing_next_round if p.chance_of_playing_next_round is not None else 100) / 100.0
        pred = predicted_points.get(p.id, p.ep_next)
        return pred * chance

    # Rotation-risk filter. Falls back to the full XI if nobody clears the floor
    # (e.g. early season when starts_pct is still low for everyone).
    pool = [p for p in xi if p.starts_pct >= starts_pct_floor] or list(xi)
    ranked = sorted(
        pool,
        key=lambda p: (_weighted(p), -_avg_fdr(p.fixtures_next_3)),
        reverse=True,
    )
    top = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else ranked[0]

    rationale = (
        f"{top.web_name}: {_weighted(top):.1f} weighted expected points "
        f"({predicted_points.get(top.id, top.ep_next):.1f} pred x "
        f"{(top.chance_of_playing_next_round or 100)}% chance), "
        f"fixture FDR {_avg_fdr(top.fixtures_next_3):.1f}. "
        f"Vice: {runner_up.web_name} ({_weighted(runner_up):.1f})."
    )
    return CaptainPick(
        player=top,
        vice=runner_up,
        expected_points=_weighted(top),
        vice_expected_points=_weighted(runner_up),
        rationale=rationale,
    )


@dataclass
class BenchSlot:
    player: PlayerSummary
    order: int  # 1 = first sub, ascending priority
    expected_points: float


def order_bench(
    bench: list[PlayerSummary],
    predicted_points: dict[int, float],
) -> list[BenchSlot]:
    """
    Deterministic bench order — sort by predicted points * playing-chance.
    GKP always ranked first (FPL's own auto-sub rules require the bench
    goalkeeper in the first bench slot — confirmed live: the /my-team/
    endpoint rejects a payload with the bench GK anywhere else with
    "Sub-position not allowed for element type"). Outfield subs fill the
    remaining slots in priority order after that.
    """
    def _weighted(p: PlayerSummary) -> float:
        chance = (p.chance_of_playing_next_round if p.chance_of_playing_next_round is not None else 100) / 100.0
        pred = predicted_points.get(p.id, p.ep_next)
        return pred * chance

    outfield = sorted(
        (p for p in bench if p.position != "GKP"),
        key=_weighted,
        reverse=True,
    )
    gkp = [p for p in bench if p.position == "GKP"]

    ordered = gkp + outfield
    return [
        BenchSlot(player=p, order=i + 1, expected_points=round(_weighted(p), 2))
        for i, p in enumerate(ordered)
    ]


