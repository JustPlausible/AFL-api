# scheduler/schedule_stat_scrapes.py

from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timedelta, timezone
from utils.log import setup_logger
import pytz
import sqlite3
import subprocess
from db.connection import get_db_connection
from scheduler.registry import add_registered_job, stats_match_job_id

# Dedicated logger for scheduler processes (not scraper internals)
scheduler_log = setup_logger("scheduler_jobs", "scheduler_jobs.log")

AWST = pytz.timezone("Australia/Perth")

def run_stats_scraper(match_id: int):
    """Subprocess wrapper to run scraper via match_id."""
    scheduler_log.info(f"📈 Running stat scraper for match {match_id}")
    cmd = f"python3 -m scraper.scrape_afl_player_stats --match-id {match_id}"
    scheduler_log.info(f"📦 Executing command: {cmd}")

    try:
        result = subprocess.run(
            cmd.split(), capture_output=True, text=True, check=True
        )
        scheduler_log.info(f"✅ Command succeeded for match {match_id}")

    except subprocess.CalledProcessError as e:
        scheduler_log.error(f"❌ Command failed for match {match_id} with exit code {e.returncode}")
        scheduler_log.error(f"❌ STDERR:\n{e.stderr.strip()}")

def was_scraped_recently(match_id: int, conn, window_minutes: int = 5) -> bool:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT scraped_at FROM scrape_log
        WHERE match_id = ?
        ORDER BY scraped_at DESC
        LIMIT 1
    """, (match_id,))
    row = cursor.fetchone()
    if not row:
        return False

    scraped_at = datetime.fromisoformat(row[0])
    now = datetime.now(timezone.utc)
    return (now - scraped_at) < timedelta(minutes=window_minutes)

def register_stat_scrape_jobs(scheduler):
    scheduler_log.info("📋 Registering stat scraping jobs...")
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT match_id, start_time_utc
        FROM matches
        WHERE status IN ('UPCOMING', 'LIVE') AND start_time_utc IS NOT NULL
    """)

    for match_id, start_time_utc in cursor.fetchall():
        try:
            # Ensure UTC → AWST with proper timezone awareness
            start_dt = datetime.fromisoformat(start_time_utc).replace(tzinfo=timezone.utc)
            match_start = start_dt.astimezone(AWST)
            scrape_time = match_start + timedelta(seconds=10)

            add_registered_job(
                scheduler, run_stats_scraper,
                trigger=DateTrigger(run_date=scrape_time),
                run_date=scrape_time, args=[match_id],
                job_id=stats_match_job_id(match_id), job_type="player_stats", match_id=match_id,
                name=f"Run stat scraper for match {match_id}",
                replace_existing=True
            )

            scheduler_log.info(f"📝 Scheduled job 'stats_match_{match_id}' for {scrape_time.isoformat()} AWST")

        except Exception as e:
            scheduler_log.error(f"❌ Failed to schedule job for match {match_id}: {e}")

    conn.close()
    scheduler_log.info("✅ Stat scraping jobs registered")

def register_live_stat_scrapers(scheduler):
    scheduler_log.info("📡 Checking for active LIVE matches to resume scraping...")

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT match_id, start_time_utc
        FROM matches
        WHERE status = 'LIVE' AND start_time_utc IS NOT NULL
    """)

    for match_id, start_time_utc in cursor.fetchall():
        if was_scraped_recently(match_id, conn, window_minutes=5):
            scheduler_log.info(f"⏭ Match {match_id} was scraped recently — skipping re-trigger.")
            continue

        scheduler_log.info(f"🚨 Starting immediate scraper for LIVE match {match_id}")
        add_registered_job(
            scheduler, run_stats_scraper,
            trigger=DateTrigger(run_date=datetime.now(AWST)), run_date=datetime.now(AWST), args=[match_id],
            job_id=stats_match_job_id(match_id), job_type="player_stats", match_id=match_id,
            name=f"Recovery stat scraper for LIVE match {match_id}", replace_existing=True
        )

    conn.close()
