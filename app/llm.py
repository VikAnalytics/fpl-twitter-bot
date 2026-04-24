from __future__ import annotations

import json
import os
import sys
from openai import OpenAI, APIError, RateLimitError

from . import ranking
from .models import (
    BudgetInfo, Fixture, LeagueStanding, ManagerInfo, PlayerSummary,
    SquadPick, TransferOutcome, TransferRecommendation,
)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    return _client


_FALLBACK = "Analysis temporarily unavailable. Try again in a moment."


# ─────────────────────── Formatting helpers ───────────────────────

def _fixture_summary(fixtures: list[Fixture]) -> str:
    if not fixtures:
        return "No fixtures"
    parts = []
    for f in fixtures:
        if f.directional_fdr is not None:
            parts.append(f"{f.opp}({f.venue}) FDR{f.fdr}/dFDR{f.directional_fdr:.1f}")
        else:
            parts.append(f"{f.opp}({f.venue}) FDR{f.fdr}")
    avg_fdr = sum((f.directional_fdr if f.directional_fdr is not None else f.fdr) for f in fixtures) / len(fixtures)
    difficulty = "HARD" if avg_fdr >= 4 else ("TOUGH" if avg_fdr >= 3.3 else ("MIXED" if avg_fdr >= 2.7 else "EASY"))
    return f"{', '.join(parts)} | Avg {avg_fdr:.1f} ({difficulty})"


def _sell_candidates_str(sell_reports: list[ranking.SellReport]) -> str:
    if not sell_reports:
        return "Squad data unavailable."
    lines = []
    for r in sell_reports[:4]:
        p = r.player
        form_str = "→".join(str(x) for x in p.recent_form_5gw) if p.recent_form_5gw else "N/A"
        fix_str = _fixture_summary(p.fixtures_next_3)
        flag_str = ", ".join(r.flags) if r.flags else "no acute flags (relative weakest)"
        lines.append(
            f"- [{p.position}] {p.web_name} ({p.team_name}) £{p.now_cost}m | urgency {r.score:.1f}\n"
            f"  Last 5 GWs: {form_str} | Trend: {r.trend} | ep_next: {p.ep_next}\n"
            f"  Underlying: xG {p.xg:.1f}, xA {p.xa:.1f}, xGI/90 {p.xgi_per_90:.2f} | "
            f"Starts {p.starts_pct:.0f}% | YC {p.yellow_cards}\n"
            f"  Next 3 fixtures: {fix_str}\n"
            f"  Sell flags: {flag_str}"
        )
    return "\n".join(lines)


def format_squad_for_prompt(squad: list[SquadPick]) -> str:
    lines = []
    for pick in squad:
        p = pick.player
        role = ""
        if pick.is_captain:        role = "  ★ CAPTAIN"
        elif pick.is_vice_captain: role = "  ★ VICE-CAPTAIN"
        elif pick.position > 11:   role = "  [BENCH]"
        fix_str = _fixture_summary(p.fixtures_next_3)
        form_list = p.recent_form_5gw or []
        form_str = "→".join(str(pts) for pts in form_list) if form_list else "N/A"
        trend = ranking.form_trend(form_list)
        line = (
            f"[{p.position}] {p.web_name} ({p.team_name}) £{p.now_cost}m{role}\n"
            f"      Season: {p.total_points}pts, PPG:{p.points_per_game}, "
            f"Form:{p.form}, ep_next:{p.ep_next}, Owned:{p.selected_by_percent}%\n"
            f"      Last 5 GWs (newest→oldest): {form_str} | Trend: {trend}\n"
            f"      Fixtures: {fix_str}"
        )
        if p.chance_of_playing_next_round is not None:
            line += f"\n      ⚠ {p.news} ({p.chance_of_playing_next_round}% chance)"
        lines.append(line)
    return "\n".join(lines)


def _injury_summary(injury_flags: list[PlayerSummary]) -> str:
    if not injury_flags:
        return "No injury concerns."
    parts = []
    for p in injury_flags:
        chance = f"{p.chance_of_playing_next_round}%" if p.chance_of_playing_next_round is not None else "Doubt"
        parts.append(f"- {p.web_name} ({p.team_name}): {p.news or 'Injury concern'} [{chance}]")
    return "\n".join(parts)


def _format_grounded_targets(
    grounded_targets: dict[str, list[PlayerSummary]],
    sell_by_name: dict[str, PlayerSummary],
    gw: int,
) -> str:
    if not grounded_targets:
        return ""
    lines = ["VERIFIED TRANSFER TARGETS (FPL API + ranking — pick ONLY from these lists):"]
    for sell_name, targets in grounded_targets.items():
        lines.append(f"\n  If selling {sell_name}:")
        sold = sell_by_name.get(sell_name)
        for t in targets[:6]:
            report = ranking.score_buy_report(t, sold, gw) if sold else None
            fix_str = _fixture_summary(t.fixtures_next_3)
            form_str = "→".join(str(x) for x in t.recent_form_5gw) if t.recent_form_5gw else "N/A"
            flag_str = ", ".join(report.flags) if (report and report.flags) else ""
            lines.append(
                f"    • {t.web_name} ({t.team_name}) £{t.now_cost}m | score {report.score:.1f} | "
                f"ep_next:{t.ep_next} | form:{t.form} | 5GW:{form_str}\n"
                f"      xGI/90 {t.xgi_per_90:.2f} | Starts {t.starts_pct:.0f}% | "
                f"{'PEN1' if t.penalties_order == 1 else ''}"
                f"{' DFK' + str(t.direct_freekicks_order) if t.direct_freekicks_order and t.direct_freekicks_order <= 2 else ''}"
                f"\n      Fixtures: {fix_str}"
                + (f"\n      Flags: {flag_str}" if flag_str else "")
            )
    return "\n".join(lines)


# ─────────────────────── Web search (opt-in) ───────────────────────

def fetch_player_context(player_names: list[str], enabled: bool = False) -> str:
    """
    Gated behind `enabled` — default OFF. Expensive and flaky.
    Enable only on explicit deep-analysis requests.
    """
    if not enabled or not player_names:
        return ""
    names = ", ".join(player_names[:6])
    query = (
        f"Premier League current season — for these players: {names}.\n"
        "Search for and summarise (factual, current-season only, under 250 words):\n"
        "1. Manager press conference quotes on fitness/availability/rotation.\n"
        "2. Upcoming European fixtures in the next 10-14 days and expected starters.\n"
        "3. International call-up fatigue concerns.\n"
        "4. Yellow-card suspension risk.\n"
        "Skip players with nothing noteworthy."
    )
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o-search-preview",
            messages=[{"role": "user", "content": query}],
            max_tokens=500,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return ""


# ─────────────────────── Vibe check (unchanged) ───────────────────────

def generate_vibe_check(
    manager: ManagerInfo,
    squad: list[SquadPick],
    injury_flags: list[PlayerSummary],
) -> str:
    system = (
        "You are a brutally honest but entertaining FPL (Fantasy Premier League) analyst. "
        "You write like a knowledgeable friend who has seen too many FPL disasters. "
        "Be direct, specific, and use football slang naturally. "
        "Max 200 words. Flowing prose only — no bullet points."
    )
    user = (
        f"Manager: {manager.name} | Team: {manager.team_name}\n"
        f"Overall Rank: {manager.overall_rank:,} | Total Points: {manager.total_points}\n"
        f"Current Gameweek: {manager.current_gameweek}\n\n"
        f"SQUAD (fixtures include FDR 1=easy to 5=hardest):\n{format_squad_for_prompt(squad)}\n\n"
        f"INJURY CONCERNS:\n{_injury_summary(injury_flags)}\n\n"
        "Give this manager an honest 'Vibe Check' of their squad. Comment on: "
        "overall squad quality, captain choice, fixture difficulty for key assets, injury situation. "
        "End with one punchy verdict sentence."
    )
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.9,
            max_tokens=500,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content.strip()
    except (APIError, RateLimitError):
        return _FALLBACK


# ─────────────────────── Validators ───────────────────────

def _strip_club_annotation(name: str) -> str:
    """Strip trailing '(Club)' if LLM appended it."""
    n = name.strip()
    idx = n.rfind(" (")
    if idx > 0 and n.endswith(")"):
        return n[:idx].strip()
    return n


def _normalize_key(name: str) -> str:
    """Strip initial-dot prefix (e.g. 'F.Kadıoğlu' → 'kadıoğlu')."""
    n = name.lower().strip()
    if "." in n:
        parts = n.split(".", 1)
        if len(parts[0]) <= 3:  # likely an initial
            n = parts[1].strip()
    return n


def _resolve_name(
    name: str,
    index: dict[str, PlayerSummary],
) -> PlayerSummary | None:
    """Exact match → initial-stripped match → substring match (last resort)."""
    stripped = _strip_club_annotation(name).lower()
    if stripped in index:
        return index[stripped]
    norm = _normalize_key(stripped)
    for k, v in index.items():
        if _normalize_key(k) == norm:
            return v
    # substring as final fallback — require target to be >= 4 chars
    if len(norm) >= 4:
        candidates = [v for k, v in index.items() if norm in _normalize_key(k)]
        if len(candidates) == 1:
            return candidates[0]
    return None


def _validate_transfer(
    t: dict,
    player_index: dict[str, PlayerSummary],
    squad_by_name: dict[str, PlayerSummary],
    sell_by_name: dict[str, PlayerSummary],
    grounded_by_sell: dict[str, list[PlayerSummary]],
    budget_itb: float,
    recently_sold_names: set[str],
) -> PlayerSummary | None:
    """
    Returns the verified buy-player if transfer passes all constraints, else None.
    """
    out_name_raw = str(t.get("out", ""))
    in_name_raw = str(t.get("in", ""))
    if not out_name_raw or not in_name_raw:
        return None

    out_p = _resolve_name(out_name_raw, squad_by_name)
    in_p = _resolve_name(in_name_raw, player_index)
    if not out_p or not in_p:
        print(f"[VALIDATE] missing player: out={out_name_raw} in={in_name_raw}", file=sys.stderr)
        return None

    # Must be a real sell candidate
    if out_p.web_name not in sell_by_name:
        print(f"[VALIDATE] {out_p.web_name} not in sell candidates", file=sys.stderr)
        return None

    # Must be in grounded targets list for that sell
    targets = grounded_by_sell.get(out_p.web_name, [])
    if in_p.id not in {tt.id for tt in targets}:
        print(f"[VALIDATE] {in_p.web_name} not in grounded list for {out_p.web_name}", file=sys.stderr)
        return None

    # Same FPL position
    if out_p.position != in_p.position:
        print(f"[VALIDATE] position mismatch {out_p.position}→{in_p.position}", file=sys.stderr)
        return None

    # Different club
    if out_p.team_name == in_p.team_name:
        print(f"[VALIDATE] same club", file=sys.stderr)
        return None

    # Budget
    if in_p.now_cost > out_p.now_cost + budget_itb + 0.01:
        print(f"[VALIDATE] over budget: {in_p.now_cost} > {out_p.now_cost + budget_itb}", file=sys.stderr)
        return None

    # Recently sold exclusion
    if in_p.web_name.lower() in recently_sold_names:
        print(f"[VALIDATE] {in_name} recently sold, refusing flip-flop", file=sys.stderr)
        return None

    return in_p


# ─────────────────────── Main brief generation ───────────────────────

def generate_pre_deadline_brief(
    manager: ManagerInfo,
    squad: list[SquadPick],
    injury_flags: list[PlayerSummary],
    dgw_players: list[PlayerSummary],
    bgw_players: list[PlayerSummary],
    deadline_str: str,
    transfer_history: list[dict],
    budget: BudgetInfo,
    league_standings: list[LeagueStanding],
    grounded_targets: dict[str, list[PlayerSummary]] | None = None,
    sell_reports: list[ranking.SellReport] | None = None,
    active_chip: str | None = None,
    past_outcomes: list[TransferOutcome] | None = None,
    enable_web_search: bool = False,
) -> tuple[str, list[TransferRecommendation]]:

    if grounded_targets is None:
        grounded_targets = {}
    if sell_reports is None:
        sell_reports = []

    gw = manager.current_gameweek
    phase = ranking.season_phase(gw)
    sell_by_name = {r.player.web_name: r.player for r in sell_reports}
    squad_by_name = {p.player.web_name.lower(): p.player for p in squad}
    player_index = dict(squad_by_name)
    for targets in grounded_targets.values():
        for t in targets:
            player_index[t.web_name.lower()] = t

    # Recently-sold set for flip-flop exclusion (lookback 3 GWs)
    recent_sold_ids = ranking.recently_sold_ids(transfer_history, gw, lookback=3)
    # Need names too — look up via any target or squad
    recent_sold_names = set()
    for _id in recent_sold_ids:
        for p in list(player_index.values()):
            if p.id == _id:
                recent_sold_names.add(p.web_name.lower())

    # Feedback loop
    feedback = ranking.past_outcome_adjustment(past_outcomes or [])

    # External context (gated)
    sell_names_for_context = []
    for r in sell_reports[:4]:
        if any("injury" in f or "suspension" in f for f in r.flags):
            sell_names_for_context.append(r.player.web_name)
    external_context = fetch_player_context(sell_names_for_context, enabled=enable_web_search)

    # Build strings for prompt
    sell_candidates_str = _sell_candidates_str(sell_reports)
    grounded_str = _format_grounded_targets(grounded_targets, sell_by_name, gw)

    # Chip-aware transfer count target
    chip_note = ""
    max_transfers = 3
    if active_chip == "wildcard":
        chip_note = "WILDCARD IS ACTIVE — no hit cost for any transfers. Suggest 3-5 high-impact moves."
        max_transfers = 5
    elif active_chip == "freehit":
        chip_note = "FREE HIT IS ACTIVE — transfers reset after this GW. Maximize ep_next for this GW only; ignore long-term fixture ticker."
        max_transfers = 5
    elif active_chip in ("bboost", "3xc"):
        chip_note = f"{active_chip.upper()} CHIP ACTIVE — standard transfer rules but emphasize premium upside."

    system = (
        "You are a precision FPL (Fantasy Premier League) transfer analyst.\n\n"
        f"SEASON PHASE: {phase} (GW{gw}).\n"
        f"{chip_note}\n\n"
        "DATA PROVIDED:\n"
        "- Sell candidates with urgency score, flags, form trend, xG/xA, set-pieces.\n"
        "- Grounded buy targets with composite score, trend, directional FDR.\n"
        "- Directional FDR (dFDR) accounts for team attack/defence strength.\n\n"
        "RULES (STRICT):\n"
        "1. Only sell players from SELL CANDIDATES.\n"
        "2. Only buy from the VERIFIED TRANSFER TARGETS list for each sell.\n"
        "3. Never same club. Never same player. Match FPL position.\n"
        "4. Budget arithmetic will be checked by validator — do not invent numbers.\n"
        "5. For sell_reasoning: cite urgency flags + last 5 GWs + trend + directional FDR.\n"
        "6. For buy_reasoning: cite composite score flags + xGI/90 + set-piece role + directional FDR.\n"
        "7. External_context field: only if external context section is present.\n"
        "8. If free_transfers == 0, EACH transfer object's signals[] MUST include "
        "'4pt hit required'. Recommend only when projected gain justifies it.\n"
        "9. ALWAYS return 1–" + str(max_transfers) + " transfers unless squad is flawless. "
        "NEVER return empty transfers.\n\n"
        "OUTPUT: JSON with keys 'narrative' and 'transfers'.\n"
        "'narrative': 2–3 sentences on squad situation. DO NOT name transfer targets here.\n"
        "'transfers': array of 1–" + str(max_transfers) + " objects. Each has keys:\n"
        "  out, in, sell_reasoning, buy_reasoning, signals, external_context\n"
        "  'out' and 'in' MUST be the player's web_name ONLY (e.g. 'Saka', not 'Saka (Arsenal)').\n"
        "Signals array: 3-5 short strings. External_context: 1-2 sentences or empty string.\n"
        "confidence, budget_check, and clubs/prices will be filled by validator — do not set them.\n"
    )

    dgw_str = "\n".join(f"- {p.web_name} ({p.team_name})" for p in dgw_players) or "None"
    bgw_str = "\n".join(f"- {p.web_name} ({p.team_name})" for p in bgw_players) or "None"
    league_str = (
        "\n".join(f"- {l.name}: Rank {l.rank:,} / {l.total_managers:,}" for l in league_standings)
        or "No mini-league data"
    )
    outcome_str = feedback.get("caveat", "") or "No recent track record adjustment."

    user = (
        f"Deadline: {deadline_str}\n"
        f"Manager: {manager.name} ({manager.team_name}) | Rank: {manager.overall_rank:,}\n\n"

        f"BUDGET:\n"
        f"- In the bank: £{budget.itb}m\n"
        f"- Free transfers available: {budget.free_transfers}\n"
        f"- Transfers made this GW: {budget.transfers_made}\n"
        f"- Active chip: {active_chip or 'none'}\n\n"

        f"SELL CANDIDATES (ranked by urgency):\n{sell_candidates_str}\n\n"

        + (grounded_str + "\n\n" if grounded_str else "")

        + (f"EXTERNAL CONTEXT:\n{external_context}\n\n" if external_context else "") +

        f"FULL SQUAD:\n{format_squad_for_prompt(squad)}\n\n"
        f"INJURY FLAGS:\n{_injury_summary(injury_flags)}\n\n"
        f"DOUBLE GW ASSETS:\n{dgw_str}\n\n"
        f"BLANK GW CONCERNS:\n{bgw_str}\n\n"
        f"MINI-LEAGUE:\n{league_str}\n\n"
        f"PAST TRACK RECORD:\n{outcome_str}\n\n"

        "Produce the JSON response now. Return 1–" + str(max_transfers) + " transfers with "
        "sell_reasoning and buy_reasoning each 2 sentences. Do NOT put transfer advice in narrative."
    )

    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            max_tokens=1400,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
        )
        raw_content = resp.choices[0].message.content
        print(f"[LLM DEBUG] raw JSON:\n{raw_content[:2000]}", file=sys.stderr)
        data = json.loads(raw_content)
        narrative = data.get("narrative", _FALLBACK)
        if feedback.get("caveat"):
            narrative = f"{narrative} {feedback['caveat']}"
        raw_transfers = data.get("transfers", [])
        if not isinstance(raw_transfers, list):
            raw_transfers = []
    except (APIError, RateLimitError, json.JSONDecodeError, KeyError) as e:
        print(f"[LLM DEBUG] generation failed: {e}", file=sys.stderr)
        return _FALLBACK, []

    # ── Post-hoc validation + deterministic fields ────────────────────────────
    transfers: list[TransferRecommendation] = []
    green_threshold_high = 5 if feedback.get("tighten_confidence") else 4
    green_threshold_med = 3 if feedback.get("tighten_confidence") else 2

    for t in raw_transfers[: max_transfers]:
        if not isinstance(t, dict):
            continue
        in_p = _validate_transfer(
            t, player_index, squad_by_name, sell_by_name, grounded_targets,
            budget.itb, recent_sold_names,
        )
        if in_p is None:
            continue

        out_p = _resolve_name(str(t.get("out", "")), squad_by_name)
        if out_p is None:
            continue

        sell_report = next((r for r in sell_reports if r.player.id == out_p.id), None)
        buy_report = ranking.score_buy_report(in_p, out_p, gw)

        # Hit breakeven gate
        hit_required = budget.free_transfers == 0
        if hit_required and sell_report:
            if not ranking.hit_breakeven_ok(buy_report, sell_report):
                print(f"[VALIDATE] hit not profitable: {out_name}→{in_p.web_name}", file=sys.stderr)
                continue

        # Build signals (merge LLM-provided with deterministic)
        llm_signals = [str(s) for s in t.get("signals", []) if s]
        merged_signals = list(dict.fromkeys(llm_signals + buy_report.signals))
        if hit_required and not any("4pt hit" in s.lower() for s in merged_signals):
            merged_signals.append("4pt hit required")

        # Deterministic confidence from positive flag count
        green_count = len(buy_report.flags)
        # Penalize if sell urgency was low (weak case to sell in first place)
        if sell_report and sell_report.score < 8:
            green_count = max(0, green_count - 1)
        if green_count >= green_threshold_high:
            confidence = "High"
        elif green_count >= green_threshold_med:
            confidence = "Medium"
        else:
            confidence = "Low"

        # Deterministic budget_check
        net = round((out_p.now_cost + budget.itb) - in_p.now_cost, 1)
        budget_check = (
            f"Sell £{out_p.now_cost}m + £{budget.itb}m ITB = "
            f"£{round(out_p.now_cost + budget.itb, 1)}m available. "
            f"Target £{in_p.now_cost}m. Net: {'+' if net >= 0 else ''}£{net}m"
            + (" (4pt hit)" if hit_required else "")
        )

        transfers.append(TransferRecommendation(
            out=out_p.web_name,
            out_club=out_p.team_name,
            out_price=f"£{out_p.now_cost}m",
            in_=in_p.web_name,
            in_club=in_p.team_name,
            in_price=f"£{in_p.now_cost}m",
            sell_reasoning=str(t.get("sell_reasoning", "")),
            buy_reasoning=str(t.get("buy_reasoning", "")),
            budget_check=budget_check,
            confidence=confidence,
            signals=merged_signals[:6],
            external_context=str(t.get("external_context", "")),
        ))

    return narrative, transfers
