# scheduler/scheduled_tasks.py

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
from subprocess import Popen
from utils.log import log
import pytz
import sys

local_tz = pytz.timezone("Australia/Perth")

scheduler = BlockingScheduler(
    jobstores={'default': MemoryJobStore()},
    executors={'default': ThreadPoolExecutor(5)},
    timezone=local_tz
)

# Static daily job example
@scheduler.scheduled_job(CronTrigger(hour=11, minute=0))  # 11:00 AM AWST
def daily_injury_scrape():
    now = datetime.now(local_tz).isoformat()
    log(f"📅 [Daily] Injury scrape @ {now}", "INFO")
    Popen([sys.executable, "-m", "scraper.scrape_afl_injuries"])
