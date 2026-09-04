from __future__ import annotations

import os
import sys
from openai import OpenAI

from . import ranking
from .models import Fixture, PlayerSummary

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    return _client


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
    for r in sell_reports:
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
            + (f"\n  FPL news: {p.news}" if p.news else "")
        )
    return "\n".join(lines)


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
                + (f"\n      FPL news: {t.news}" if t.news else "")
            )
    return "\n".join(lines)


# ─────────────────────── Web search (opt-in, real cost per call) ───────────
#
# The old gpt-4o-search-preview / gpt-4o-mini-search-preview chat-completion
# models were deprecated (July 2026) — confirmed live (a real call returned
# HTTP 404 model_not_found) rather than assumed. Current mechanism is the
# Responses API with a `web_search` tool attached to a regular model —
# verified with a real call before wiring this in (a live query about a
# player correctly returned an answer dated "August 23, 2026").

_SEARCH_MODEL = "gpt-4.1-mini"  # see app/pricing.py for the (higher, since
# this bills a flat ~$0.025/call tool fee + a search-content token block on
# top of normal generation) cost this carries vs. the debate's gpt-4o-mini.


def fetch_player_context(
    player_names: list[str],
    enabled: bool = False,
    return_usage: bool = False,
) -> str | tuple[str, dict]:
    """
    Real web search (press conferences, news outlet coverage, training-ground
    reports — e.g. "not seen training with the squad") for the given
    players, ONE call covering all of them (not one call per player — this
    tool bills a flat per-call fee, so batching matters). Gated behind
    `enabled` — default OFF for callers that don't want the cost/latency.

    return_usage=True returns (text, {"tokens_in":, "tokens_out":}) instead
    of just the text, for cost tracking (see app/agents/pipeline.py).
    """
    empty = ("", {}) if return_usage else ""
    if not enabled or not player_names:
        return empty

    names = ", ".join(sorted(set(player_names))[:40])
    query = (
        f"Premier League current season — for these players: {names}.\n"
        "Search for and summarise (factual, current-season only, under 500 words):\n"
        "1. Reports that a player has been left out of training, not seen training with "
        "the squad, or training away from the main group.\n"
        "2. Manager press conference quotes on fitness/availability/rotation.\n"
        "3. Upcoming European fixtures in the next 10-14 days and expected starters.\n"
        "4. International call-up fatigue concerns.\n"
        "5. Yellow-card suspension risk.\n"
        "Skip players with nothing noteworthy — don't pad with 'no news' filler."
    )
    try:
        resp = _get_client().responses.create(
            model=_SEARCH_MODEL,
            tools=[{"type": "web_search", "search_context_size": "low"}],
            input=query,
        )
        text = (resp.output_text or "").strip()
        if not return_usage:
            return text
        usage = resp.usage
        return text, {
            "tokens_in": getattr(usage, "input_tokens", None),
            "tokens_out": getattr(usage, "output_tokens", None),
        }
    except Exception:
        return empty


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


