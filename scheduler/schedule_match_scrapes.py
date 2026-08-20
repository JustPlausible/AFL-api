# scheduler/schedule_match_scrapes.py

from apscheduler.triggers.interval import IntervalTrigger
from utils.log import setup_logger
from db.connection import get_db_connection
from scheduler.registry import add_registered_job, live_match_day_job_id, live_match_refresh_job_id
from scheduler.registry import match_refresh_job_id, record_planning_failure
from scheduler.time_policy import MetadataTimestampError, match_day_bounds, parse_metadata_timestamp
from datetime import datetime
import random
import time
from logging_sources import LOG_SOURCES

_source = LOG_SOURCES["refresh_live_matches"]
log = setup_logger(_source.logger_name, _source.filename)

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

    from collection.source_policy import OperationalDomain
    from scheduler.collection import collect_scheduled
    for (match_id,) in rows:
        log.info(f"🔁 Refreshing public JSON status for LIVE match {match_id}...")
        collect_scheduled(OperationalDomain.MATCH_STATUS, target_id=match_id)

    conn.close()

def _today_match_ids(conn, now: datetime | None = None) -> list[int]:
    """Select matches in the configured AFL day without database timezone logic."""
    start, end = match_day_bounds(now)
    selected = []
    for match_id, raw_start in conn.execute("SELECT match_id, start_time_utc FROM matches"):
        try:
            instant = parse_metadata_timestamp(raw_start)
        except MetadataTimestampError as exc:
            record_planning_failure(
                match_refresh_job_id(match_id), "match_refresh", exc.reason_code, match_id=match_id
            )
            log.error("Failed to plan match-day refresh for match %s: %s", match_id, exc.reason_code)
            continue
        if start <= instant < end:
            selected.append(match_id)
    return selected


def scrape_today_matches(now: datetime | None = None):
    log.info("🔁 Live match-day public JSON status collection running...")
    conn = get_db_connection()
    try:
        match_ids = _today_match_ids(conn, now)
    finally:
        conn.close()
    from collection.source_policy import OperationalDomain
    from scheduler.collection import collect_scheduled
    return [collect_scheduled(OperationalDomain.MATCH_STATUS, target_id=match_id) for match_id in match_ids]

def register_live_match_day_scraper(scheduler, now: datetime | None = None):
    def today_has_matches():
        conn = get_db_connection()
        try:
            return bool(_today_match_ids(conn, now))
        finally:
            conn.close()

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
