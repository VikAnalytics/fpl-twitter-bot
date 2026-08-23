"""
Escalation AND approval channel — Telegram's official Bot API. Free,
first-party (no reverse-engineered WhatsApp API, no paid X/Twitter DM tier
required). A plain tweet isn't guaranteed to be seen in time, and a
missed-execution "never acceptable" requirement means failures go straight
to the phone. Decision proposals are also sent here with inline Approve/
Reject buttons — tap one, done, no need to visit /decisions/{manager_id}
(that page still works too, just as a secondary surface).

One-time setup (free):
1. Message @BotFather on Telegram, run /newbot, copy the bot token it gives you.
2. Message your new bot once (anything) so it can message you back.
3. Fetch your chat_id: GET https://api.telegram.org/bot<token>/getUpdates
   and read `message.chat.id` from the reply.
4. Once deployed, register the webhook so button taps reach the app:
   GET https://api.telegram.org/bot<token>/setWebhook?url=<service-url>/telegram/webhook&secret_token=<TELEGRAM_WEBHOOK_SECRET>

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_WEBHOOK_SECRET (any
random string you make up — verified against the header Telegram echoes back
on every webhook call, same shared-secret pattern as CRON_SECRET).
Falls back to a dry-run print if unset, same pattern as bot.py's Twitter client.
"""
from __future__ import annotations

import os
import sys

import requests


def send_telegram_alert(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"[DRY-RUN Telegram] {message}")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"Telegram alert failed: {e}", file=sys.stderr)
        return False


def escalate_unapproved_decision(gameweek: int, hours_left: float, decision_id: int, manager_id: int | None = None) -> bool:
    review_hint = f"/decisions/{manager_id}" if manager_id else "the decisions page"
    return send_telegram_alert(
        f"⚠️ FPL ALERT: GW{gameweek} decision #{decision_id} still unapproved with "
        f"{hours_left:.1f}h left before deadline. Review and approve at {review_hint} "
        f"or make the transfer manually — this will NOT execute on its own."
    )


def escalate_execution_failure(gameweek: int, decision_id: int, error: str) -> bool:
    return send_telegram_alert(
        f"🚨 FPL EXECUTION FAILED: GW{gameweek} decision #{decision_id} could not be "
        f"submitted after retries ({error}). Log in and make the change manually — "
        f"deadline is close."
    )


def escalate_failsafe(gameweek: int, decision_id: int) -> bool:
    return send_telegram_alert(
        f"🚨🚨 FPL LAST-CHANCE ALERT: GW{gameweek} decision #{decision_id} is STILL not "
        f"executed with under 1 hour to deadline. This is the final automated warning — "
        f"act now."
    )


# ── Approval via Telegram (inline buttons) ───────────────────────────────────

def format_decision_summary(decision_type: str, gameweek: int, proposal: dict) -> str:
    if decision_type == "transfer":
        if not proposal.get("transfers"):
            return f"📋 GW{gameweek} TRANSFER: no move recommended this week.\n{proposal.get('summary', '')}"
        lines = [f"📋 GW{gameweek} TRANSFER PROPOSAL (confidence: {proposal.get('confidence', '?')})"]
        for t in proposal["transfers"]:
            lines.append(f"{t['out']} → {t['in']}: {t.get('rationale', '')}")
        if proposal.get("summary"):
            lines.append(proposal["summary"])
        return "\n".join(lines)
    if decision_type == "captain":
        return f"⭐ GW{gameweek} CAPTAIN: {proposal['captain']} (vice: {proposal['vice']})\n{proposal.get('rationale', '')}"
    if decision_type == "lineup":
        xi = ", ".join(p["player"] for p in proposal.get("starting_xi", []))
        bench = ", ".join(b["player"] for b in proposal.get("bench_order", []))
        return f"🧩 GW{gameweek} LINEUP ({proposal.get('formation', '?')})\nXI: {xi}\nBench: {bench}"
    return f"GW{gameweek} {decision_type}: {proposal}"


def send_decision_for_approval(decision_id: int, text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"[DRY-RUN Telegram approval] {text}")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "✅ Approve", "callback_data": f"approve:{decision_id}"},
                        {"text": "❌ Reject", "callback_data": f"reject:{decision_id}"},
                    ]]
                },
            },
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"Telegram approval message failed: {e}", file=sys.stderr)
        return False


def answer_telegram_callback(callback_query_id: str, text: str) -> None:
    """Acknowledges a button tap — clears the loading spinner on the button
    and shows a small toast in Telegram confirming what happened."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"Telegram callback answer failed: {e}", file=sys.stderr)
