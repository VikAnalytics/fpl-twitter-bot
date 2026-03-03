# 🤖 Serverless FPL Twitter Agent

An event-driven FPL data pipeline that tracks API changes, manages state to provide real-time alerts and posts critical updates directly to X (Twitter). 


## ✨ Key Features

* **🚨 Dynamic Deadline Alerts:** Calculates exact time-deltas to the upcoming gameweek deadline and fires an alert when the 12-hour window is breached.
* **🏥 Injury Tracking & State Management:** Monitors players with >5% ownership for status changes. Uses a local JSON state file to "remember" previously tweeted injuries, ensuring it only alerts on *new* or *changed* injury news.
* **🔥 Blank & Double Gameweek Detection:** Dynamically parses the weekly fixture lists to identify teams playing zero or multiple times in a single gameweek.
* **🌟 Weekly Wrap-Up:** Automatically triggers after a gameweek concludes to extract, sort, and publish the top 3 highest-scoring players of the week.

## 🏗️ Architecture: Decoupled & Event-Driven
Unlike standard cron-based scripts which are prone to execution jitter, this project utilizes a **decoupled orchestration pattern**. 

* **Orchestration Layer:** An external, precision-timed scheduler (`cron-job.org`) handles the clock, ensuring sub-second execution timing.
* **Compute Layer:** GitHub Actions acts as a serverless execution environment, triggered only via the GitHub REST API (`workflow_dispatch`).
* **Why this matters:** This architecture separates the "clock" from the "compute," ensuring reliable execution and preventing the "thundering herd" latency issues common in native CI/CD schedulers.

## ⚙️ How It Works (The Pipeline)

1. **Trigger:** An external scheduler pings the GitHub API at the 11-minute mark of every hour.
2. **Execute:** The GitHub Action environment spins up, authenticates via secure tokens, and executes the Python processing logic.
3. **State Management:** The script compares current FPL API state with a local `fpl_state.json` file to filter out redundant alerts (e.g., preventing duplicate injury notifications).
4. **Commit:** The system commits the updated state file back to the repository, ensuring continuity for the next hourly execution.

## 🚀 Professional Engineering Highlights
* **Decoupled Architecture:** Moved away from non-deterministic native cron to an API-driven event pattern.
* **API Versioning:** Implemented explicit GitHub API versioning (`2022-11-28`) for production stability.
* **Security-First:** Implemented fine-grained Personal Access Tokens (PATs) with 30-day rotation cycles to mirror enterprise IAM best practices.

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
