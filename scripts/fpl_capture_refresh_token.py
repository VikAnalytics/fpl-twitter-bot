"""
One-time (or occasional) local setup: capture a long-lived FPL refresh token
by letting YOU log in yourself in a real browser, then storing the resulting
OIDC refresh token in Turso for the autonomous pipeline to use.

Why this exists: FPL migrated login to PingOne (an OAuth2/OIDC identity
provider) — see docs/FINDINGS in the FPL-MCP project (github.com/MoayadAbbara/
FPL-MCP) for the full research trail. The site's own JS (oidc-client-ts)
stores the completed session — including a refresh_token (scope includes
`offline_access`) — in the browser's localStorage after you log in, under the
key `oidc.user:<authority>:<client_id>`. This script:

  1. Opens a real, visible Chromium window on the FPL site.
  2. YOU log in yourself — your password is typed into FPL's own official
     page and never touches this script. This is not automating a login,
     it's reading the token a human-completed login already produced.
  3. Reads the refresh_token out of localStorage once the login completes.
  4. Writes it straight to Turso (`bot_state` key `fpl_refresh_token`) so the
     Cloud Run pipeline can use it immediately — no manual secret copy-paste.

Run this once, or whenever `app/fpl_auth.py` reports the refresh token was
rejected (it can eventually expire or be revoked by FPL).

Requires Playwright, which is NOT a production dependency — install it just
for this script:

    pip install playwright
    playwright install chromium
    python scripts/fpl_capture_refresh_token.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Running this script directly (`python scripts/fpl_capture_refresh_token.py`)
# puts scripts/ on sys.path, not the repo root — `app` lives at the root, so
# it isn't importable without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FPL_HOME = "https://fantasy.premierleague.com/"
OIDC_AUTHORITY = "https://account.premierleague.com/as"
OIDC_CLIENT_ID = "bfcbaf69-aade-4c1b-8f00-c1cb8a193030"
OIDC_STORAGE_KEY = f"oidc.user:{OIDC_AUTHORITY}:{OIDC_CLIENT_ID}"
TIMEOUT_SECONDS = 300.0


async def _wait_for_session(page) -> dict | None:
    waited = 0.0
    while waited < TIMEOUT_SECONDS:
        if page.is_closed():
            print("Browser window was closed. Nothing saved.", file=sys.stderr)
            return None
        try:
            raw = await page.evaluate(
                "key => window.localStorage.getItem(key)", OIDC_STORAGE_KEY
            )
        except Exception:
            # The login flow navigates the page several times (FPL -> PingOne
            # -> back to FPL); evaluate() throws if it lands mid-navigation.
            # Just skip this poll and try again on the next tick.
            raw = None
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        await asyncio.sleep(1.0)
        waited += 1.0
    return None


async def main() -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto(FPL_HOME)

        print(
            "\nA browser window has opened. Log in to FPL yourself — "
            f"waiting up to {int(TIMEOUT_SECONDS)}s...\n",
            file=sys.stderr,
        )

        session = await _wait_for_session(page)
        await browser.close()

        if session is None:
            print("Timed out waiting for login. Nothing saved.", file=sys.stderr)
            return 1

        refresh_token = session.get("refresh_token")
        access_token = session.get("access_token")
        expires_at = session.get("expires_at")
        if not refresh_token:
            print(
                "Logged in, but no refresh_token was present in the session "
                "(scope may not include offline_access). Nothing saved.",
                file=sys.stderr,
            )
            return 1

        from dotenv import load_dotenv
        load_dotenv()
        from app import database as db

        db.set_bot_state("fpl_refresh_token", refresh_token)
        if access_token and expires_at:
            db.set_bot_state("fpl_access_token", access_token)
            db.set_bot_state("fpl_access_token_expires_at", expires_at)
        db.close()

        print("Saved refresh token to Turso (bot_state). Automated execution is live.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
