# scheduler/api.py
import sqlite3
from datetime import datetime

from fastapi import FastAPI, HTTPException, Response, status
from apscheduler.jobstores.base import JobLookupError
from fastapi import APIRouter
from pydantic import BaseModel
from scheduler.schedule_stat_scrapes import register_stat_scrape_jobs
from scraper.scrape_afl_fixtures import update_fixture_cache
from scheduler.scheduled_tasks import scheduler  # same scheduler you already use
from scheduler.schedule_refresh_jobs import register_refresh_jobs
from scheduler.schedule_lineup_scrapes import register_lineup_jobs
from scheduler.registry import registry_rows
from db.connection import get_read_only_db_connection
from scheduler.match_windows import inspection_rows
from utils.log import log
from scheduler.manual_triggers import router as manual_triggers_router
from version import __version__

log("🔍 scheduler/api.py loaded", "DEBUG")

app = FastAPI(title="Scheduler API")
app.include_router(manual_triggers_router)

# Stable top-level states for /scheduler/health (Issue #178). An empty job
# registry is never by itself a reason to report anything other than
# "healthy" -- these constants intentionally say nothing about job_count.
SCHEDULER_STATE_HEALTHY = "healthy"
SCHEDULER_STATE_STARTING = "starting"
SCHEDULER_STATE_UNHEALTHY = "unhealthy"

# Stable, sanitized diagnostic codes. Never derived from exception text so a
# raw error message (which could carry a path, DSN, or other operational
# detail) can never leak through this contract.
DIAGNOSTIC_DATABASE_UNAVAILABLE = "database_unavailable"
DIAGNOSTIC_REGISTRY_UNREADABLE = "registry_unreadable"
DIAGNOSTIC_SCHEDULER_NOT_RUNNING = "scheduler_not_running"


class SchedulerHealthResponse(BaseModel):
    """Small, stable scheduler health/readiness contract.

    Deliberately excludes scheduler internals (job payloads, credentials,
    exception text, filesystem paths). ``job_count`` is informational only;
    ``job_count == 0`` with ``state == "healthy"`` is a normal, healthy
    outcome, not a degraded one.
    """

    state: str
    scheduler_running: bool
    database_accessible: bool
    registry_accessible: bool
    job_count: int
    diagnostics: list[str]
    version: str


@app.get("/scheduler/health", response_model=SchedulerHealthResponse)
def scheduler_health(response: Response) -> SchedulerHealthResponse:
    """Read-only scheduler health/status contract (Issue #178).

    Required-for-readiness dependencies are the application database and the
    persisted job registry table it hosts -- both are load-bearing for every
    other scheduler endpoint. Optional runtime data (match windows, CFS
    polling state) is intentionally not consulted here.
    """
    diagnostics: list[str] = []

    database_accessible = True
    conn = None
    try:
        conn = get_read_only_db_connection()
        conn.execute("SELECT 1").fetchone()
    except (FileNotFoundError, sqlite3.Error):
        database_accessible = False
        diagnostics.append(DIAGNOSTIC_DATABASE_UNAVAILABLE)
    finally:
        if conn is not None:
            conn.close()

    registry_accessible = True
    try:
        registry_rows()
    except (FileNotFoundError, sqlite3.Error):
        registry_accessible = False
        diagnostics.append(DIAGNOSTIC_REGISTRY_UNREADABLE)

    scheduler_running = scheduler.running
    job_count = len(scheduler.get_jobs())

    if not database_accessible or not registry_accessible:
        state = SCHEDULER_STATE_UNHEALTHY
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif not scheduler_running:
        state = SCHEDULER_STATE_STARTING
        diagnostics.append(DIAGNOSTIC_SCHEDULER_NOT_RUNNING)
    else:
        state = SCHEDULER_STATE_HEALTHY

    return SchedulerHealthResponse(
        state=state,
        scheduler_running=scheduler_running,
        database_accessible=database_accessible,
        registry_accessible=registry_accessible,
        job_count=job_count,
        diagnostics=diagnostics,
        version=__version__,
    )

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


@app.get("/scheduler/match-windows")
def list_match_windows():
    conn = get_read_only_db_connection()
    try:
        return inspection_rows(conn)
    finally:
        conn.close()


@app.get("/scheduler/match-state-evidence")
def match_state_evidence(match_id: int | None = None, match_provider_id: str | None = None,
                          transitions_only: bool = False, limit: int = 500):
    """Read-only diagnostic evidence for Issue #148 (opt-in capture only)."""
    from scheduler.match_state_capture import MatchStateCaptureSettings
    from collection.match_state_evidence import evidence_rows
    settings = MatchStateCaptureSettings.from_config()
    conn = get_read_only_db_connection()
    try:
        rows = evidence_rows(
            conn, match_id=match_id, match_provider_id=match_provider_id,
            transitions_only=transitions_only, limit=limit,
        )
    finally:
        conn.close()
    return {
        "enabled": settings.enabled,
        "interval_seconds": settings.interval_seconds,
        "kickoff_tolerance_seconds": settings.kickoff_tolerance_seconds,
        "post_live_grace_seconds": settings.post_live_grace_seconds,
        "observations": rows,
    }


@app.get("/scheduler/player-stat-polling")
def polling_status():
    from scheduler.player_stat_polling import get_player_stat_polling_worker
    worker = get_player_stat_polling_worker()
    settings = worker.settings
    conn = get_read_only_db_connection()
    try:
        rows = inspection_rows(conn)
    finally:
        conn.close()
    return {
        "read_only": True,
        "operational": worker.status(),
        "enabled": settings.enabled,
        "kill_switch": settings.kill_switch,
        "drain": settings.drain,
        "max_workers": settings.max_workers,
        "network_concurrency": settings.network_concurrency,
        "live_cadence_seconds": int(settings.live_cadence.total_seconds()),
        "jitter_seconds": settings.jitter_seconds,
        "allowed_competitions": settings.allowed_competitions,
        "allowed_seasons": settings.allowed_seasons,
        "allowed_matches": settings.allowed_matches,
        "windows": rows,
    }
