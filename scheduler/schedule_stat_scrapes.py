# scheduler/schedule_stat_scrapes.py

from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timedelta, timezone
from utils.log import setup_logger
import pytz
from db.connection import get_db_connection
from scheduler.registry import add_registered_job, stats_match_job_id

# Dedicated logger for scheduler processes (not scraper internals)
scheduler_log = setup_logger("scheduler_jobs", "scheduler_jobs.log")

AWST = pytz.timezone("Australia/Perth")

def run_stats_scraper(match_id: int):
    """Run the policy-selected canonical CFS collector for an internal match ID."""
    from collection.source_policy import OperationalDomain, collect_operational
    scheduler_log.info(f"📈 Running CFS stat collector for match {match_id}")
    return collect_operational(OperationalDomain.MATCH_PLAYER_STATS, target_id=match_id)

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
        recovery_time = datetime.now(AWST) + timedelta(seconds=1)
        add_registered_job(
            scheduler, run_stats_scraper,
            trigger=DateTrigger(run_date=recovery_time), run_date=recovery_time, args=[match_id],
            job_id=stats_match_job_id(match_id), job_type="player_stats", match_id=match_id,
            name=f"Recovery stat scraper for LIVE match {match_id}", replace_existing=True
        )

    conn.close()
