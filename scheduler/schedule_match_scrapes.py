# scheduler/schedule_match_scrapes.py

from apscheduler.triggers.interval import IntervalTrigger
from utils.log import setup_logger
from db.connection import get_db_connection
from scheduler.registry import add_registered_job, live_match_day_job_id, live_match_refresh_job_id
import random
import time

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
        SELECT match_id
        FROM matches
        WHERE status = 'LIVE' AND round_id IS NOT NULL
    """)
    rows = cursor.fetchall()

    log.info(f"🔎 Found {len(rows)} matches with status='LIVE'")

    if not rows:
        log.info("📭 No LIVE matches found.")

    from collection.source_policy import OperationalDomain, collect_operational
    for (match_id,) in rows:
        log.info(f"🔁 Refreshing public JSON status for LIVE match {match_id}...")
        collect_operational(OperationalDomain.MATCH_STATUS, target_id=match_id)

    conn.close()

def scrape_today_matches():
    log.info("🔁 Live match-day public JSON status collection running...")
    conn = get_db_connection()
    try:
        rows = conn.execute("""
            SELECT match_id FROM matches
            WHERE date(start_time_utc) = date('now', 'localtime')
        """).fetchall()
    finally:
        conn.close()
    from collection.source_policy import OperationalDomain, collect_operational
    return [collect_operational(OperationalDomain.MATCH_STATUS, target_id=row[0]) for row in rows]

def register_live_match_day_scraper(scheduler):
    def today_has_matches():
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM matches
            WHERE date(start_time_utc) = date('now', 'localtime')
        """)
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0

    if today_has_matches():
        add_registered_job(
            scheduler, scrape_today_matches, trigger=IntervalTrigger(minutes=5), args=[],
            job_id=live_match_day_job_id(), job_type="match_refresh",
            name="Scrape matches frequently during match day", replace_existing=True, trigger_type="interval"
        )
        log.info("✅ Match day detected. Started frequent scraping job.")
    else:
        log.info("🛌 No matches today — skipping frequent scraping job.")

def register_match_scrape_jobs(scheduler):
    add_registered_job(
        scheduler, refresh_live_matches, trigger=IntervalTrigger(minutes=5), args=[],
        job_id=live_match_refresh_job_id(), job_type="match_refresh",
        name="Refresh matches with LIVE status every 5 minutes", replace_existing=True, trigger_type="interval"
    )
