from __future__ import annotations

import datetime
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DAILY_BRIEF_LIMIT = 5

DB_PATH = Path("data/fpl_intel.db")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS managers (
                id          INTEGER PRIMARY KEY,
                added_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS brief_cache (
                manager_id  INTEGER NOT NULL,
                gameweek    INTEGER NOT NULL,
                data        TEXT    NOT NULL,
                created_at  TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (manager_id, gameweek)
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS rate_limits (
                manager_id  INTEGER NOT NULL,
                date        TEXT    NOT NULL,
                count       INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (manager_id, date)
            );

            CREATE TABLE IF NOT EXISTS transfer_suggestions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_id  INTEGER NOT NULL,
                gameweek    INTEGER NOT NULL,
                out_id      INTEGER,
                out_name    TEXT    NOT NULL,
                in_id       INTEGER,
                in_name     TEXT    NOT NULL,
                created_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS transfer_outcomes (
                suggestion_id  INTEGER PRIMARY KEY,
                implemented    INTEGER NOT NULL,
                out_points     INTEGER,
                in_points      INTEGER,
                delta          INTEGER,
                evaluated_at   TEXT DEFAULT (datetime('now'))
            );
        """)


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Managers ──────────────────────────────────────────────────────────────────

def get_managers() -> list[int]:
    with _conn() as conn:
        rows = conn.execute("SELECT id FROM managers ORDER BY added_at").fetchall()
    return [r["id"] for r in rows]


def add_manager(manager_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO managers (id) VALUES (?)",
            (manager_id,),
        )


# ── Brief cache ───────────────────────────────────────────────────────────────
# TTL: 2 hours — briefs don't change within a GW once generated

BRIEF_TTL_MINUTES = 120


def get_brief_cache(manager_id: int, gameweek: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT data FROM brief_cache
            WHERE manager_id = ? AND gameweek = ?
              AND created_at > datetime('now', ? )
            """,
            (manager_id, gameweek, f"-{BRIEF_TTL_MINUTES} minutes"),
        ).fetchone()
    return json.loads(row["data"]) if row else None


def set_brief_cache(manager_id: int, gameweek: int, data: dict) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO brief_cache (manager_id, gameweek, data)
            VALUES (?, ?, ?)
            ON CONFLICT(manager_id, gameweek)
            DO UPDATE SET data = excluded.data, created_at = datetime('now')
            """,
            (manager_id, gameweek, json.dumps(data)),
        )


def invalidate_brief_cache(manager_id: int, gameweek: int) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM brief_cache WHERE manager_id = ? AND gameweek = ?",
            (manager_id, gameweek),
        )


# ── Bot state (replaces fpl_state.json) ──────────────────────────────────────

def get_bot_state(key: str, default: Any = None) -> Any:
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def set_bot_state(key: str, value: Any) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO bot_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value, updated_at = datetime('now')
            """,
            (key, json.dumps(value)),
        )


def get_full_bot_state() -> dict:
    with _conn() as conn:
        rows = conn.execute("SELECT key, value FROM bot_state").fetchall()
    return {r["key"]: json.loads(r["value"]) for r in rows}


def set_full_bot_state(state: dict) -> None:
    for key, value in state.items():
        set_bot_state(key, value)


# ── Rate limiting ─────────────────────────────────────────────────────────────

def get_rate_limit_count(manager_id: int) -> int:
    today = datetime.date.today().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT count FROM rate_limits WHERE manager_id = ? AND date = ?",
            (manager_id, today),
        ).fetchone()
    return row["count"] if row else 0


# ── Transfer tracking ─────────────────────────────────────────────────────────

def save_transfer_suggestions(manager_id: int, gameweek: int, suggestions: list[dict]) -> None:
    """Idempotent — skips if suggestions already saved for this GW."""
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM transfer_suggestions WHERE manager_id = ? AND gameweek = ? LIMIT 1",
            (manager_id, gameweek),
        ).fetchone()
        if existing:
            return
        for s in suggestions:
            conn.execute(
                "INSERT INTO transfer_suggestions (manager_id, gameweek, out_id, out_name, in_id, in_name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (manager_id, gameweek, s.get("out_id"), s["out_name"], s.get("in_id"), s["in_name"]),
            )


def get_unevaluated_suggestions(manager_id: int, before_gw: int) -> list[dict]:
    """Return suggestions from past GWs that haven't been evaluated yet."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT ts.id, ts.gameweek, ts.out_id, ts.out_name, ts.in_id, ts.in_name
            FROM transfer_suggestions ts
            LEFT JOIN transfer_outcomes to_ ON ts.id = to_.suggestion_id
            WHERE ts.manager_id = ?
              AND ts.gameweek < ?
              AND ts.out_id IS NOT NULL
              AND ts.in_id  IS NOT NULL
              AND to_.suggestion_id IS NULL
            ORDER BY ts.gameweek DESC
            """,
            (manager_id, before_gw),
        ).fetchall()
    return [dict(r) for r in rows]


def save_transfer_outcome(
    suggestion_id: int,
    implemented: bool,
    out_points: int | None,
    in_points: int | None,
) -> None:
    delta = (in_points - out_points) if (out_points is not None and in_points is not None) else None
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO transfer_outcomes (suggestion_id, implemented, out_points, in_points, delta) "
            "VALUES (?, ?, ?, ?, ?)",
            (suggestion_id, int(implemented), out_points, in_points, delta),
        )


def get_recent_outcomes(manager_id: int, limit: int = 5) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT ts.gameweek, ts.out_name, ts.in_name,
                   to_.implemented, to_.out_points, to_.in_points, to_.delta
            FROM transfer_suggestions ts
            JOIN transfer_outcomes to_ ON ts.id = to_.suggestion_id
            WHERE ts.manager_id = ?
            ORDER BY ts.gameweek DESC
            LIMIT ?
            """,
            (manager_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def increment_rate_limit(manager_id: int) -> int:
    """Increment today's count and return the new value."""
    today = datetime.date.today().isoformat()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO rate_limits (manager_id, date, count)
            VALUES (?, ?, 1)
            ON CONFLICT(manager_id, date)
            DO UPDATE SET count = count + 1
            """,
            (manager_id, today),
        )
        row = conn.execute(
            "SELECT count FROM rate_limits WHERE manager_id = ? AND date = ?",
            (manager_id, today),
        ).fetchone()
    return row["count"]
