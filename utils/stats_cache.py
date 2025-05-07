import json
from pathlib import Path
from datetime import datetime, timedelta
from utils.log import log
from subprocess import run

LEADERBOARD_PATH = Path("data/afl_stats_leaderboard.json")

def ensure_leaderboard_fresh(max_age_hours=24):
    """
    Ensures the leaderboard JSON is fresh.
    If it's missing or older than `max_age_hours`, it runs the scraper.
    """
    if not LEADERBOARD_PATH.exists():
        log("📉 Leaderboard file missing — scraping fresh data...", "INFO")
        _scrape()
        return

    last_modified = datetime.fromtimestamp(LEADERBOARD_PATH.stat().st_mtime)
    if datetime.now() - last_modified > timedelta(hours=max_age_hours):
        log("📉 Leaderboard file is stale — scraping fresh data...", "INFO")
        _scrape()
    else:
        log("📈 Leaderboard file is recent — using cached data", "DEBUG")

def _scrape():
    result = run(["python3", "-m", "scraper.scrape_afl_stats"], capture_output=True, text=True)
    if result.returncode != 0:
        log("❌ Error while scraping leaderboard:\n" + result.stderr, "ERROR")
    else:
        log("✅ Scraped fresh leaderboard data", "SUCCESS")
