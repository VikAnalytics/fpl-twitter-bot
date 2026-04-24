import re
import requests
import os
import datetime
from collections import Counter
import tweepy

from app.database import init_db, get_full_bot_state, set_full_bot_state

init_db()

TWEET_LIMIT = 280
_NEWS_CHANCE_SUFFIX = re.compile(r"\s*-?\s*\d{1,3}\s*%\s*chance of playing\.?\s*$", re.IGNORECASE)


def _clean_news(news: str) -> str:
    """Remove trailing '- NN% chance of playing' so we don't double up with status line."""
    if not news:
        return "Injury concern"
    cleaned = _NEWS_CHANCE_SUFFIX.sub("", news).strip().rstrip("-").strip()
    return cleaned or "Injury concern"


def _format_countdown(td: datetime.timedelta) -> str:
    """e.g. '9h 12m' or '45m'."""
    total_min = int(td.total_seconds() // 60)
    hours, minutes = divmod(max(total_min, 0), 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _fit_tweet(lines: list[str], footer: str = "") -> str:
    """Join lines; if over limit, trim from the middle data block, keeping header + footer."""
    text = "\n".join(lines + ([footer] if footer else []))
    if len(text) <= TWEET_LIMIT:
        return text
    # Drop trailing data lines until it fits; replace removed tail with '…'
    body = list(lines)
    while body and len("\n".join(body + ["…", footer] if footer else body + ["…"])) > TWEET_LIMIT:
        body.pop()
    return "\n".join(body + ["…"] + ([footer] if footer else []))

_TWITTER_KEYS = ("TWITTER_CONSUMER_KEY", "TWITTER_CONSUMER_SECRET",
                 "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")

if all(os.environ.get(k) for k in _TWITTER_KEYS):
    client = tweepy.Client(
        consumer_key=os.environ["TWITTER_CONSUMER_KEY"],
        consumer_secret=os.environ["TWITTER_CONSUMER_SECRET"],
        access_token=os.environ["TWITTER_ACCESS_TOKEN"],
        access_token_secret=os.environ["TWITTER_ACCESS_TOKEN_SECRET"],
    )
else:
    client = None
    print("Twitter credentials missing — running in dry-run mode.")


def send_tweet(message: str) -> None:
    if client is None:
        print(f"[DRY-RUN] {message}")
        return
    try:
        client.create_tweet(text=message)
        print(f"Successfully tweeted: {message}")
    except Exception as e:
        print(f"Error sending tweet: {e}")


def _trim(seq: list, keep: int = 10) -> list:
    """Trim bot-state id lists so they don't grow forever."""
    return seq[-keep:] if len(seq) > keep else seq

def main():
    # --- FETCH FPL DATA ---

    
    r_static = requests.get("https://fantasy.premierleague.com/api/bootstrap-static/", timeout=30)
    r_static.raise_for_status()
    fpl_static = r_static.json()
    if not isinstance(fpl_static, dict) or 'events' not in fpl_static:
        print(f"Unexpected FPL API response: {str(fpl_static)[:200]}")
        return

    r_fixtures = requests.get("https://fantasy.premierleague.com/api/fixtures/", timeout=30)
    r_fixtures.raise_for_status()
    fpl_fixtures = r_fixtures.json()

    # Find the upcoming gameweek
    next_event = next((e for e in fpl_static['events'] if e['is_next']), None)
    if not next_event: 
        return
        
    _stored = get_full_bot_state()
    state = {
        "dgw": _stored.get("dgw", []),
        "bgw": _stored.get("bgw", []),
        "injuries": _stored.get("injuries", {}),
        "deadline_alert": _stored.get("deadline_alert", []),
        "top_players": _stored.get("top_players", []),
    }
        
# --- 1. CHECK 12-HOUR DEADLINE ---
    deadline = datetime.datetime.strptime(
        next_event['deadline_time'], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=datetime.timezone.utc)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    time_diff = deadline - now_utc

    # Triggers the first time the bot wakes up and the deadline is under 12 hours away.
    # Skip if already past deadline (time_diff < 0) — guards against stale state.
    if datetime.timedelta(0) < time_diff <= datetime.timedelta(hours=12) \
            and next_event['id'] not in state.get('deadline_alert', []):
        countdown = _format_countdown(time_diff)
        send_tweet(_fit_tweet(
            [
                f"⏰ DEADLINE INCOMING",
                "",
                f"GW{next_event['id']} locks in {countdown}.",
                "",
                "Final checks:",
                "• Captain sorted?",
                "• Bench order?",
                "• Injuries cleared?",
            ],
            footer=f"\n#FPL #GW{next_event['id']}",
        ))
        state.setdefault('deadline_alert', []).append(next_event['id'])

# --- 2. CHECK DOUBLE & BLANK GAMEWEEKS ---
    team_full = {team['id']: team['name'] for team in fpl_static['teams']}
    team_short = {team['id']: team['short_name'] for team in fpl_static['teams']}

    gw_fixtures = [f for f in fpl_fixtures if f['event'] == next_event['id']]
    team_counts = Counter()
    for f in gw_fixtures:
        team_counts[f['team_a']] += 1
        team_counts[f['team_h']] += 1

    # -- Double Gameweek --
    dgw_team_ids = [tid for tid, c in team_counts.items() if c > 1]
    if dgw_team_ids and next_event['id'] not in state.get('dgw', []):
        dgw_names = ", ".join(team_short[tid] for tid in dgw_team_ids)
        send_tweet(_fit_tweet(
            [
                f"🔥 DGW{next_event['id']} CONFIRMED",
                "",
                f"{len(dgw_team_ids)} teams play twice:",
                dgw_names,
                "",
                "Captaincy goldmine — stack exposure.",
            ],
            footer="\n#FPL #DGW",
        ))
        state.setdefault('dgw', []).append(next_event['id'])

    # -- Blank Gameweek --
    # Only fire when fixture list looks settled (deadline ≤ 7 days AND ≥ 7 fixtures).
    all_team_ids = set(team_full.keys())
    bgw_team_ids = list(all_team_ids - set(team_counts.keys()))
    schedule_settled = (
        time_diff <= datetime.timedelta(days=7)
        and len(gw_fixtures) >= 7
    )
    if bgw_team_ids and schedule_settled and next_event['id'] not in state.get('bgw', []):
        bgw_names = ", ".join(team_short[tid] for tid in bgw_team_ids)
        send_tweet(_fit_tweet(
            [
                f"🚫 BGW{next_event['id']} INCOMING",
                "",
                f"{len(bgw_team_ids)} teams blank:",
                bgw_names,
                "",
                "Bench them. Plan transfers. Chip window?",
            ],
            footer="\n#FPL #BGW",
        ))
        state.setdefault('bgw', []).append(next_event['id'])

    # --- 3. CHECK INJURIES ---
    # Owned >5% to avoid spam. Track both injury additions/changes AND recoveries.
    position_map = {et['id']: et['singular_name_short'] for et in fpl_static['element_types']}
    current_injuries: dict[str, dict] = {}
    for p in fpl_static['elements']:
        own = float(p.get('selected_by_percent') or 0)
        chance = p.get('chance_of_playing_next_round')
        if own > 5.0 and chance is not None and chance != 100:
            current_injuries[str(p['id'])] = {
                "name": p['web_name'],
                "team": team_short.get(p['team'], ""),
                "pos": position_map.get(p['element_type'], ""),
                "status": chance,
                "news": _clean_news(p.get('news') or ""),
                "own": own,
            }

    old_injuries = state.get("injuries", {})

    # New / worsened / changed injuries
    for pid, info in current_injuries.items():
        prev_status = old_injuries.get(pid, {}).get('status')
        if prev_status != info['status']:
            # Severity icon: 0% out, ≤50% doubtful, >50% likely
            if info['status'] == 0:
                sev = "🔴 RULED OUT"
            elif info['status'] <= 50:
                sev = "🟠 DOUBTFUL"
            else:
                sev = "🟡 KNOCK"
            send_tweet(_fit_tweet(
                [
                    f"🏥 {sev}: {info['name']} ({info['team']} {info['pos']})",
                    "",
                    info['news'],
                    f"Chance of playing: {info['status']}%",
                    f"Ownership: {info['own']:.1f}%",
                ],
                footer="\n#FPL",
            ))

    # Recoveries: was flagged, now back to 100% or cleared entirely
    for pid, prev in old_injuries.items():
        if pid in current_injuries:
            continue
        p = next((pl for pl in fpl_static['elements'] if str(pl['id']) == pid), None)
        if not p:
            continue
        chance = p.get('chance_of_playing_next_round')
        if chance == 100 or chance is None:
            club = team_short.get(p['team'], "")
            pos = position_map.get(p['element_type'], "")
            send_tweet(_fit_tweet(
                [
                    f"✅ RETURN: {prev['name']} ({club} {pos})",
                    "",
                    "Back to full fitness.",
                    "Transfer window: open.",
                ],
                footer="\n#FPL",
            ))

    # --- 4. KINGS OF THE GAMEWEEK ---
    # Use the finished event's /event/{id}/live/ payload (authoritative). `event_points`
    # on bootstrap resets to the current event after a new deadline opens, so it can
    # be 0 for most players if the bot runs between GWs — that's why the old version
    # was shaky. Only rely on `is_previous` to identify the target GW, then pull live.
    prev_event = next((e for e in fpl_static['events'] if e.get('is_previous')), None)

    if prev_event and prev_event.get('finished') and prev_event['id'] not in state.get('top_players', []):
        try:
            r_live = requests.get(
                f"https://fantasy.premierleague.com/api/event/{prev_event['id']}/live/",
                timeout=30,
            )
            r_live.raise_for_status()
            live = r_live.json()
        except requests.RequestException as e:
            print(f"Could not fetch live data for GW {prev_event['id']}: {e}")
            live = None

        if live and isinstance(live.get('elements'), list):
            player_map = {p['id']: p for p in fpl_static['elements']}

            # Build (player, points, minutes) — minutes > 0 to exclude no-shows tied on 0
            scored = []
            for el in live['elements']:
                stats = el.get('stats', {}) or {}
                pts = stats.get('total_points', 0)
                mins = stats.get('minutes', 0)
                if mins <= 0:
                    continue
                p = player_map.get(el['id'])
                if not p:
                    continue
                scored.append((p, pts, mins))

            # Sort: points desc, then minutes desc (tiebreak), then lower ownership (differential wins ties)
            scored.sort(
                key=lambda x: (x[1], x[2], -float(x[0].get('selected_by_percent') or 0)),
                reverse=True,
            )

            if scored:
                cutoff_points = scored[min(2, len(scored) - 1)][1]
                top = [row for row in scored if row[1] >= cutoff_points][:5]
                any_diff = any(float(p.get('selected_by_percent') or 0) < 5.0 for p, _, _ in top)

                lines = [f"👑 GW{prev_event['id']} KINGS OF THE GAMEWEEK", ""]
                for idx, (p, pts, _) in enumerate(top, 1):
                    pos = position_map.get(p['element_type'], '')
                    club = team_short.get(p['team'], '')
                    own = float(p.get('selected_by_percent') or 0)
                    diff_tag = " 💎" if own < 5.0 else ""
                    lines.append(f"{idx}. {p['web_name']} ({club} {pos}) — {pts}pts{diff_tag}")
                if any_diff:
                    lines.extend(["", "💎 = sub-5% differential"])

                send_tweet(_fit_tweet(lines, footer=f"\n#FPL #GW{prev_event['id']}"))
                state.setdefault('top_players', []).append(prev_event['id'])

    state['injuries'] = current_injuries
    for k in ('dgw', 'bgw', 'deadline_alert', 'top_players'):
        if isinstance(state.get(k), list):
            state[k] = _trim(state[k])
    set_full_bot_state(state)

if __name__ == "__main__":
    main()
