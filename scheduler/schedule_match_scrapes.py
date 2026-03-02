# scheduler/schedule_match_scrapes.py

from apscheduler.triggers.interval import IntervalTrigger
from utils.log import setup_logger
from datetime import datetime
import sqlite3
from db.connection import get_db_connection
from scraper.scrape_afl_matches import run as scrape_matches
import sys
import subprocess
from subprocess import Popen
import time, random

log = setup_logger("refresh_live_matches", "refresh_live_matches.log")

def refresh_live_matches():
    log.info("⏰ Running refresh_live_matches job...")
    jitter = random.randint(5, 30)
    log.debug(f"⏱ Sleeping {jitter}s to reduce bot signature...")
    time.sleep(jitter)

    log.info("🔄 Checking for LIVE matches to refresh...")
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT DISTINCT round_id
        FROM matches
        WHERE status = 'LIVE' AND round_id IS NOT NULL
    """)
    rows = cursor.fetchall()

    log.info(f"🔎 Found {len(rows)} matches with status='LIVE'")

    if not rows:
        log.info("📭 No LIVE matches found.")

    for (round_id,) in rows:
        log.info(f"🔁 Refreshing round {round_id} due to LIVE match...")
        scrape_matches(round_id=round_id)

    conn.close()

def scrape_today_matches():
    log.info("🔁 Live match-day scrape running...")
    subprocess.run([sys.executable, "-m", "scraper.scrape_afl_matches"], check=True)

def register_live_match_day_scraper(scheduler):
    def today_has_matches():
        conn = sqlite3.connect("data/afl_players.db")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM matches
            WHERE date(start_time_utc) = date('now', 'localtime')
        """)
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0

    if today_has_matches():
        scheduler.add_job(
            scrape_today_matches,
            trigger=IntervalTrigger(minutes=5),
            id="live_match_scraper",
            name="Scrape matches frequently during match day",
            replace_existing=True
        )
        log.info("✅ Match day detected. Started frequent scraping job.")
    else:
        log.info("🛌 No matches today — skipping frequent scraping job.")

def register_match_scrape_jobs(scheduler):
    scheduler.add_job(
        refresh_live_matches,
        trigger=IntervalTrigger(minutes=5),
        id="refresh_live_matches",
        name="Refresh matches with LIVE status every 5 minutes",
        replace_existing=True
    )
