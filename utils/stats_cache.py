import json
import sys
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
        return _scrape()

    last_modified = datetime.fromtimestamp(LEADERBOARD_PATH.stat().st_mtime)
    if datetime.now() - last_modified > timedelta(hours=max_age_hours):
        log("📉 Leaderboard file is stale — scraping fresh data...", "INFO")
        return _scrape()
    else:
        log("📈 Leaderboard file is recent — using cached data", "DEBUG")
        return True

def _scrape():
    # Use the interpreter running the application and the module that actually
    # produces afl_stats_leaderboard.json. The old module name did not exist.
    result = run([sys.executable, "-m", "scraper.scrape_afl_players"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("Leaderboard refresh failed: " + (result.stderr.strip() or result.stdout.strip()))
    if not LEADERBOARD_PATH.exists():
        raise RuntimeError("Leaderboard refresh completed without creating the leaderboard file")
    try:
        records = json.loads(LEADERBOARD_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Leaderboard refresh produced invalid JSON: {exc}") from exc
    if not isinstance(records, list) or not records:
        raise RuntimeError("Leaderboard refresh produced no player records")
    log("✅ Scraped fresh leaderboard data", "SUCCESS")
    return True
