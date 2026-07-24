# scheduler/api.py
from datetime import datetime

from fastapi import FastAPI, HTTPException
from apscheduler.jobstores.base import JobLookupError
from fastapi import APIRouter
from scheduler.schedule_stat_scrapes import register_stat_scrape_jobs
from scraper.scrape_afl_fixtures import update_fixture_cache
from scheduler.scheduled_tasks import scheduler  # same scheduler you already use
from scheduler.schedule_refresh_jobs import register_refresh_jobs
from scheduler.schedule_lineup_scrapes import register_lineup_jobs
from scheduler.registry import registry_rows
from utils.log import log
from scheduler.manual_triggers import router as manual_triggers_router

log("🔍 scheduler/api.py loaded", "DEBUG")

app = FastAPI(title="Scheduler API")
app.include_router(manual_triggers_router)

@app.get("/scheduler/jobs")
def list_jobs():
    jobs = scheduler.get_jobs()
    persisted = {row["job_id"]: row for row in registry_rows()}
    rows = []
    for job in jobs:
        row = persisted.get(job.id, {})
        rows.append({
            "id": job.id,
            "func": str(job.func_ref),
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
            "apscheduler_state": "scheduled" if job.next_run_time else "paused",
            "persisted": row or None,
            "persisted_status": row.get("status"),
            "persisted_job_type": row.get("job_type"),
            "persisted_last_attempt_time": row.get("last_attempt_time"),
            "persisted_last_success_time": row.get("last_success_time"),
            "persisted_attempt_count": row.get("attempt_count"),
            "persisted_last_error_summary": row.get("last_error_summary"),
        })
    memory_ids = {job.id for job in jobs}
    for job_id, row in persisted.items():
        if job_id not in memory_ids:
            rows.append({
                "id": job_id,
                "func": row.get("func_ref"),
                "next_run_time": None,
                "trigger": None,
                "apscheduler_state": "absent",
                "persisted": row,
                "persisted_status": row.get("status"),
                "persisted_job_type": row.get("job_type"),
                "persisted_last_attempt_time": row.get("last_attempt_time"),
                "persisted_last_success_time": row.get("last_success_time"),
                "persisted_attempt_count": row.get("attempt_count"),
                "persisted_last_error_summary": row.get("last_error_summary"),
            })
    return rows

@app.post("/scheduler/run/{job_id}")
def run_job(job_id: str):
    try:
        job = scheduler.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        job.modify(next_run_time=datetime.now(scheduler.timezone))  # triggers immediate run
        return {"message": f"Job {job_id} scheduled to run now"}
    except JobLookupError:
        raise HTTPException(status_code=404, detail="Job not found")

@app.delete("/scheduler/job/{job_id}")
def delete_job(job_id: str):
    try:
        scheduler.remove_job(job_id)
        return {"message": f"Job {job_id} deleted"}
    except JobLookupError:
        raise HTTPException(status_code=404, detail="Job not found")

@app.post("/scheduler/refresh")
def refresh_all_jobs():
    log("🔁 Manual refresh of all scheduler jobs triggered", "INFO")
    scheduler.remove_all_jobs()
    register_stat_scrape_jobs(scheduler)
    register_lineup_jobs(scheduler)
    register_refresh_jobs(scheduler)
    return {"status": "ok", "message": "All jobs re-registered"}
