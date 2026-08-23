from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any

import libsql_client

DAILY_BRIEF_LIMIT = 5

# Local fallback (dev only) when TURSO_DATABASE_URL isn't set. On Cloud Run
# this path is NOT persistent across invocations — production always sets
# TURSO_DATABASE_URL. See docs/architecture.md "Known architectural gap".
DB_PATH = Path("data/fpl_intel.db")

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS managers (
        id          INTEGER PRIMARY KEY,
        added_at    TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brief_cache (
        manager_id  INTEGER NOT NULL,
        gameweek    INTEGER NOT NULL,
        data        TEXT    NOT NULL,
        created_at  TEXT    DEFAULT (datetime('now')),
        PRIMARY KEY (manager_id, gameweek)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_state (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        updated_at  TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rate_limits (
        manager_id  INTEGER NOT NULL,
        date        TEXT    NOT NULL,
        count       INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (manager_id, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transfer_suggestions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        manager_id  INTEGER NOT NULL,
        gameweek    INTEGER NOT NULL,
        out_id      INTEGER,
        out_name    TEXT    NOT NULL,
        in_id       INTEGER,
        in_name     TEXT    NOT NULL,
        created_at  TEXT    DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transfer_outcomes (
        suggestion_id  INTEGER PRIMARY KEY,
        implemented    INTEGER NOT NULL,
        out_points     INTEGER,
        in_points      INTEGER,
        delta          INTEGER,
        evaluated_at   TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_decisions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        manager_id     INTEGER NOT NULL,
        gameweek       INTEGER NOT NULL,
        decision_type  TEXT    NOT NULL,
        proposal_json  TEXT    NOT NULL,
        confidence     TEXT    NOT NULL,
        status         TEXT    NOT NULL DEFAULT 'pending_approval',
        created_at     TEXT    DEFAULT (datetime('now')),
        approved_at    TEXT,
        executed_at    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_conversations (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id   INTEGER NOT NULL,
        gameweek      INTEGER NOT NULL,
        round         INTEGER NOT NULL,
        agent_name    TEXT    NOT NULL,
        message       TEXT    NOT NULL,
        created_at    TEXT    DEFAULT (datetime('now')),
        FOREIGN KEY (decision_id) REFERENCES agent_decisions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_candidates (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id       INTEGER NOT NULL,
        player_id         INTEGER NOT NULL,
        player_name       TEXT    NOT NULL,
        role              TEXT    NOT NULL,
        source_agent      TEXT    NOT NULL,
        predicted_points  REAL    NOT NULL,
        actual_points     REAL,
        evaluated_at      TEXT,
        FOREIGN KEY (decision_id) REFERENCES agent_decisions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_calibration (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_name   TEXT    NOT NULL,
        decision_id    INTEGER NOT NULL,
        stance         TEXT    NOT NULL,
        was_correct    INTEGER,
        evaluated_at   TEXT,
        FOREIGN KEY (decision_id) REFERENCES agent_decisions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pipeline_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id        TEXT    NOT NULL,
        gameweek      INTEGER,
        manager_id    INTEGER,
        decision_id   INTEGER,
        stage         TEXT    NOT NULL,
        status        TEXT    NOT NULL,   -- 'started' | 'succeeded' | 'failed'
        detail_json   TEXT,
        duration_ms   INTEGER,
        tokens_in     INTEGER,
        tokens_out    INTEGER,
        cost_usd      REAL,
        created_at    TEXT    DEFAULT (datetime('now'))
    )
    """,
]


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _get_client().batch(_SCHEMA_STATEMENTS)


# ── Connection ────────────────────────────────────────────────────────────────
# libSQL/Turso is SQLite-wire-compatible — same SQL, same `?` placeholders,
# same Row.__getitem__ access by name. Falls back to a local file (dev only;
# NOT persistent on Cloud Run) when TURSO_DATABASE_URL isn't set, mirroring
# the fallback-friendly pattern already used in app/ml/model.py.

_client: libsql_client.ClientSync | None = None


def _get_client() -> libsql_client.ClientSync:
    global _client
    if _client is None:
        url = os.environ.get("TURSO_DATABASE_URL")
        if url:
            _client = libsql_client.create_client_sync(
                url=url, auth_token=os.environ.get("TURSO_AUTH_TOKEN")
            )
        else:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _client = libsql_client.create_client_sync(url=f"file:{DB_PATH}")
    return _client


def _execute(sql: str, params: tuple | list = ()) -> libsql_client.ResultSet:
    return _get_client().execute(sql, params)


def _rows(rs: libsql_client.ResultSet) -> list[dict]:
    return [r.asdict() for r in rs.rows]


def _row(rs: libsql_client.ResultSet) -> dict | None:
    return rs.rows[0].asdict() if rs.rows else None


def close() -> None:
    """
    ClientSync runs a background thread that otherwise keeps the process
    alive forever — harmless (even desired) for the long-lived FastAPI app,
    but a one-shot CLI script (app/agents/pipeline.py --dry-run etc.) must
    call this before exiting or it will hang indefinitely.
    """
    global _client
    if _client is not None:
        _client.close()
        _client = None


# ── Managers ──────────────────────────────────────────────────────────────────

def get_managers() -> list[int]:
    rs = _execute("SELECT id FROM managers ORDER BY added_at")
    return [r["id"] for r in _rows(rs)]


def add_manager(manager_id: int) -> None:
    _execute("INSERT OR IGNORE INTO managers (id) VALUES (?)", (manager_id,))


# ── Brief cache ───────────────────────────────────────────────────────────────
# TTL: 2 hours — briefs don't change within a GW once generated

BRIEF_TTL_MINUTES = 120


def get_brief_cache(manager_id: int, gameweek: int) -> dict | None:
    rs = _execute(
        """
        SELECT data FROM brief_cache
        WHERE manager_id = ? AND gameweek = ?
          AND created_at > datetime('now', ? )
        """,
        (manager_id, gameweek, f"-{BRIEF_TTL_MINUTES} minutes"),
    )
    row = _row(rs)
    return json.loads(row["data"]) if row else None


def set_brief_cache(manager_id: int, gameweek: int, data: dict) -> None:
    _execute(
        """
        INSERT INTO brief_cache (manager_id, gameweek, data)
        VALUES (?, ?, ?)
        ON CONFLICT(manager_id, gameweek)
        DO UPDATE SET data = excluded.data, created_at = datetime('now')
        """,
        (manager_id, gameweek, json.dumps(data)),
    )


def invalidate_brief_cache(manager_id: int, gameweek: int) -> None:
    _execute(
        "DELETE FROM brief_cache WHERE manager_id = ? AND gameweek = ?",
        (manager_id, gameweek),
    )


# ── Bot state (replaces fpl_state.json) ──────────────────────────────────────

def get_bot_state(key: str, default: Any = None) -> Any:
    rs = _execute("SELECT value FROM bot_state WHERE key = ?", (key,))
    row = _row(rs)
    if row is None:
        return default
    return json.loads(row["value"])


def set_bot_state(key: str, value: Any) -> None:
    _execute(
        """
        INSERT INTO bot_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value, updated_at = datetime('now')
        """,
        (key, json.dumps(value)),
    )


def get_full_bot_state() -> dict:
    rs = _execute("SELECT key, value FROM bot_state")
    return {r["key"]: json.loads(r["value"]) for r in _rows(rs)}


def set_full_bot_state(state: dict) -> None:
    for key, value in state.items():
        set_bot_state(key, value)


# ── Rate limiting ─────────────────────────────────────────────────────────────

def get_rate_limit_count(manager_id: int) -> int:
    today = datetime.date.today().isoformat()
    rs = _execute(
        "SELECT count FROM rate_limits WHERE manager_id = ? AND date = ?",
        (manager_id, today),
    )
    row = _row(rs)
    return row["count"] if row else 0


# ── Transfer tracking ─────────────────────────────────────────────────────────

def save_transfer_suggestions(manager_id: int, gameweek: int, suggestions: list[dict]) -> None:
    """Idempotent — skips if suggestions already saved for this GW."""
    existing = _row(_execute(
        "SELECT id FROM transfer_suggestions WHERE manager_id = ? AND gameweek = ? LIMIT 1",
        (manager_id, gameweek),
    ))
    if existing:
        return
    for s in suggestions:
        _execute(
            "INSERT INTO transfer_suggestions (manager_id, gameweek, out_id, out_name, in_id, in_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (manager_id, gameweek, s.get("out_id"), s["out_name"], s.get("in_id"), s["in_name"]),
        )


def get_unevaluated_suggestions(manager_id: int, before_gw: int) -> list[dict]:
    """Return suggestions from past GWs that haven't been evaluated yet."""
    rs = _execute(
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
    )
    return _rows(rs)


def save_transfer_outcome(
    suggestion_id: int,
    implemented: bool,
    out_points: int | None,
    in_points: int | None,
) -> None:
    delta = (in_points - out_points) if (out_points is not None and in_points is not None) else None
    _execute(
        "INSERT OR IGNORE INTO transfer_outcomes (suggestion_id, implemented, out_points, in_points, delta) "
        "VALUES (?, ?, ?, ?, ?)",
        (suggestion_id, int(implemented), out_points, in_points, delta),
    )


def get_recent_outcomes(manager_id: int, limit: int = 5) -> list[dict]:
    rs = _execute(
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
    )
    return _rows(rs)


def increment_rate_limit(manager_id: int) -> int:
    """Increment today's count and return the new value."""
    today = datetime.date.today().isoformat()
    _execute(
        """
        INSERT INTO rate_limits (manager_id, date, count)
        VALUES (?, ?, 1)
        ON CONFLICT(manager_id, date)
        DO UPDATE SET count = count + 1
        """,
        (manager_id, today),
    )
    row = _row(_execute(
        "SELECT count FROM rate_limits WHERE manager_id = ? AND date = ?",
        (manager_id, today),
    ))
    return row["count"]


# ── Agent decisions (transfers, captain, lineup) ────────────────────────────

def create_agent_decision(
    manager_id: int,
    gameweek: int,
    decision_type: str,
    proposal: dict,
    confidence: str,
) -> int:
    """Insert a decision in 'pending_approval' state, return its id."""
    rs = _execute(
        """
        INSERT INTO agent_decisions (manager_id, gameweek, decision_type, proposal_json, confidence, status)
        VALUES (?, ?, ?, ?, ?, 'pending_approval')
        """,
        (manager_id, gameweek, decision_type, json.dumps(proposal), confidence),
    )
    return rs.last_insert_rowid


def set_decision_status(decision_id: int, status: str) -> None:
    """status: 'pending_approval' | 'approved' | 'rejected' | 'executed' | 'failed'."""
    ts_col = {"approved": "approved_at", "executed": "executed_at"}.get(status)
    if ts_col:
        _execute(
            f"UPDATE agent_decisions SET status = ?, {ts_col} = datetime('now') WHERE id = ?",
            (status, decision_id),
        )
    else:
        _execute("UPDATE agent_decisions SET status = ? WHERE id = ?", (status, decision_id))


def get_agent_decision(decision_id: int) -> dict | None:
    row = _row(_execute("SELECT * FROM agent_decisions WHERE id = ?", (decision_id,)))
    if not row:
        return None
    row["proposal"] = json.loads(row["proposal_json"])
    return row


def get_decisions(manager_id: int, gameweek: int | None = None, decision_type: str | None = None) -> list[dict]:
    query = "SELECT * FROM agent_decisions WHERE manager_id = ?"
    params: list = [manager_id]
    if gameweek is not None:
        query += " AND gameweek = ?"
        params.append(gameweek)
    if decision_type is not None:
        query += " AND decision_type = ?"
        params.append(decision_type)
    query += " ORDER BY gameweek DESC, created_at DESC"
    out = []
    for d in _rows(_execute(query, params)):
        d["proposal"] = json.loads(d["proposal_json"])
        out.append(d)
    return out


def update_decision_proposal(decision_id: int, proposal: dict, confidence: str) -> None:
    _execute(
        "UPDATE agent_decisions SET proposal_json = ?, confidence = ? WHERE id = ?",
        (json.dumps(proposal), confidence, decision_id),
    )


def get_pending_decisions(manager_id: int) -> list[dict]:
    out = []
    rs = _execute(
        "SELECT * FROM agent_decisions WHERE manager_id = ? AND status = 'pending_approval' "
        "ORDER BY created_at DESC",
        (manager_id,),
    )
    for d in _rows(rs):
        d["proposal"] = json.loads(d["proposal_json"])
        out.append(d)
    return out


# ── Agent conversations (debate transcript) ─────────────────────────────────

def log_agent_message(decision_id: int, gameweek: int, round_: int, agent_name: str, message: str) -> None:
    """Write a single debate turn immediately — crash-safe transcript logging."""
    _execute(
        "INSERT INTO agent_conversations (decision_id, gameweek, round, agent_name, message) "
        "VALUES (?, ?, ?, ?, ?)",
        (decision_id, gameweek, round_, agent_name, message),
    )


def get_conversation(decision_id: int) -> list[dict]:
    rs = _execute(
        "SELECT * FROM agent_conversations WHERE decision_id = ? ORDER BY round, id",
        (decision_id,),
    )
    return _rows(rs)


# ── Decision candidates (feedback loop — every candidate, not just the winner) ──

def save_decision_candidates(decision_id: int, candidates: list[dict]) -> None:
    """Each candidate: {player_id, player_name, role, source_agent, predicted_points}."""
    for c in candidates:
        _execute(
            """
            INSERT INTO decision_candidates
                (decision_id, player_id, player_name, role, source_agent, predicted_points)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (decision_id, c["player_id"], c["player_name"], c["role"],
             c["source_agent"], c["predicted_points"]),
        )


def get_unevaluated_candidates(before_gw: int) -> list[dict]:
    rs = _execute(
        """
        SELECT dc.*, ad.gameweek, ad.manager_id
        FROM decision_candidates dc
        JOIN agent_decisions ad ON ad.id = dc.decision_id
        WHERE ad.gameweek < ? AND dc.actual_points IS NULL
        """,
        (before_gw,),
    )
    return _rows(rs)


def save_candidate_outcome(candidate_id: int, actual_points: float) -> None:
    _execute(
        "UPDATE decision_candidates SET actual_points = ?, evaluated_at = datetime('now') WHERE id = ?",
        (actual_points, candidate_id),
    )


def get_candidates_for_decision(decision_id: int) -> list[dict]:
    rs = _execute("SELECT * FROM decision_candidates WHERE decision_id = ?", (decision_id,))
    return _rows(rs)


# ── Persona calibration ──────────────────────────────────────────────────────

def save_persona_stances(decision_id: int, stances: list[dict]) -> None:
    """Each stance: {persona_name, stance} — 'favored' or 'opposed' the final pick."""
    for s in stances:
        _execute(
            "INSERT INTO persona_calibration (persona_name, decision_id, stance) VALUES (?, ?, ?)",
            (s["persona_name"], decision_id, s["stance"]),
        )


def mark_persona_outcomes(decision_id: int, was_correct_by_persona: dict[str, bool]) -> None:
    for persona_name, correct in was_correct_by_persona.items():
        _execute(
            """
            UPDATE persona_calibration
            SET was_correct = ?, evaluated_at = datetime('now')
            WHERE decision_id = ? AND persona_name = ?
            """,
            (int(correct), decision_id, persona_name),
        )


def get_stances_for_decision(decision_id: int) -> list[dict]:
    rs = _execute("SELECT * FROM persona_calibration WHERE decision_id = ?", (decision_id,))
    return _rows(rs)


# ── Pipeline observability (structured step-by-step run log) ────────────────

def log_pipeline_step(
    run_id: str,
    stage: str,
    status: str,
    detail: dict | None = None,
    gameweek: int | None = None,
    manager_id: int | None = None,
    decision_id: int | None = None,
    duration_ms: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """status: 'started' | 'succeeded' | 'failed'. `detail` is arbitrary
    structured JSON — ML predictions, an agent's full structured output,
    an error + traceback, etc. tokens_in/tokens_out/cost_usd are set for
    LLM-calling stages only (see app/pricing.py)."""
    _execute(
        """
        INSERT INTO pipeline_log
            (run_id, gameweek, manager_id, decision_id, stage, status, detail_json, duration_ms,
             tokens_in, tokens_out, cost_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, gameweek, manager_id, decision_id, stage, status,
         json.dumps(detail) if detail is not None else None, duration_ms,
         tokens_in, tokens_out, cost_usd),
    )


def get_run_log(run_id: str) -> list[dict]:
    out = []
    for r in _rows(_execute(
        "SELECT * FROM pipeline_log WHERE run_id = ? ORDER BY id", (run_id,)
    )):
        r["detail"] = json.loads(r["detail_json"]) if r["detail_json"] else None
        out.append(r)
    return out


def get_recent_runs(limit: int = 20) -> list[dict]:
    """One row per distinct run_id: first-seen time, step count, whether any
    step failed, and total LLM token usage/cost across the run."""
    rs = _execute(
        """
        SELECT run_id,
               MIN(created_at) AS started_at,
               MAX(created_at) AS last_seen_at,
               COUNT(*) AS step_count,
               SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
               MAX(gameweek) AS gameweek,
               MAX(manager_id) AS manager_id,
               SUM(tokens_in) AS total_tokens_in,
               SUM(tokens_out) AS total_tokens_out,
               SUM(cost_usd) AS total_cost_usd
        FROM pipeline_log
        GROUP BY run_id
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return _rows(rs)


def get_persona_accuracy(persona_name: str, limit: int = 20) -> dict:
    """Rolling accuracy over the last `limit` evaluated decisions for a persona."""
    rs = _execute(
        """
        SELECT was_correct FROM persona_calibration
        WHERE persona_name = ? AND was_correct IS NOT NULL
        ORDER BY evaluated_at DESC LIMIT ?
        """,
        (persona_name, limit),
    )
    rows = _rows(rs)
    if not rows:
        return {"accuracy": None, "sample_size": 0}
    correct = sum(r["was_correct"] for r in rows)
    return {"accuracy": correct / len(rows), "sample_size": len(rows)}
