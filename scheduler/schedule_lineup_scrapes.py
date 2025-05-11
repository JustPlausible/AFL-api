from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timedelta
from db.helpers import get_round_start_times
import sqlite3
import pytz
import os
from utils.log import log
from db.connection import get_db_connection

AWST = pytz.timezone("Australia/Perth")

def run_lineup_round_scraper(round_id: int):
    log(f"🚀 [Lineups] Running line-up scrape for round {round_id}", "INFO")
    os.system(f"python3 -m scraper.scrape_afl_lineups {round_id}")

def run_lineup_match_scraper(match_id: int):
    log(f"🚀 [Lineups] Running line-up scrape for match {match_id}", "INFO")
    os.system(f"python3 -m scraper.scrape_afl_lineups --match {match_id}")

def register_lineup_jobs(scheduler):
    log("📋 Registering line-up scrape jobs...", "INFO")
    conn = get_db_connection()

    for round_id, round_start_utc in get_round_start_times(conn):
        if not round_start_utc:
            log(f"⚠️ Skipping round {round_id} — no start time available", "WARN")
            continue

        round_start = datetime.fromisoformat(round_start_utc).astimezone(AWST)
        log(f"🕒 Round {round_id} first match: {round_start.isoformat()}", "DEBUG")

        # T-1 day @ 5pm AWST
        day_before_5pm = round_start.replace(hour=17, minute=0, second=0, microsecond=0) - timedelta(days=1)
        scheduler.add_job(run_lineup_round_scraper, trigger=DateTrigger(run_date=day_before_5pm), args=[round_id])
        log(f"📅 Scheduled: T-1 day 5pm for Round {round_id} → {day_before_5pm}", "INFO")

        # If Thursday, also 5pm Thursday
        if round_start.weekday() == 3:  # Thursday
            thursday_5pm = round_start.replace(hour=17, minute=0, second=0, microsecond=0)
            scheduler.add_job(run_lineup_round_scraper, trigger=DateTrigger(run_date=thursday_5pm), args=[round_id])
            log(f"📅 Scheduled: Thursday 5pm for Round {round_id} → {thursday_5pm}", "INFO")

        # 1-hour-before each match
        cursor = conn.cursor()
        cursor.execute("""
            SELECT match_id, start_time_utc
            FROM matches
            WHERE round_id = ? AND start_time_utc IS NOT NULL
        """, (round_id,))
        match_rows = cursor.fetchall()
        log(f"📦 Found {len(match_rows)} matches in Round {round_id} to schedule 1hr-before scrapes", "DEBUG")

        for match_id, match_start_utc in match_rows:
            match_start = datetime.fromisoformat(match_start_utc).astimezone(AWST)
            one_hour_before = match_start - timedelta(hours=1)
            scheduler.add_job(run_lineup_match_scraper, trigger=DateTrigger(run_date=one_hour_before), args=[match_id])
            log(f"📅 Scheduled: 1 hour before match {match_id} → {one_hour_before}", "DEBUG")

    conn.close()
    log("✅ Line-up scrape jobs registered successfully.", "INFO")
