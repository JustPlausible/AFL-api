# scheduler/schedule_refresh_jobs.py

from apscheduler.triggers.interval import IntervalTrigger
from utils.log import log
from scheduler.scheduled_tasks import scheduler
from scraper.scrape_afl_players import scrape_afl_stats_leaderboard
from scheduler.registry import add_registered_job, refresh_job_id

def register_refresh_jobs(scheduler):
    log("🔁 Registering periodic refresh jobs...", "DEBUG")

    add_registered_job(
        scheduler, scrape_afl_stats_leaderboard, trigger=IntervalTrigger(days=5), args=[],
        job_id=refresh_job_id("players"), job_type="general_refresh",
        name="Refresh Players every 5 days", replace_existing=True, trigger_type="interval"
    )
