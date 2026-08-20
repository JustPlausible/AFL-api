# scheduler/start.py

from __future__ import annotations

import os
import signal
import threading
from contextlib import asynccontextmanager

# A container may receive SIGTERM while this module is still importing heavy
# scheduler dependencies.  Record it immediately so direct module execution can
# exit cleanly instead of dying with the platform's default signal status.
_early_stop_requested = False
if __name__ == "__main__":
    def _record_early_stop(signum, frame):  # pragma: no cover - timing dependent
        global _early_stop_requested
        _early_stop_requested = True
    signal.signal(signal.SIGTERM, _record_early_stop)

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
)
from apscheduler.schedulers.base import STATE_STOPPED
from fastapi import FastAPI

from db.migration_runner import migrate_database
from health import router as health_router
from scheduler import scheduled_tasks  # force import to register cron jobs
from scheduler.api import app as scheduler_api
from scheduler.registry import (
    fixture_job_id,
    injury_job_id,
    reconcile_scheduler,
    refresh_job_id,
    upsert_job,
)
from scheduler.schedule_lineup_scrapes import register_lineup_jobs
from scheduler.schedule_match_scrapes import (
    register_live_match_day_scraper,
    register_match_scrape_jobs,
)
from scheduler.schedule_refresh_jobs import register_refresh_jobs
from scheduler.schedule_stat_scrapes import (
    register_live_stat_scrapers,
    register_stat_scrape_jobs,
)
from scheduler.scheduled_tasks import scheduler
from scheduler.write_lane import write_lane
from scheduler.player_stat_polling import shutdown_player_stat_polling_worker
from scheduler.match_state_capture import (
    register_match_state_capture_job,
    shutdown_match_state_capture_client,
)
from scheduler.match_windows import MatchWindowSettings, reconcile as reconcile_match_windows
from scheduler.recovery import reconcile_interrupted_attempts
from scheduler.runtime import establish_instance, mark_graceful_shutdown
from utils.log import setup_logger

SUPPORTED_UVICORN_COMMAND = "python -m uvicorn scheduler.start:app --host 0.0.0.0 --port 8000"

log = setup_logger("scheduler_start", "scheduler_start.log")
scheduler_log = setup_logger("scheduler_jobs", "scheduler_jobs.log")

log.debug("🟢 scheduler/start.py loaded!")

_bootstrap_lock = threading.Lock()
_jobs_registered = False
_scheduler_thread: threading.Thread | None = None
WRITE_LANE_DRAIN_TIMEOUT_SECONDS = 30.0


def _reconcile_match_windows_startup() -> None:
    try:
        write_lane.execute("match_windows.reconcile_startup", "startup", lambda conn: reconcile_match_windows(conn, settings=MatchWindowSettings.from_config()))
    except FileNotFoundError:
        log.warning("Match-window startup reconciliation skipped because the database is unavailable after migration")


def _recover_interrupted_attempts_startup() -> None:
    report = reconcile_interrupted_attempts(trigger_source="startup")
    log.info("Interrupted-attempt reconciliation summary: %s", report.to_dict())


def _validate_single_scheduler_configuration() -> None:
    replicas = int(os.environ.get("AFL_SCHEDULER_REPLICAS", "1"))
    if replicas != 1:
        raise RuntimeError("Unsupported deployment: AFL_SCHEDULER_REPLICAS must be exactly 1 for SQLite")


# 🔁 Register all dynamic (non-cron) jobs
def register_all_jobs():
    log.info("🧠 Registering dynamic scrape jobs...")
    register_lineup_jobs(scheduler)
    register_stat_scrape_jobs(scheduler)
    register_refresh_jobs(scheduler)
    register_live_stat_scrapers(scheduler)
    register_match_scrape_jobs(scheduler)
    register_live_match_day_scraper(scheduler)
    register_match_state_capture_job(scheduler)
    upsert_job(injury_job_id(), "injury", None, trigger_type="cron", func_ref="scheduler.scheduled_tasks:daily_injury_scrape")
    upsert_job(fixture_job_id(), "fixture", None, trigger_type="cron", func_ref="scheduler.scheduled_tasks:daily_fixture_scrape")
    upsert_job(refresh_job_id("matches_daily"), "general_refresh", None, trigger_type="cron", func_ref="scheduler.scheduled_tasks:daily_match_scrape")
    upsert_job(refresh_job_id("check_match_day"), "general_refresh", None, trigger_type="cron", func_ref="scheduler.scheduled_tasks:check_for_match_day")
    log.info("🔁 Reconciled persisted scheduler registry: %s", reconcile_scheduler(scheduler))


def bootstrap_scheduler() -> None:
    """Run one-time scheduler startup work without starting APScheduler twice."""
    global _jobs_registered
    with _bootstrap_lock:
        _validate_single_scheduler_configuration()
        if _jobs_registered:
            log.info("Scheduler bootstrap already completed; skipping duplicate registration.")
            return
        migrate_database()
        establish_instance()
        _recover_interrupted_attempts_startup()
        _reconcile_match_windows_startup()
        register_all_jobs()
        _jobs_registered = True


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
    EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED,
)


def start_scheduler_blocking() -> None:
    """Start APScheduler in the current thread and block until shutdown."""
    if scheduler.running:
        log.info("APScheduler is already running; not starting a duplicate instance.")
        return
    bootstrap_scheduler()
    log.info("📆 Starting APScheduler in blocking standalone mode...")
    scheduler.start()


def start_scheduler_for_app() -> None:
    """Start APScheduler for the FastAPI/Uvicorn lifecycle in one managed thread."""
    global _scheduler_thread
    with _bootstrap_lock:
        if scheduler.running:
            log.info("APScheduler is already running; not starting a duplicate instance.")
            return
        if _scheduler_thread and _scheduler_thread.is_alive():
            log.info("APScheduler thread is already alive; not starting a duplicate instance.")
            return
        _validate_single_scheduler_configuration()
        if not _jobs_registered:
            migrate_database()
            establish_instance()
            _recover_interrupted_attempts_startup()
            _reconcile_match_windows_startup()
            register_all_jobs()
            globals()["_jobs_registered"] = True
        log.info("📆 Starting APScheduler background thread for FastAPI lifecycle...")
        _scheduler_thread = threading.Thread(target=scheduler.start, name="apscheduler", daemon=True)
        _scheduler_thread.start()


def shutdown_scheduler(wait: bool = True) -> None:
    """Stop APScheduler and wait for executors so interpreter shutdown is clean."""
    shutdown_player_stat_polling_worker()
    shutdown_match_state_capture_client()
    if scheduler.state != STATE_STOPPED:
        log.info("🛑 Shutting down APScheduler...")
        scheduler.shutdown(wait=wait)
        mark_graceful_shutdown()
    if wait and not write_lane.drain(timeout=WRITE_LANE_DRAIN_TIMEOUT_SECONDS):
        raise RuntimeError("Scheduler write lane did not drain")


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler_for_app()
    try:
        yield
    finally:
        shutdown_scheduler(wait=True)


# 🌐 FastAPI app for live job inspection
app = FastAPI(lifespan=lifespan)
app.include_router(health_router)
app.mount("/", scheduler_api)


def _install_shutdown_handlers() -> threading.Event:
    stop_event = threading.Event()

    def request_shutdown(signum, frame):  # pragma: no cover - exercised via subprocess
        log.info("Received signal %s; requesting scheduler shutdown.", signum)
        stop_event.set()
        shutdown_scheduler(wait=True)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    return stop_event


def main() -> int:
    """Run `python -m scheduler.start` as a blocking standalone scheduler."""
    _install_shutdown_handlers()
    if _early_stop_requested:
        return 0
    try:
        start_scheduler_blocking()
    except (KeyboardInterrupt, SystemExit):
        shutdown_scheduler(wait=True)
    finally:
        shutdown_scheduler(wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
