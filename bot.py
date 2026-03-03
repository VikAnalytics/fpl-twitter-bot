import requests
import json
import os
import datetime
import tweepy
from pathlib import Path

# --- SETUP TWITTER CONNECTION ---
# This safely pulls your hidden secret keys to log into your bot's Twitter account
client = tweepy.Client(
    consumer_key=os.environ.get("TWITTER_CONSUMER_KEY"),
    consumer_secret=os.environ.get("TWITTER_CONSUMER_SECRET"),
    access_token=os.environ.get("TWITTER_ACCESS_TOKEN"),
    access_token_secret=os.environ.get("TWITTER_ACCESS_TOKEN_SECRET")
)

def send_tweet(message):
    try:
        client.create_tweet(text=message)
        print(f"Successfully tweeted: {message}")
    except Exception as e:
        print(f"Error sending tweet: {e}")

def main():
    # --- FETCH FPL DATA ---

    
    fpl_static = requests.get("https://fantasy.premierleague.com/api/bootstrap-static/").json()
    fpl_fixtures = requests.get("https://fantasy.premierleague.com/api/fixtures/").json()
    
    # Find the upcoming gameweek
    next_event = next((e for e in fpl_static['events'] if e['is_next']), None)
    if not next_event: 
        return
        
    # Set up a "memory" file so the bot doesn't tweet the same injury twice
    state_file = Path("fpl_state.json")
    if state_file.exists():
        with open(state_file, "r") as f:
            state = json.load(f)
    else:
        state = {"dgw": [], "injuries": {}}

    # --- 1. CHECK 12-HOUR DEADLINE ---
    deadline = datetime.datetime.strptime(next_event['deadline_time'], "%Y-%m-%dT%H:%M:%SZ")
    time_diff = deadline - datetime.datetime.utcnow()
    
    # Triggers only when the check happens exactly between 11 and 12 hours before deadline
    if datetime.timedelta(hours=11) <= time_diff <= datetime.timedelta(hours=12):
        send_tweet(f"🚨 FPL DEADLINE ALERT 🚨\n\nGameweek {next_event['id']} locks in exactly 12 hours! Double check your captains and bench. #FPL")

    # --- 2. CHECK DOUBLE GAMEWEEKS ---
    gw_fixtures = [f for f in fpl_fixtures if f['event'] == next_event['id']]
    teams_playing = [f['team_a'] for f in gw_fixtures] + [f['team_h'] for f in gw_fixtures]
    
    # If a team is listed more than once in the same Gameweek, it's a Double GW!
    dgw_teams = [team for team in teams_playing if teams_playing.count(team) > 1]
    
    if dgw_teams and next_event['id'] not in state.get('dgw', []):
        send_tweet(f"🔥 DOUBLE GAMEWEEK DETECTED 🔥\n\nGet ready for DGW {next_event['id']}! Time to plan those transfers. #FPL")
        state.setdefault('dgw', []).append(next_event['id'])

    # --- 3. CHECK INJURIES ---
    current_injuries = {}
    # We only look at players owned by more than 5% of people to avoid spamming unknown players
    for p in fpl_static['elements']:
        if float(p['selected_by_percent']) > 5.0 and p['chance_of_playing_next_round'] != 100 and p['chance_of_playing_next_round'] is not None:
            current_injuries[str(p['id'])] = {"name": p['web_name'], "status": p['chance_of_playing_next_round'], "news": p['news']}

    old_injuries = state.get("injuries", {})
    
    for pid, info in current_injuries.items():
        # If the player wasn't injured before, or their injury status changed, tweet it!
        if pid not in old_injuries or old_injuries[pid]['status'] != info['status']:
            tweet_text = f"🏥 FPL INJURY UPDATE 🏥\n\n{info['name']}: {info['news']} ({info['status']}% chance of playing next round). #FPL"
            send_tweet(tweet_text)

    # --- SAVE MEMORY ---
    state['injuries'] = current_injuries
    with open(state_file, "w") as f:
        json.dump(state, f)

if __name__ == "__main__":
    main()
