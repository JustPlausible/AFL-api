# scheduler/start.py

from fastapi import FastAPI
import threading

from scheduler.scheduled_tasks import scheduler  # Just defines scheduler + decorators
from scheduler.schedule_lineup_scrapes import register_lineup_jobs
from scheduler.schedule_stat_scrapes import register_stat_scrape_jobs
from scheduler.api import app as scheduler_api
from utils.log import log

log("🟢 scheduler/start.py loaded!", "DEBUG")

# 🔁 Register all dynamic (non-cron) jobs
def register_all_jobs():
    log("🧠 Registering dynamic scrape jobs...", "INFO")
    register_lineup_jobs(scheduler)
    register_stat_scrape_jobs(scheduler)

# ▶ Start APScheduler in a separate thread
def start_scheduler():
    log("📆 Starting APScheduler background thread...", "INFO")
    scheduler.start()

# 🌐 FastAPI app for live job inspection
app = FastAPI()
app.mount("/", scheduler_api)

# 🟢 Bootstrap the job system
register_all_jobs()
threading.Thread(target=start_scheduler, daemon=True).start()
