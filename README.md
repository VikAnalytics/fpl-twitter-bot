# 🤖 Serverless FPL Twitter Agent

An automated, serverless Python bot that tracks the official Fantasy Premier League (FPL) API and posts critical updates directly to X (Twitter). 



This project runs 24/7 in the cloud without a dedicated server, utilizing **GitHub Actions** as a cron scheduler to extract data, evaluate state changes, and push real-time alerts for FPL managers.

## ✨ Key Features

* **🚨 Dynamic Deadline Alerts:** Calculates exact time-deltas to the upcoming gameweek deadline and fires an alert when the 12-hour window is breached.
* **🏥 Injury Tracking & State Management:** Monitors players with >5% ownership for status changes. Uses a local JSON state file to "remember" previously tweeted injuries, ensuring it only alerts on *new* or *changed* injury news.
* **🔥 Blank & Double Gameweek Detection:** Dynamically parses the weekly fixture lists to identify teams playing zero or multiple times in a single gameweek.
* **🌟 Weekly Wrap-Up:** Automatically triggers after a gameweek concludes to extract, sort, and publish the top 3 highest-scoring players of the week.

## 🛠️ Architecture & Tech Stack

* **Language:** Python 3.10
* **Data Source:** Official FPL REST API (`bootstrap-static` and `fixtures` endpoints)
* **Integration:** X/Twitter API v2 (via `tweepy` library)
* **Automation:** GitHub Actions (Scheduled CRON jobs running hourly)
* **State Management:** JSON (`fpl_state.json`) written back to the repository by the GitHub bot to maintain continuity between ephemeral serverless runs.

## ⚙️ How It Works (The ETL Pipeline)

1. **Extract:** Every hour, GitHub Actions spins up a container and pulls the latest JSON payloads from the FPL API.
2. **Transform:** The Python script parses the data, compares current injury statuses and gameweek IDs against the stored `fpl_state.json` memory file, and formats the output strings.
3. **Load:** If new conditions are met (e.g., a new injury, or the deadline is <12 hours away), the bot authenticates securely via GitHub Secrets and pushes the payload to the Twitter API. Finally, it commits the updated state file back to the repository.

## 🚀 Setup & Deployment (For Developers)

If you want to fork this repository and run your own instance:

1. Clone/Fork the repository.
2. Set up a free developer account on X to get your App credentials.
3. Add the following repository secrets in GitHub (`Settings > Secrets and variables > Actions`):
   * `TWITTER_CONSUMER_KEY`
   * `TWITTER_CONSUMER_SECRET`
   * `TWITTER_ACCESS_TOKEN`
   * `TWITTER_ACCESS_TOKEN_SECRET`
4. The GitHub Action is already configured in `.github/workflows/run_bot.yml` to run at the top of every hour!
