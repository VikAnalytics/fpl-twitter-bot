"""
FPL has no official transfer API. This wraps the same unofficial login +
transfer/lineup flow the wider FPL bot community reverse-engineered from the
website. Only ever invoked from the approval-gate handler (app/main.py's
/api/decisions/{id}/approve) — never from the debate/proposal path — so a bug
in the debate engine can never submit anything without the human tap.

Env vars: FPL_EMAIL, FPL_PASSWORD, FPL_MANAGER_ID.

Retries login + submit with backoff; the caller is responsible for escalating
(app/notify.py) on final failure and for recording status via
app/database.py's set_decision_status().
"""
from __future__ import annotations

import os
import time

import requests

LOGIN_URL = "https://users.premierleague.com/accounts/login/"
FPL_BASE = "https://fantasy.premierleague.com/api"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; fpl-gaffer-bot/1.0)",
    "Referer": "https://fantasy.premierleague.com/",
}


class FplAuthError(Exception):
    pass


def login(email: str | None = None, password: str | None = None, retries: int = 3, backoff_seconds: int = 20) -> requests.Session:
    email = email or os.environ["FPL_EMAIL"]
    password = password or os.environ["FPL_PASSWORD"]

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        session = requests.Session()
        session.headers.update(_HEADERS)
        try:
            session.get(LOGIN_URL, timeout=15)  # seeds csrftoken cookie
            csrf_token = session.cookies.get("csrftoken")
            resp = session.post(
                LOGIN_URL,
                data={
                    "login": email,
                    "password": password,
                    "app": "plfpl-web",
                    "redirect_uri": "https://fantasy.premierleague.com/a/login",
                    "csrfmiddlewaretoken": csrf_token,
                },
                timeout=15,
            )
            resp.raise_for_status()
            if not session.cookies.get("pl_profile"):
                raise FplAuthError("login did not yield a session cookie — check credentials")
            return session
        except (requests.RequestException, FplAuthError) as e:
            last_error = e
            if attempt < retries:
                time.sleep(backoff_seconds)
    raise FplAuthError(f"login failed after {retries} attempts: {last_error}")


def _csrf_headers(session: requests.Session) -> dict:
    token = session.cookies.get("csrftoken")
    return {"X-CSRFToken": token, "Referer": "https://fantasy.premierleague.com/"} if token else {}


def submit_transfers(
    session: requests.Session,
    manager_id: int,
    gameweek: int,
    transfers: list[dict],  # [{"element_in": id, "element_out": id, "purchase_price": int, "selling_price": int}]
    retries: int = 3,
    backoff_seconds: int = 15,
) -> dict:
    payload = {
        "confirmed": True,
        "entry": manager_id,
        "event": gameweek,
        "transfers": transfers,
        "wildcard": False,
        "freehit": False,
    }
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.post(
                f"{FPL_BASE}/transfers/", json=payload, headers=_csrf_headers(session), timeout=20
            )
            resp.raise_for_status()
            return {"ok": True, "response": resp.json() if resp.content else {}}
        except requests.RequestException as e:
            last_error = e
            if attempt < retries:
                time.sleep(backoff_seconds)
    return {"ok": False, "error": str(last_error)}


def fetch_my_team(session: requests.Session, manager_id: int) -> dict:
    """Authenticated equivalent of /entry/{id}/picks/ — includes selling_price per pick,
    which the public endpoint doesn't expose and which transfers require."""
    resp = session.get(f"{FPL_BASE}/my-team/{manager_id}/", headers=_csrf_headers(session), timeout=20)
    resp.raise_for_status()
    return resp.json()


def set_lineup(
    session: requests.Session,
    manager_id: int,
    picks: list[dict],  # [{"element": id, "position": int, "is_captain": bool, "is_vice_captain": bool}]
    retries: int = 3,
    backoff_seconds: int = 15,
) -> dict:
    payload = {"picks": picks}
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.post(
                f"{FPL_BASE}/my-team/{manager_id}/", json=payload, headers=_csrf_headers(session), timeout=20
            )
            resp.raise_for_status()
            return {"ok": True, "response": resp.json() if resp.content else {}}
        except requests.RequestException as e:
            last_error = e
            if attempt < retries:
                time.sleep(backoff_seconds)
    return {"ok": False, "error": str(last_error)}
