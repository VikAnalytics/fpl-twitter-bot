from __future__ import annotations

import time
from typing import Any

# ── In-memory TTL cache ───────────────────────────────────────────────────────
# Keyed by arbitrary string, stores (value, expires_at) tuples.
# Not thread-safe for writes but fine for single-process FastAPI + uvicorn workers.

_store: dict[str, tuple[Any, float]] = {}


def get(key: str) -> Any | None:
    entry = _store.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if time.monotonic() > expires_at:
        del _store[key]
        return None
    return value


def set(key: str, value: Any, ttl_seconds: int) -> None:
    _store[key] = (value, time.monotonic() + ttl_seconds)


def delete(key: str) -> None:
    _store.pop(key, None)


def clear() -> None:
    _store.clear()


# ── Bootstrap cache (shared across all requests) ──────────────────────────────
# FPL bootstrap is ~8MB and changes at most once per hour (on GW deadline).
# Caching for 5 minutes eliminates ~95% of bootstrap fetches under load.

BOOTSTRAP_TTL = 300   # 5 minutes
FIXTURES_TTL  = 300   # 5 minutes


def get_bootstrap() -> dict | None:
    return get("bootstrap")


def set_bootstrap(data: dict) -> None:
    set("bootstrap", data, BOOTSTRAP_TTL)


def get_fixtures() -> list | None:
    return get("fixtures")


def set_fixtures(data: list) -> None:
    set("fixtures", data, FIXTURES_TTL)
