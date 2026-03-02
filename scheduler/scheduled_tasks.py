# scheduler/scheduled_tasks.py

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger
from scheduler.schedule_match_scrapes import register_live_match_day_scraper
from datetime import datetime
from subprocess import Popen
from utils.log import setup_logger
import pytz
import sys

local_tz = pytz.timezone("Australia/Perth")
log = setup_logger("scheduled_tasks", "scheduled_tasks.log")
log.info("✅ scheduled_tasks.py loaded and logger active")

scheduler = BlockingScheduler(
    jobstores={'default': MemoryJobStore()},
    executors={'default': ThreadPoolExecutor(5)},
    timezone=local_tz
)

# Static daily job for injury updates
@scheduler.scheduled_job(CronTrigger(hour=11, minute=0))  # 11:00 AM AWST
def daily_injury_scrape():
    now = datetime.now(local_tz).isoformat()
    log.info(f"📅 [Daily] Injury scrape @ {now}")
    Popen([sys.executable, "-m", "scraper.scrape_afl_injuries"])

# Static daily job for fixture updates
@scheduler.scheduled_job(CronTrigger(hour=23, minute=0))  # 11:00 PM AWST
def daily_fixture_scrape():
    now = datetime.now(local_tz).isoformat()
    log.info(f"📅 [Daily] Fixtures scrape triggered @ {now}")
    Popen([sys.executable, "-m", "scraper.scrape_afl_fixtures"])

# Static daily job for match updates
@scheduler.scheduled_job(CronTrigger(hour=8, minute=0))  # 8:00 AM AWST daily
def daily_match_scrape():
    log.info("🔥 daily_match_scrape triggered manually for test")
    now = datetime.now(local_tz).isoformat()
    log.info(f"📅 [Daily] Match scrape @ {now}")
    Popen([sys.executable, "-m", "scraper.scrape_afl_matches"])

@scheduler.scheduled_job(CronTrigger(hour=9, minute=0))  # 9:00 AM AWST
def check_for_match_day():
    register_live_match_day_scraper(scheduler)


# Start the scheduler loop
if __name__ == "__main__":
    from scheduler.job_cleaner import clean_broken_jobs
    clean_broken_jobs()
    scheduler.start()