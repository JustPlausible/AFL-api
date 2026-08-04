from apscheduler.triggers.date import DateTrigger
from datetime import timedelta
from db.helpers import get_round_start_times
from utils.log import setup_logger
from db.connection import get_db_connection
from scheduler.registry import add_registered_job, lineup_match_job_id, lineup_round_job_id, record_planning_failure
from scheduler.time_policy import MetadataTimestampError, match_day_timezone, parse_metadata_timestamp

log = setup_logger("refresh_afl_lineups", "refresh_afl_lineups.log")

def run_lineup_round_scraper(round_id: int):
    from collection.source_policy import OperationalDomain, collect_operational
    log.info(f"🚀 [Lineups] Running persistent HTML lineup collection for round {round_id}")
    return collect_operational(OperationalDomain.LINEUPS, target_id=round_id)

def run_lineup_match_scraper(match_id: int):
    from collection.source_policy import OperationalDomain, collect_operational, round_for_match
    log.info(f"🚀 [Lineups] Running persistent HTML lineup collection for match {match_id}")
    return collect_operational(OperationalDomain.LINEUPS,
                               target_id=round_for_match(match_id))

def register_lineup_jobs(scheduler):
    log.info("📋 Registering line-up scrape jobs...")
    conn = get_db_connection()

    for round_id, round_start_utc in get_round_start_times(conn):
        if not round_start_utc:
            log.warning(f"⚠️ Skipping round {round_id} — no start time available")
            continue

        try:
            round_start = parse_metadata_timestamp(round_start_utc).astimezone(match_day_timezone())
        except MetadataTimestampError as exc:
            record_planning_failure(
                lineup_round_job_id(round_id, "day_before_5pm"), "lineup", exc.reason_code, round_id=round_id
            )
            log.error("Failed to plan lineup jobs for round %s: %s", round_id, exc.reason_code)
            continue
        log.debug(f"🕒 Round {round_id} first match: {round_start.isoformat()}")

        # T-1 day @ 5pm AWST
        day_before_5pm = round_start.replace(hour=17, minute=0, second=0, microsecond=0) - timedelta(days=1)
        add_registered_job(scheduler, run_lineup_round_scraper, trigger=DateTrigger(run_date=day_before_5pm), run_date=day_before_5pm, args=[round_id], job_id=lineup_round_job_id(round_id, "day_before_5pm"), job_type="lineup", round_id=round_id)
        log.info(f"📅 Scheduled: T-1 day 5pm for Round {round_id} → {day_before_5pm}")

        # If Thursday, also 5pm Thursday
        if round_start.weekday() == 3:  # Thursday
            thursday_5pm = round_start.replace(hour=17, minute=0, second=0, microsecond=0)
            add_registered_job(scheduler, run_lineup_round_scraper, trigger=DateTrigger(run_date=thursday_5pm), run_date=thursday_5pm, args=[round_id], job_id=lineup_round_job_id(round_id, "thursday_5pm"), job_type="lineup", round_id=round_id)
            log.info(f"📅 Scheduled: Thursday 5pm for Round {round_id} → {thursday_5pm}")

        # 1-hour-before each match
        cursor = conn.cursor()
        cursor.execute("""
            SELECT match_id, start_time_utc
            FROM matches
            WHERE round_id = ?
        """, (round_id,))
        match_rows = cursor.fetchall()
        log.debug(f"📦 Found {len(match_rows)} matches in Round {round_id} to schedule 1hr-before scrapes")

        for match_id, match_start_utc in match_rows:
            try:
                match_start = parse_metadata_timestamp(match_start_utc).astimezone(match_day_timezone())
            except MetadataTimestampError as exc:
                record_planning_failure(
                    lineup_match_job_id(match_id), "lineup", exc.reason_code, match_id=match_id
                )
                log.error("Failed to plan lineup job for match %s: %s", match_id, exc.reason_code)
                continue
            one_hour_before = match_start - timedelta(hours=1)
            add_registered_job(scheduler, run_lineup_match_scraper, trigger=DateTrigger(run_date=one_hour_before), run_date=one_hour_before, args=[match_id], job_id=lineup_match_job_id(match_id), job_type="lineup", match_id=match_id)
            log.debug(f"📅 Scheduled: 1 hour before match {match_id} → {one_hour_before}")

    conn.close()
    log.info("✅ Line-up scrape jobs registered successfully.")
