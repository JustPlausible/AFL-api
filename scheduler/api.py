from fastapi import FastAPI, HTTPException
from apscheduler.jobstores.base import JobLookupError
from scheduler.scheduled_tasks import scheduler  # same scheduler you already use
from utils.log import log

log("🔍 scheduler/api.py loaded", "DEBUG")

app = FastAPI(title="Scheduler API")

@app.get("/scheduler/jobs")
def list_jobs():
    jobs = scheduler.get_jobs()
    return [{
        "id": job.id,
        "func": str(job.func_ref),
        "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
        "trigger": str(job.trigger)
    } for job in jobs]

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
