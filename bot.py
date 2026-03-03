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
    
    # Triggers the first time the bot wakes up and the deadline is under 12 hours away
    if time_diff <= datetime.timedelta(hours=12) and next_event['id'] not in state.get('deadline_alert', []):
        send_tweet(f"🚨 FPL DEADLINE ALERT 🚨\n\nGameweek {next_event['id']} locks in under 12 hours! Double check your captains and bench. #FPL")
        state.setdefault('deadline_alert', []).append(next_event['id'])

# --- 2. CHECK DOUBLE & BLANK GAMEWEEKS ---
    # Create a dictionary to map team IDs to actual team names for the tweets
    team_mapping = {team['id']: team['name'] for team in fpl_static['teams']}
    
    gw_fixtures = [f for f in fpl_fixtures if f['event'] == next_event['id']]
    teams_playing = [f['team_a'] for f in gw_fixtures] + [f['team_h'] for f in gw_fixtures]
    
    # -- Double Gameweek Logic --
    # If a team is listed more than once in the same Gameweek, it's a Double GW!
    dgw_team_ids = [team for team in set(teams_playing) if teams_playing.count(team) > 1]
    
    if dgw_team_ids and next_event['id'] not in state.get('dgw', []):
        dgw_names = [team_mapping[tid] for tid in dgw_team_ids]
        send_tweet(f"🔥 DOUBLE GAMEWEEK DETECTED 🔥\n\nGet ready for DGW {next_event['id']}! Teams playing twice:\n{', '.join(dgw_names)}\n\n#FPL")
        state.setdefault('dgw', []).append(next_event['id'])

    # -- Blank Gameweek Logic --
    # If any of the 20 Premier League teams are NOT in the teams_playing list, it's a Blank GW!
    all_team_ids = set(team_mapping.keys())
    bgw_team_ids = list(all_team_ids - set(teams_playing))
    
    if bgw_team_ids and next_event['id'] not in state.get('bgw', []):
        bgw_names = [team_mapping[tid] for tid in bgw_team_ids]
        send_tweet(f"❌ BLANK GAMEWEEK DETECTED ❌\n\nThese teams have NO fixture in GW {next_event['id']}:\n{', '.join(bgw_names)}\n\nPlan your bench carefully! #FPL")
        state.setdefault('bgw', []).append(next_event['id'])

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

    # --- 4. TOP 3 PLAYERS OF LAST GW ---
    # Find the gameweek that just finished
    prev_event = next((e for e in fpl_static['events'] if e.get('is_previous')), None)
    
    # If a previous gameweek exists and we haven't tweeted its top players yet
    if prev_event and prev_event['id'] not in state.get('top_players', []):
        # Sort all players by their points in the last gameweek (highest to lowest)
        all_players = fpl_static['elements']
        top_3 = sorted(all_players, key=lambda x: x['event_points'], reverse=True)[:3]
        
        # Build the tweet
        tweet_lines = [f"🌟 GW {prev_event['id']} KINGS OF THE GAMEWEEK 🌟\n"]
        for idx, player in enumerate(top_3, 1):
            tweet_lines.append(f"{idx}. {player['web_name']} - {player['event_points']} pts")
            
        tweet_lines.append("\n#FPL")
        
        # Send the tweet and save to memory so it only posts once
        send_tweet("\n".join(tweet_lines))
        state.setdefault('top_players', []).append(prev_event['id'])

    # --- SAVE MEMORY ---
    state['injuries'] = current_injuries
    with open(state_file, "w") as f:
        json.dump(state, f)

if __name__ == "__main__":
    main()
