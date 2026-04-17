from __future__ import annotations

import json
import os
from openai import OpenAI, APIError, RateLimitError
from .models import BudgetInfo, Fixture, LeagueStanding, ManagerInfo, PlayerSummary, SquadPick, TransferRecommendation

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    return _client


_FALLBACK = "Analysis temporarily unavailable. Try again in a moment."


def _form_trend(form_5gw: list) -> str:
    if not form_5gw or len(form_5gw) < 3:
        return "UNKNOWN"
    recent = sum(form_5gw[:2]) / 2
    older  = sum(form_5gw[2:]) / len(form_5gw[2:])
    diff = recent - older
    if diff <= -1.5:  return "DECLINING ↓↓"
    if diff <  -0.5:  return "DIPPING ↓"
    if diff >=  1.5:  return "RISING ↑↑"
    if diff >   0.5:  return "IMPROVING ↑"
    return "STABLE →"


def _fixture_summary(fixtures: list[Fixture]) -> str:
    """Full 3-fixture breakdown with FDRs and avg difficulty."""
    if not fixtures:
        return "No fixtures"
    parts = [f"{f.opp}({f.venue}) FDR{f.fdr}" for f in fixtures]
    avg_fdr = sum(f.fdr for f in fixtures) / len(fixtures)
    difficulty = "HARD" if avg_fdr >= 4 else ("TOUGH" if avg_fdr >= 3.3 else ("MIXED" if avg_fdr >= 2.7 else "EASY"))
    return f"{', '.join(parts)} | Avg FDR {avg_fdr:.1f} ({difficulty})"


def _sell_candidates(squad: list[SquadPick]) -> str:
    """
    Score every starting XI player and surface the weakest ones.
    Always returns at least 3 candidates (worst-ranked, even if form is stable),
    so GPT always has targets to work with.
    """
    scored = []
    for pick in squad:
        if pick.position > 11:
            continue
        p = pick.player
        flags = []
        score = 0  # higher = more urgent to sell

        # injury / doubt (highest priority)
        if p.chance_of_playing_next_round is not None and p.chance_of_playing_next_round < 75:
            flags.append(f"injury doubt ({p.chance_of_playing_next_round}%)")
            score += 30 + (75 - p.chance_of_playing_next_round)

        # form trend
        trend = _form_trend(p.recent_form_5gw)
        if "DECLINING" in trend:
            recent_avg = sum(p.recent_form_5gw[:2]) / 2 if len(p.recent_form_5gw) >= 2 else 0
            flags.append(f"form {trend} (last 2 GW avg: {recent_avg:.1f}pts)")
            score += 20
        elif "DIPPING" in trend:
            recent_avg = sum(p.recent_form_5gw[:2]) / 2 if len(p.recent_form_5gw) >= 2 else 0
            flags.append(f"form {trend} (last 2 GW avg: {recent_avg:.1f}pts)")
            score += 10

        # ep_next (non-GKP)
        try:
            ep = float(p.ep_next)
        except (TypeError, ValueError):
            ep = 0.0
        if p.position != "GKP":
            if ep < 3.0:
                flags.append(f"low ep_next ({p.ep_next})")
                score += 15
            elif ep < 4.5:
                score += 5  # below average, soft flag

        # upcoming fixtures
        if p.fixtures_next_3:
            avg_fdr = sum(f.fdr for f in p.fixtures_next_3) / len(p.fixtures_next_3)
            if avg_fdr >= 4.0:
                flags.append(f"very tough fixtures (avg FDR {avg_fdr:.1f})")
                score += 15
            elif avg_fdr >= 3.3:
                flags.append(f"tough fixtures (avg FDR {avg_fdr:.1f})")
                score += 8
            score += avg_fdr  # tiebreaker

        form_str = "→".join(str(x) for x in p.recent_form_5gw) if p.recent_form_5gw else "N/A"
        fix_str = _fixture_summary(p.fixtures_next_3)
        flag_str = ", ".join(flags) if flags else "no acute flags (included as relative weakest)"
        scored.append((score, (
            f"- [{p.position}] {p.web_name} ({p.team_name}) £{p.now_cost}m\n"
            f"  Last 5 GWs: {form_str} | Trend: {trend} | ep_next: {p.ep_next}\n"
            f"  Next 3 fixtures: {fix_str}\n"
            f"  Sell flags: {flag_str}"
        )))

    # sort by urgency, always surface top 4
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:4]
    return "\n".join(entry for _, entry in top) if top else "Squad data unavailable."


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
        trend = _form_trend(form_list)
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


def fetch_player_context(player_names: list[str]) -> str:
    """
    Live web search for external context on sell candidates:
    press conferences, European fixtures, international impacts, rotation risk.
    Uses gpt-4o-search-preview for real-time information.
    """
    if not player_names:
        return ""
    names = ", ".join(player_names[:6])
    query = (
        f"Premier League 2024-25 season — for these players: {names}.\n"
        f"Search for and summarise (be factual, cite only current-season news):\n"
        f"1. Manager press conference quotes about fitness, availability, or rotation risk\n"
        f"2. Upcoming Champions League / Europa League / Conference League fixtures "
        f"in the next 10-14 days and whether these players are expected to start\n"
        f"3. International break call-ups or travel fatigue concerns\n"
        f"4. Any suspension risks (yellow card accumulation)\n"
        f"5. Any other real-world context an FPL manager should know\n"
        f"Keep total response under 300 words. If nothing found for a player, skip them."
    )
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o-search-preview",
            messages=[{"role": "user", "content": query}],
            max_tokens=500,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        # fall back to GPT-4o knowledge if search model unavailable
        try:
            resp = _get_client().chat.completions.create(
                model="gpt-4o",
                temperature=0.1,
                max_tokens=400,
                messages=[{
                    "role": "system",
                    "content": "You are an FPL analyst with up-to-date Premier League knowledge.",
                }, {
                    "role": "user",
                    "content": (
                        f"For these Premier League players: {names}\n"
                        f"Based on your knowledge, briefly note (max 200 words total):\n"
                        f"- Any known European fixture congestion in the near term\n"
                        f"- Rotation risk based on squad depth and manager tendencies\n"
                        f"- Any suspension risk from yellow card accumulation\n"
                        f"- Any known fitness concerns beyond FPL's official data\n"
                        f"Be honest about uncertainty. Skip players you have nothing useful to add."
                    ),
                }],
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            return ""


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
        "Give this manager a honest 'Vibe Check' of their squad. Comment on: "
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


def _format_grounded_targets(grounded_targets: dict[str, list[PlayerSummary]]) -> str:
    """
    Format verified replacement options per sell candidate.
    GPT must pick from these — no invented names allowed.
    """
    if not grounded_targets:
        return ""
    lines = ["VERIFIED TRANSFER TARGETS (FPL API data — pick ONLY from these lists):"]
    for sell_name, targets in grounded_targets.items():
        lines.append(f"\n  If selling {sell_name}:")
        for t in targets:
            fix_str = _fixture_summary(t.fixtures_next_3)
            lines.append(
                f"    • {t.web_name} ({t.team_name}) £{t.now_cost}m | "
                f"ep_next:{t.ep_next} | form:{t.form} | Fixtures: {fix_str}"
            )
    return "\n".join(lines)


def _build_player_index(squad: list[SquadPick], grounded_targets: dict[str, list[PlayerSummary]]) -> dict[str, PlayerSummary]:
    """Name → PlayerSummary index for post-generation validation."""
    index: dict[str, PlayerSummary] = {}
    for pick in squad:
        index[pick.player.web_name.lower()] = pick.player
    for targets in grounded_targets.values():
        for t in targets:
            index[t.web_name.lower()] = t
    return index


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
) -> tuple[str, list[TransferRecommendation]]:

    if grounded_targets is None:
        grounded_targets = {}

    player_index = _build_player_index(squad, grounded_targets)

    # --- pre-compute sell candidates & fetch live context ---
    sell_candidates_str = _sell_candidates(squad)

    # extract names of top sell candidates for external context search
    # always include top 3 from sell candidates (not just injured/bad form)
    sell_names = []
    scored_for_context = []
    for pick in squad:
        if pick.position > 11:
            continue
        p = pick.player
        trend = _form_trend(p.recent_form_5gw)
        is_injured = p.chance_of_playing_next_round is not None and p.chance_of_playing_next_round < 75
        is_bad_form = "DECLINING" in trend or "DIPPING" in trend
        priority = (2 if is_injured else 0) + (1 if is_bad_form else 0)
        scored_for_context.append((priority, p.web_name))
    scored_for_context.sort(key=lambda x: x[0], reverse=True)
    # only fetch external context if there are genuinely flagged players (saves search-preview cost)
    flagged_names = [name for priority, name in scored_for_context if priority > 0]
    sell_names = flagged_names[:4] if flagged_names else []

    external_context = fetch_player_context(sell_names) if sell_names else ""

    system = (
        "You are a precision FPL (Fantasy Premier League) transfer analyst.\n\n"

        "DATA AVAILABLE:\n"
        "- ep_next: FPL's ML model prediction for next GW points\n"
        "- Last 5 GW scores (newest→oldest) with trend label\n"
        "- Next 3 fixture FDRs with avg difficulty rating for both outgoing AND incoming players\n"
        "- Player price, ownership %, PPG\n"
        "- Live external context: press conferences, European fixtures, suspension risk\n\n"

        "TRANSFER RULES — FOLLOW STRICTLY:\n"
        "1. Only sell players listed in SELL CANDIDATES.\n"
        "2. ONLY suggest transfer-in players from the VERIFIED TRANSFER TARGETS list. "
        "Never invent a player name not on the list.\n"
        "3. NEVER suggest a replacement from the SAME CLUB as the player being sold.\n"
        "4. MATCH POSITION SUB-TYPE using football knowledge: selling an RB → target RB/WB, "
        "not a CB. Selling a striker → target striker. Use real positional knowledge within FPL's "
        "GKP/DEF/MID/FWD groupings.\n"
        "5. BUDGET: incoming price ≤ (sell price + ITB). All targets in the list are pre-filtered "
        "as affordable — just pick the best.\n"
        "6. For sell_reasoning: cite last 5 GW scores, form trend, ep_next, AND all 3 upcoming "
        "fixture FDRs. If external context applies, reference it.\n"
        "7. For buy_reasoning: cite form trend, ep_next, all 3 upcoming FDRs, DGW exposure, "
        "ownership differential. Reference external context if relevant.\n"
        "8. For external_context field: include any press conference quotes, European fixture "
        "congestion, suspension risk, or other real-world context relevant to THIS specific transfer. "
        "Leave empty string if nothing applicable.\n"
        "9. Confidence — High: form ↑ + fixtures easy + ep_next strong. Medium: 2 signals. Low: 1.\n\n"

        "FREE TRANSFER RULE:\n"
        "- ALWAYS return 1–3 transfer suggestions regardless of how many free transfers the manager has.\n"
        "- If free_transfers = 0, each suggestion's signals array MUST include '4pt hit required' and "
        "the budget_check MUST end with '(4pt hit)'. The narrative should acknowledge the hit cost "
        "but still recommend the move if the data justifies it.\n"
        "- NEVER use zero free transfers as a reason to return an empty transfers array.\n\n"

        "OUTPUT: Return ONLY valid JSON with keys 'narrative' and 'transfers'.\n"
        "'narrative': 2–3 sentence overview of the squad situation (deadline urgency, "
        "key risks, league context). DO NOT name specific transfer-in targets or give "
        "transfer advice here — that belongs exclusively in the 'transfers' array.\n"
        "'transfers': array of EXACTLY 1–3 objects. This array MUST NOT be empty. "
        "Each object has EXACTLY these keys:\n"
        "  out, out_club, out_price, in, in_club, in_price,\n"
        "  sell_reasoning, buy_reasoning, budget_check, confidence, signals, external_context\n"
        "sell_reasoning / buy_reasoning: 2 sentences each, data-specific.\n"
        "budget_check: 'Sell price £Xm + £Ym ITB = £Zm available. Target £Wm. Net: ±£Vm'\n"
        "signals: 3–5 short strings (the raw data signals).\n"
        "external_context: 1–2 sentences of real-world context, or empty string.\n"
    )

    dgw_str = "\n".join(f"- {p.web_name} ({p.team_name})" for p in dgw_players) or "None"
    bgw_str = "\n".join(f"- {p.web_name} ({p.team_name})" for p in bgw_players) or "None"
    league_str = (
        "\n".join(f"- {l.name}: Rank {l.rank:,} / {l.total_managers:,}" for l in league_standings)
        or "No mini-league data"
    )

    user = (
        f"Deadline: {deadline_str}\n"
        f"Manager: {manager.name} ({manager.team_name}) | Rank: {manager.overall_rank:,}\n\n"

        f"BUDGET:\n"
        f"- In the bank: £{budget.itb}m\n"
        f"- Free transfers available: {budget.free_transfers} "
        f"{'(any further transfer = 4pt hit)' if budget.free_transfers == 0 else '(additional transfers cost 4pts each)'}\n"
        f"- Transfers made this GW: {budget.transfers_made}"
        f"{f' (hit already taken: -{budget.hit_cost}pts)' if budget.hit_cost > 0 else ''}\n\n"

        f"SELL CANDIDATES (starting XI only):\n{sell_candidates_str}\n\n"

        + (_format_grounded_targets(grounded_targets) + "\n\n" if grounded_targets else "")

        + (f"EXTERNAL CONTEXT (press conferences, European fixtures, suspensions):\n{external_context}\n\n"
           if external_context else "") +

        f"FULL SQUAD:\n{format_squad_for_prompt(squad)}\n\n"

        f"INJURY FLAGS:\n{_injury_summary(injury_flags)}\n\n"

        f"DOUBLE GW ASSETS:\n{dgw_str}\n\n"
        f"BLANK GW CONCERNS:\n{bgw_str}\n\n"

        f"MINI-LEAGUE:\n{league_str}\n\n"

        "Produce the JSON response now. The 'transfers' array MUST contain 1–3 fully populated "
        "objects. Pick sell targets from SELL CANDIDATES, match position sub-type, verify budget "
        "arithmetic, reference all 3 fixture FDRs in your reasoning, and incorporate any relevant "
        "external context. Do NOT put transfer player names or advice in 'narrative'."
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
        import sys
        print(f"[LLM DEBUG] raw JSON:\n{raw_content[:2000]}", file=sys.stderr)
        data = json.loads(raw_content)
        narrative = data.get("narrative", _FALLBACK)
        raw_transfers = data.get("transfers", [])
        if not isinstance(raw_transfers, list):
            raw_transfers = []

        def _fmt_price(v) -> str:
            if isinstance(v, (int, float)):
                return f"£{v:.1f}m"
            s = str(v).strip() if v else ""
            if s and not s.startswith("£"):
                s = f"£{s}"
            return s

        def _ground(name: str, field_club: str, field_price: str) -> tuple[str, str]:
            """Look up verified club and price from our API data."""
            p = player_index.get(name.lower())
            if p:
                return p.team_name, f"£{p.now_cost}m"
            return field_club, field_price

        transfers: list[TransferRecommendation] = []
        for t in raw_transfers[:3]:
            if not isinstance(t, dict):
                continue
            try:
                out_name = str(t.get("out", ""))
                in_name  = str(t.get("in", ""))

                # Validate / overwrite with API truth
                out_club, out_price = _ground(out_name, str(t.get("out_club", "")), _fmt_price(t.get("out_price", "")))
                in_club,  in_price  = _ground(in_name,  str(t.get("in_club", "")),  _fmt_price(t.get("in_price", "")))

                transfers.append(TransferRecommendation(
                    out=out_name,
                    out_club=out_club,
                    out_price=out_price,
                    in_=in_name,
                    in_club=in_club,
                    in_price=in_price,
                    sell_reasoning=str(t.get("sell_reasoning", "")),
                    buy_reasoning=str(t.get("buy_reasoning", "")),
                    budget_check=str(t.get("budget_check", "")),
                    confidence=str(t.get("confidence", "Medium")),
                    signals=[str(s) for s in t.get("signals", [])],
                    external_context=str(t.get("external_context", "")),
                ))
            except Exception as e:
                import sys
                print(f"[LLM DEBUG] transfer parse error: {e} | raw: {t}", file=sys.stderr)
                continue

        return narrative, transfers
    except (APIError, RateLimitError, json.JSONDecodeError, KeyError):
        return _FALLBACK, []
