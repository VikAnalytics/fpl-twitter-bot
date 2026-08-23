"""
OpenAI per-token pricing, kept as an explicit lookup table (not hardcoded
inline at every call site) so it's one place to update when prices change.

Source: verified via live web search against OpenAI's current published
pricing (not relied on from memory, which could be stale) — see
docs/progress.md for the date this was last checked.

gpt-4o-mini: $0.15 / 1M input tokens (cache-miss), $0.60 / 1M output tokens.
Cached input tokens are cheaper ($0.075/1M) but usage-metadata from
LangChain doesn't reliably distinguish cache hits, so cost estimates here
are a conservative upper bound (always priced as cache-miss).

gpt-4.1-mini (used for the web_search tool via the Responses API — the old
gpt-4o-*-search-preview chat-completion models were deprecated July 2026,
confirmed live via a 404 rather than assumed): $0.40/1M input, $1.60/1M
output, PLUS a flat $25/1,000 calls ($0.025/call) tool fee for the search
itself (non-reasoning-model tier) — verified separately, since search-tool
calls are billed differently from plain chat completions, and the ~8,000
input tokens they return as "search content" showed up exactly as expected
in a real test call. This is meaningfully more expensive per-call than the
debate's gpt-4o-mini nodes (cents vs fractions of a cent), which is why it's
called once per debate (all squad + target names batched into one search),
not once per player.
"""
from __future__ import annotations

# $ per token (not per million) — kept as the per-token rate so callers just
# multiply by a token count with no division at the call site.
_PRICING_PER_TOKEN = {
    "gpt-4o-mini": {"input": 0.15 / 1_000_000, "output": 0.60 / 1_000_000},
    "gpt-4.1-mini": {"input": 0.40 / 1_000_000, "output": 1.60 / 1_000_000},
}

_FLAT_FEE_PER_CALL = {
    "gpt-4.1-mini": 0.025,  # only applies when called with the web_search tool attached
}


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float | None:
    rates = _PRICING_PER_TOKEN.get(model)
    if rates is None:
        return None
    cost = tokens_in * rates["input"] + tokens_out * rates["output"] + _FLAT_FEE_PER_CALL.get(model, 0.0)
    return round(cost, 6)
