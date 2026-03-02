# scheduler/job_cleaner.py

from scheduler.scheduled_tasks import scheduler
from utils.log import setup_logger

log = setup_logger("job_cleaner", "scheduled_tasks.log")

def clean_broken_jobs():
    broken_jobs = []
    for job in scheduler.get_jobs():
        if job.func is None:
            log.warning(f"🧹 Removing broken job: {job.id} (Function: None)")
            scheduler.remove_job(job.id)
            broken_jobs.append(job.id)

    if not broken_jobs:
        log.info("✅ No broken jobs found.")
    else:
        log.info(f"🗑️ Removed {len(broken_jobs)} broken job(s): {broken_jobs}")
