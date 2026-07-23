"""Persistent scheduler job registry and restart reconciliation.

Persisted statuses are controlled and intentionally small:
* pending: planned by the application and eligible to run in the future.
* running: the common wrapper has started an attempt.
* succeeded: the wrapper observed successful completion; not auto-recovered.
* failed: the wrapper observed an exception or non-zero command result; not retried automatically.
* skipped: reconciliation decided a job is expired or otherwise unsafe to recover.

Recovery rule: only pending date-triggered jobs whose scheduled_run_time is still in the future
are re-registered when missing from APScheduler. Pending past jobs are marked skipped.
Succeeded and failed jobs are never automatically re-registered merely because they are absent.
"""
from __future__ import annotations

import importlib
import json
import re
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from apscheduler.triggers.date import DateTrigger

from db.connection import get_db_connection
from utils.log import setup_logger

log = setup_logger("scheduler_registry", "scheduler_registry.log")

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"
STATUSES = {PENDING, RUNNING, SUCCEEDED, FAILED, SKIPPED}
MAX_ERROR_LENGTH = 500


def stats_match_job_id(match_id: int) -> str: return f"stats_match_{int(match_id)}"
def lineup_match_job_id(match_id: int) -> str: return f"lineups_match_{int(match_id)}"
def lineup_round_job_id(round_id: int | str, slot: str) -> str: return f"lineups_round_{round_id}_{_slug(slot)}"
def match_refresh_job_id(match_id: int) -> str: return f"match_refresh_{int(match_id)}"
def fixture_job_id() -> str: return "fixtures_daily"
def injury_job_id() -> str: return "injuries_daily"
def refresh_job_id(name: str = "players") -> str: return f"refresh_{_slug(name)}"
def live_match_refresh_job_id() -> str: return "match_refresh_live"
def live_match_day_job_id() -> str: return "match_refresh_match_day"

def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_") or "general"

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

def _func_ref(func: Callable[..., Any]) -> str:
    return f"{func.__module__}:{func.__name__}"

def summarize_error(exc: BaseException | subprocess.CalledProcessError | str) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        msg = f"Command failed with exit code {exc.returncode}"
        stderr = getattr(exc, "stderr", None)
        if stderr:
            msg += f": {str(stderr).strip().splitlines()[-1]}"
    else:
        msg = str(exc) or exc.__class__.__name__ if not isinstance(exc, str) else exc
    msg = re.sub(r"(?i)(api[_-]?key|token|password|secret)=\S+", r"\1=<redacted>", msg)
    return msg[:MAX_ERROR_LENGTH]

def upsert_job(job_id: str, job_type: str, scheduled_run_time: datetime | None, *, match_id=None, round_id=None, status=PENDING, func_ref=None, args=None, trigger_type="date") -> None:
    if status not in STATUSES:
        raise ValueError(f"Unsupported scheduler job status: {status}")
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO scheduler_job_registry
              (job_id, job_type, match_id, round_id, scheduled_run_time, status, func_ref, args_json, trigger_type, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
              job_type=excluded.job_type,
              match_id=excluded.match_id,
              round_id=excluded.round_id,
              scheduled_run_time=excluded.scheduled_run_time,
              status=CASE WHEN scheduler_job_registry.status IN ('succeeded','failed') THEN scheduler_job_registry.status ELSE excluded.status END,
              func_ref=excluded.func_ref,
              args_json=excluded.args_json,
              trigger_type=excluded.trigger_type,
              updated_at=excluded.updated_at
        """, (job_id, job_type, match_id, None if round_id is None else str(round_id), _iso(scheduled_run_time), status, func_ref, json.dumps(args or []), trigger_type, utc_now()))
        conn.commit()
    finally:
        conn.close()

def mark_running(job_id: str) -> None:
    conn = get_db_connection(); now = utc_now()
    try:
        conn.execute("UPDATE scheduler_job_registry SET status=?, last_attempt_time=?, attempt_count=attempt_count+1, last_error_summary=NULL, updated_at=? WHERE job_id=?", (RUNNING, now, now, job_id)); conn.commit()
    finally: conn.close()

def mark_succeeded(job_id: str) -> None:
    conn = get_db_connection(); now = utc_now()
    try:
        conn.execute("UPDATE scheduler_job_registry SET status=?, last_success_time=?, last_error_summary=NULL, updated_at=? WHERE job_id=?", (SUCCEEDED, now, now, job_id)); conn.commit()
    finally: conn.close()

def mark_failed(job_id: str, exc: BaseException | str) -> None:
    conn = get_db_connection(); now = utc_now()
    try:
        conn.execute("UPDATE scheduler_job_registry SET status=?, last_error_summary=?, updated_at=? WHERE job_id=?", (FAILED, summarize_error(exc), now, job_id)); conn.commit()
    finally: conn.close()

def mark_skipped(job_id: str, reason: str) -> None:
    conn = get_db_connection(); now = utc_now()
    try:
        conn.execute("UPDATE scheduler_job_registry SET status=?, last_error_summary=?, updated_at=? WHERE job_id=? AND status != ?", (SKIPPED, summarize_error(reason), now, job_id, SUCCEEDED)); conn.commit()
    finally: conn.close()

def execute_registered_job(job_id: str, func: Callable[..., Any], *args: Any) -> Any:
    try:
        mark_running(job_id)
        previous_job_id = os.environ.get("AFL_SCHEDULER_JOB_ID")
        os.environ["AFL_SCHEDULER_JOB_ID"] = job_id
        try:
            result = func(*args)
        finally:
            if previous_job_id is None:
                os.environ.pop("AFL_SCHEDULER_JOB_ID", None)
            else:
                os.environ["AFL_SCHEDULER_JOB_ID"] = previous_job_id
        if isinstance(result, subprocess.CompletedProcess) and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, result.args, stderr=result.stderr)
        mark_succeeded(job_id)
        return result
    except Exception as exc:
        log.exception("Scheduler job %s failed", job_id)
        try: mark_failed(job_id, exc)
        except Exception: log.exception("Failed to persist failure for %s", job_id)
        raise

def add_registered_job(scheduler, func: Callable[..., Any], *, job_id: str, job_type: str, run_date: datetime | None = None, trigger=None, args=None, match_id=None, round_id=None, name=None, replace_existing=True, trigger_type="date", **kwargs):
    upsert_job(job_id, job_type, run_date, match_id=match_id, round_id=round_id, func_ref=_func_ref(func), args=args or [], trigger_type=trigger_type)
    wrapped_args = [job_id, func, *(args or [])]
    return scheduler.add_job(execute_registered_job, trigger=trigger or DateTrigger(run_date=run_date), args=wrapped_args, id=job_id, name=name, replace_existing=replace_existing, **kwargs)

def registry_rows() -> list[dict[str, Any]]:
    conn = get_db_connection(); conn.row_factory = None
    try:
        cur = conn.execute("SELECT job_id, job_type, match_id, round_id, scheduled_run_time, status, last_attempt_time, last_success_time, attempt_count, last_error_summary, func_ref, args_json, trigger_type FROM scheduler_job_registry ORDER BY scheduled_run_time, job_id")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally: conn.close()

def _load_func(ref: str) -> Callable[..., Any]:
    mod, name = ref.split(":", 1)
    return getattr(importlib.import_module(mod), name)

def reconcile_scheduler(scheduler, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    counts = {"re_registered": 0, "already_present": 0, "skipped": 0, "ignored": 0, "errors": 0}
    for row in registry_rows():
        try:
            if scheduler.get_job(row["job_id"]):
                counts["already_present"] += 1; continue
            status = row["status"]
            if status != PENDING:
                counts["ignored"] += 1; continue
            if row["trigger_type"] != "date" or not row["scheduled_run_time"] or not row["func_ref"]:
                mark_skipped(row["job_id"], "Recovery skipped: only pending future date jobs are eligible")
                counts["skipped"] += 1; continue
            run_date = datetime.fromisoformat(row["scheduled_run_time"])
            if run_date.tzinfo is None: run_date = run_date.replace(tzinfo=timezone.utc)
            if run_date <= now:
                mark_skipped(row["job_id"], "Recovery skipped: scheduled run time is in the past")
                counts["skipped"] += 1; continue
            add_registered_job(scheduler, _load_func(row["func_ref"]), job_id=row["job_id"], job_type=row["job_type"], run_date=run_date, args=json.loads(row["args_json"] or "[]"), match_id=row["match_id"], round_id=row["round_id"], name=f"Recovered {row['job_id']}")
            counts["re_registered"] += 1
        except Exception:
            counts["errors"] += 1
            log.exception("Failed to reconcile scheduler registry row %s", row.get("job_id"))
    return counts
