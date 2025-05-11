from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timedelta
from utils.log import log
import pytz
import sqlite3
import os
from db.connection import get_db_connection

AWST = pytz.timezone("Australia/Perth")

def run_stats_scraper(match_id: int):
    log(f"📈 Running stat scraper for match {match_id}", "INFO")
    os.system(f"python3 -m scraper.scrape_afl_player_stats --match {match_id}")

def register_stat_scrape_jobs(scheduler):
    log("📋 Registering stat scraping jobs...", "INFO")
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT match_id, start_time_utc
        FROM matches
        WHERE status = 'UPCOMING' AND start_time_utc IS NOT NULL
    """)

    for match_id, start_time_utc in cursor.fetchall():
        match_start = datetime.fromisoformat(start_time_utc).astimezone(AWST)
        scrape_time = match_start - timedelta(minutes=2)

        scheduler.add_job(
            run_stats_scraper,
            trigger=DateTrigger(run_date=scrape_time),
            args=[match_id],
            id=f"stat_scraper_{match_id}"
        )
        log(f"📅 Scheduled: stats for match {match_id} at {scrape_time}", "DEBUG")

    conn.close()
    log("✅ Stat scraping jobs registered", "INFO")
