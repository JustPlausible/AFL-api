# scheduler/start.py

from fastapi import FastAPI
from db.migration_runner import migrate_database
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
from scheduler.registry import reconcile_scheduler, upsert_job, fixture_job_id, injury_job_id, refresh_job_id
from health import router as health_router
from utils.log import setup_logger

log = setup_logger("scheduler_start", "scheduler_start.log")
scheduler_log = setup_logger("scheduler_jobs", "scheduler_jobs.log")

log.debug("🟢 scheduler/start.py loaded!")
migrate_database()

# 🔁 Register all dynamic (non-cron) jobs
def register_all_jobs():
    log.info("🧠 Registering dynamic scrape jobs...")
    register_lineup_jobs(scheduler)
    register_stat_scrape_jobs(scheduler)
    register_refresh_jobs(scheduler)
    register_live_stat_scrapers(scheduler)
    register_match_scrape_jobs(scheduler)
    register_live_match_day_scraper(scheduler)
    upsert_job(injury_job_id(), "injury", None, trigger_type="cron", func_ref="scheduler.scheduled_tasks:daily_injury_scrape")
    upsert_job(fixture_job_id(), "fixture", None, trigger_type="cron", func_ref="scheduler.scheduled_tasks:daily_fixture_scrape")
    upsert_job(refresh_job_id("matches_daily"), "general_refresh", None, trigger_type="cron", func_ref="scheduler.scheduled_tasks:daily_match_scrape")
    upsert_job(refresh_job_id("check_match_day"), "general_refresh", None, trigger_type="cron", func_ref="scheduler.scheduled_tasks:check_for_match_day")
    log.info("🔁 Reconciled persisted scheduler registry: %s", reconcile_scheduler(scheduler))

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
