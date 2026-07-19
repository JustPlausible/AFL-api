# scheduler/start.py

from fastapi import FastAPI
import threading
from apscheduler.events import (
    EVENT_JOB_EXECUTED,
    EVENT_JOB_ERROR,
    EVENT_JOB_MISSED,
)
from scheduler.scheduled_tasks import scheduler  # Just defines scheduler + decorators
from scheduler import scheduled_tasks  # 👈 force import to register cron jobs
from scheduler.schedule_refresh_jobs import register_refresh_jobs
from scheduler.schedule_lineup_scrapes import register_lineup_jobs
from scheduler.schedule_stat_scrapes import register_stat_scrape_jobs, register_live_stat_scrapers
from scheduler.schedule_match_scrapes import register_match_scrape_jobs, register_live_match_day_scraper
from scheduler.api import app as scheduler_api
from health import router as health_router
from utils.log import setup_logger

log = setup_logger("scheduler_start", "scheduler_start.log")
scheduler_log = setup_logger("scheduler_jobs", "scheduler_jobs.log")

log.debug("🟢 scheduler/start.py loaded!")

# 🔁 Register all dynamic (non-cron) jobs
def register_all_jobs():
    log.info("🧠 Registering dynamic scrape jobs...")
    register_lineup_jobs(scheduler)
    register_stat_scrape_jobs(scheduler)
    register_refresh_jobs(scheduler)
    register_live_stat_scrapers(scheduler)
    register_match_scrape_jobs(scheduler)
    register_live_match_day_scraper(scheduler)

# ▶ Start APScheduler in a separate thread
def start_scheduler():
    log.info("📆 Starting APScheduler background thread...")
    scheduler.start()

def scheduler_listener(event):
    job_id = getattr(event, "job_id", "unknown")
    if event.code == EVENT_JOB_EXECUTED:
        scheduler_log.info(f"✅ Job '{job_id}' executed successfully.")
    elif event.code == EVENT_JOB_ERROR:
        scheduler_log.error(f"❌ Job '{job_id}' failed: {event.exception}")
    elif event.code == EVENT_JOB_MISSED:
        scheduler_log.warning(f"⚠️ Job '{job_id}' MISSED its scheduled time.")

scheduler.add_listener(
    scheduler_listener,
    EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED
)

# 🌐 FastAPI app for live job inspection
app = FastAPI()
app.include_router(health_router)
app.mount("/", scheduler_api)

# 🟢 Bootstrap the job system
register_all_jobs()
threading.Thread(target=start_scheduler, daemon=True).start()
