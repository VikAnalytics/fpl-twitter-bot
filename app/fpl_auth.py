"""
FPL migrated authentication to PingOne (OAuth2/OIDC) — the old
`users.premierleague.com` email+password login no longer even resolves in
DNS (confirmed independently, and cross-referenced against
github.com/MoayadAbbara/FPL-MCP's live-tested research, which mapped the
whole replacement flow).

The new mechanism: every FPL API call — including writes (transfers,
captain, lineup, chips) — needs only one header,
`x-api-authorization: Bearer <access_token>`. No cookies, no CSRF.

Getting that access token requires a real human login (PingOne's own
bot/fraud detection guards the login page itself, and FPL's ToS prohibits
automating login on your behalf anyway) — see
scripts/fpl_capture_refresh_token.py, which opens a real browser for you to
log into yourself, then reads FPL's own OIDC session (including a
refresh_token — the login's scope includes `offline_access`) out of
localStorage. This module only ever *refreshes* that one-time-obtained
token — it never touches your password.

Access tokens last ~8 hours. Refresh tokens are rotated by PingOne on every
use, so every refresh call persists whichever refresh_token comes back
(overwriting the previous one) — reusing a stale refresh_token fails.
"""
from __future__ import annotations

import time

import requests

from . import database as db

OIDC_TOKEN_URL = "https://account.premierleague.com/as/token"
OIDC_CLIENT_ID = "bfcbaf69-aade-4c1b-8f00-c1cb8a193030"
OIDC_SCOPE = "openid profile email offline_access"
FPL_BASE = "https://fantasy.premierleague.com/api"

_ACCESS_TOKEN_SAFETY_MARGIN_SECONDS = 60


class FplAuthError(Exception):
    pass


def _refresh_access_token(retries: int = 3, backoff_seconds: int = 10) -> str:
    refresh_token = db.get_bot_state("fpl_refresh_token")
    if not refresh_token:
        raise FplAuthError(
            "no fpl_refresh_token in bot_state — run "
            "scripts/fpl_capture_refresh_token.py to set one up"
        )

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                OIDC_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": OIDC_CLIENT_ID,
                    "scope": OIDC_SCOPE,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            access_token = data["access_token"]
            expires_in = data.get("expires_in", 28800)

            db.set_bot_state("fpl_refresh_token", data.get("refresh_token", refresh_token))
            db.set_bot_state("fpl_access_token", access_token)
            db.set_bot_state("fpl_access_token_expires_at", time.time() + expires_in)
            return access_token
        except (requests.RequestException, KeyError, ValueError) as e:
            last_error = e
            if attempt < retries:
                time.sleep(backoff_seconds)
    raise FplAuthError(f"refresh token exchange failed after {retries} attempts: {last_error}")


def get_access_token() -> str:
    """
    Reuses the cached access token (stored in Turso, not just memory — each
    Cloud Run invocation is a fresh container) if it's not close to expiring,
    refreshes otherwise.
    """
    cached_token = db.get_bot_state("fpl_access_token")
    expires_at = db.get_bot_state("fpl_access_token_expires_at")
    if cached_token and expires_at and time.time() < (expires_at - _ACCESS_TOKEN_SAFETY_MARGIN_SECONDS):
        return cached_token
    return _refresh_access_token()


def _headers(access_token: str) -> dict:
    return {
        "x-api-authorization": f"Bearer {access_token}",
        "Origin": "https://fantasy.premierleague.com",
        "Referer": "https://fantasy.premierleague.com/",
    }


def fetch_my_team(access_token: str, manager_id: int) -> dict:
    """Authenticated equivalent of /entry/{id}/picks/ — includes selling_price
    per pick, which the public endpoint doesn't expose and which transfers require."""
    resp = requests.get(f"{FPL_BASE}/my-team/{manager_id}/", headers=_headers(access_token), timeout=20)
    resp.raise_for_status()
    return resp.json()


def submit_transfers(
    access_token: str,
    manager_id: int,
    gameweek: int,
    transfers: list[dict],  # [{"element_in": id, "element_out": id, "purchase_price": int, "selling_price": int}]
    retries: int = 3,
    backoff_seconds: int = 15,
) -> dict:
    payload = {"chip": None, "entry": manager_id, "event": gameweek, "transfers": transfers}
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                f"{FPL_BASE}/transfers/", json=payload, headers=_headers(access_token), timeout=20
            )
            resp.raise_for_status()
            return {"ok": True, "response": resp.json() if resp.content else {}}
        except requests.RequestException as e:
            last_error = e
            if attempt < retries:
                time.sleep(backoff_seconds)
    return {"ok": False, "error": str(last_error)}


def set_lineup(
    access_token: str,
    manager_id: int,
    picks: list[dict],  # [{"element": id, "position": int, "is_captain": bool, "is_vice_captain": bool}]
    chip: str | None = None,
    retries: int = 3,
    backoff_seconds: int = 15,
) -> dict:
    payload = {"chip": chip, "picks": picks}
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                f"{FPL_BASE}/my-team/{manager_id}/", json=payload, headers=_headers(access_token), timeout=20
            )
            resp.raise_for_status()
            return {"ok": True, "response": resp.json() if resp.content else {}}
        except requests.RequestException as e:
            last_error = e
            if attempt < retries:
                time.sleep(backoff_seconds)
    return {"ok": False, "error": str(last_error)}
