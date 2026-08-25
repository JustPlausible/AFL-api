# scheduler/scheduled_tasks.py

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from scheduler.schedule_match_scrapes import register_live_match_day_scraper
from datetime import datetime
from utils.log import setup_logger
import pytz
import config
from scheduler.registry import execute_registered_job, fixture_job_id, injury_job_id, refresh_job_id
from logging_sources import LOG_SOURCES

local_tz = pytz.timezone("Australia/Perth")
_source = LOG_SOURCES["scheduled_tasks"]
log = setup_logger(_source.logger_name, _source.filename)
log.info("✅ scheduled_tasks.py loaded and logger active")

scheduler = BlockingScheduler(
    jobstores={'default': MemoryJobStore()},
    executors={'default': ThreadPoolExecutor(5)},
    timezone=local_tz
)

# Static daily job for injury updates
@scheduler.scheduled_job(CronTrigger(hour=11, minute=0), id=injury_job_id(), name="Daily injury scrape")  # 11:00 AM AWST
def daily_injury_scrape():
    now = datetime.now(local_tz).isoformat()
    log.info(f"📅 [Daily] Injury scrape @ {now}")
    from collection.source_policy import OperationalDomain
    from scheduler.collection import collect_scheduled
    return execute_registered_job(injury_job_id(), lambda: collect_scheduled(OperationalDomain.INJURIES))

# Static daily job for fixture updates
@scheduler.scheduled_job(CronTrigger(hour=23, minute=0), id=fixture_job_id(), name="Daily fixture scrape")  # 11:00 PM AWST
def daily_fixture_scrape():
    now = datetime.now(local_tz).isoformat()
    log.info(f"📅 [Daily] Fixtures scrape triggered @ {now}")
    from collection.source_policy import OperationalDomain
    from scheduler.collection import collect_scheduled
    return execute_registered_job(fixture_job_id(), lambda: collect_scheduled(OperationalDomain.METADATA))

# Static daily job for match updates
@scheduler.scheduled_job(CronTrigger(hour=8, minute=0), id=refresh_job_id("matches_daily"), name="Daily match refresh")  # 8:00 AM AWST daily
def daily_match_scrape():
    log.info("🔥 daily_match_scrape triggered manually for test")
    now = datetime.now(local_tz).isoformat()
    log.info(f"📅 [Daily] Match scrape @ {now}")
    from collection.source_policy import OperationalDomain
    from scheduler.collection import collect_scheduled
    return execute_registered_job(refresh_job_id("matches_daily"), lambda: collect_scheduled(OperationalDomain.METADATA))

@scheduler.scheduled_job(CronTrigger(hour=9, minute=0), id=refresh_job_id("check_match_day"), name="Check for match-day scraper")  # 9:00 AM AWST
def check_for_match_day():
    return execute_registered_job(refresh_job_id("check_match_day"), register_live_match_day_scraper, scheduler)


@scheduler.scheduled_job(IntervalTrigger(seconds=config.AFL_SCHEDULER_HEARTBEAT_SECONDS), id="player_stat_polling_planner", name="CFS player-stat polling planner", max_instances=1, coalesce=True, misfire_grace_time=10)
def player_stat_polling_planner():
    from scheduler.player_stat_polling import get_player_stat_polling_worker
    from scheduler.runtime import heartbeat
    heartbeat()
    return get_player_stat_polling_worker().run_once()


@scheduler.scheduled_job(IntervalTrigger(seconds=config.AFL_COMMENTARY_PRODUCTION_INTERVAL_SECONDS), id="match_commentary_production", name="Production CFS match-commentary polling", max_instances=1, coalesce=True, misfire_grace_time=10)
def match_commentary_production_job():
    from scheduler.match_commentary_production import poll_match_commentary
    return poll_match_commentary()


@scheduler.scheduled_job(IntervalTrigger(seconds=config.AFL_INTERCHANGE_PRODUCTION_INTERVAL_SECONDS), id="match_interchange_production", name="Production CFS match-interchange polling", max_instances=1, coalesce=True, misfire_grace_time=10)
def match_interchange_production_job():
    from scheduler.match_interchange_production import poll_match_interchange
    return poll_match_interchange()


# Analytics retention/roll-up (Issue #205): the one piece of new scheduler
# infrastructure the analytics framework adds. A single shared daily job for
# every analytics resource/route -- adding a new analytics module never
# requires a second one. Scheduled off-peak, after the other daily jobs
# above. See analytics/rollup.py.
@scheduler.scheduled_job(CronTrigger(hour=4, minute=20), id="analytics_rollup", name="Analytics retention and roll-up")  # 4:20 AM AWST
def analytics_rollup_job():
    from analytics.rollup import run_rollup_and_retention
    return run_rollup_and_retention()


# Start the scheduler loop
if __name__ == "__main__":
    from scheduler.job_cleaner import clean_broken_jobs
    clean_broken_jobs()
    scheduler.start()
