# scheduler/schedule_refresh_jobs.py

from apscheduler.triggers.interval import IntervalTrigger
from utils.log import log
from scheduler.scheduled_tasks import scheduler
from scraper.scrape_afl_players import scrape_afl_stats_leaderboard

def register_refresh_jobs(scheduler):
    log("🔁 Registering periodic refresh jobs...", "DEBUG")

    scheduler.add_job(
        scrape_afl_stats_leaderboard,
        trigger=IntervalTrigger(days=5),
        id="refresh_players",
        name="Refresh Players every 5 days",
        replace_existing=True
    )
