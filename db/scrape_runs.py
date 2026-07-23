"""Shared audit helpers for scraper run lifecycle records."""
from __future__ import annotations

import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from db.connection import get_db_connection

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUSES = {STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED}

TRIGGER_CLI = "cli"
TRIGGER_SCHEDULER = "scheduler"
TRIGGER_ADMIN_MANUAL = "admin_manual"
TRIGGER_STARTUP_RECOVERY = "startup_recovery"
TRIGGER_SOURCES = {TRIGGER_CLI, TRIGGER_SCHEDULER, TRIGGER_ADMIN_MANUAL, TRIGGER_STARTUP_RECOVERY}

MAX_ERROR_SUMMARY_LENGTH = 500
_REDACTED = "<redacted>"
_SECRET_QUERY_KEYS = re.compile(r"(?i)(api[_-]?key|access[_-]?token|auth|authorization|bearer|code|cookie|password|secret|session|signature|sig|token)")
_SECRET_ASSIGNMENTS = re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|auth(?:orization)?|bearer[_-]?token|cookie|password|secret|session(?:id)?|token)\b\s*[:=]\s*([^\s,;]+)")
_AUTH_HEADER = re.compile(r"(?i)\b(authorization|cookie|set-cookie)\s*:\s*[^\n\r]+")
_DB_PASSWORD = re.compile(r"(?i)(://[^\s:/]+:)([^@\s]+)(@)")


@dataclass(frozen=True)
class ScrapeRun:
    run_id: str
    scrape_type: str
    target_type: str | None
    target_identifier: str | None
    trigger_source: str
    status: str
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    rows_read: int | None
    rows_written: int | None
    error_class: str | None
    error_summary: str | None
    correlation_id: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def infer_trigger_source(trigger_source: str | None = None, correlation_id: str | None = None) -> str:
    if trigger_source:
        return trigger_source
    return TRIGGER_SCHEDULER if (correlation_id or os.getenv("AFL_SCHEDULER_JOB_ID")) else TRIGGER_CLI


def infer_correlation_id(correlation_id: str | None = None) -> str | None:
    return correlation_id or os.getenv("AFL_SCHEDULER_JOB_ID") or None


def validate_status(status: str) -> str:
    if status not in STATUSES:
        raise ValueError(f"Unsupported scrape run status: {status}")
    return status


def validate_trigger_source(trigger_source: str) -> str:
    if trigger_source not in TRIGGER_SOURCES:
        raise ValueError(f"Unsupported scrape run trigger_source: {trigger_source}")
    return trigger_source


def sanitize_error_summary(error: BaseException | str, max_length: int = MAX_ERROR_SUMMARY_LENGTH) -> str:
    text = str(error) if isinstance(error, str) else (str(error) or error.__class__.__name__)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}: {_REDACTED}", text)
    text = _SECRET_ASSIGNMENTS.sub(lambda m: f"{m.group(1)}={_REDACTED}", text)
    text = _DB_PASSWORD.sub(lambda m: f"{m.group(1)}{_REDACTED}{m.group(3)}", text)
    text = _redact_secret_query_params(text)
    if len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text


def _redact_secret_query_params(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        url = match.group(0)
        parts = urlsplit(url)
        query = urlencode([(k, _REDACTED if _SECRET_QUERY_KEYS.search(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, "" if query != parts.query else parts.fragment))
    return re.sub(r"https?://[^\s'\"<>]+", repl, text)


def _conn(conn: sqlite3.Connection | None) -> tuple[sqlite3.Connection, bool]:
    return (conn, False) if conn is not None else (get_db_connection(), True)


def start_scrape_run(scrape_type: str, *, target_type: str | None = None, target_identifier: Any = None, trigger_source: str | None = None, correlation_id: str | None = None, conn: sqlite3.Connection | None = None) -> str:
    db, close = _conn(conn)
    run_id = str(uuid.uuid4())
    trigger = validate_trigger_source(infer_trigger_source(trigger_source, correlation_id))
    corr = infer_correlation_id(correlation_id)
    try:
        db.execute("""
            INSERT INTO scrape_runs (run_id, scrape_type, target_type, target_identifier, trigger_source, status, started_at, correlation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, scrape_type, target_type, None if target_identifier is None else str(target_identifier), trigger, STATUS_RUNNING, utc_now(), corr))
        db.commit()
        return run_id
    finally:
        if close:
            db.close()


def _finish(run_id: str, status: str, *, rows_read=None, rows_written=None, error_class=None, error_summary=None, conn=None) -> None:
    db, close = _conn(conn)
    finished = utc_now()
    try:
        cur = db.execute("SELECT started_at FROM scrape_runs WHERE run_id=? AND status=?", (run_id, STATUS_RUNNING))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No running scrape run found for run_id={run_id}")
        started = datetime.fromisoformat(row[0])
        duration_ms = max(0, int((datetime.fromisoformat(finished) - started).total_seconds() * 1000))
        updated = db.execute("""
            UPDATE scrape_runs SET status=?, finished_at=?, duration_ms=?, rows_read=?, rows_written=?, error_class=?, error_summary=?
            WHERE run_id=? AND status=?
        """, (status, finished, duration_ms, rows_read, rows_written, error_class, error_summary, run_id, STATUS_RUNNING))
        if updated.rowcount != 1:
            raise ValueError(f"No running scrape run found for run_id={run_id}")
        db.commit()
    finally:
        if close:
            db.close()


def complete_scrape_run(run_id: str, *, rows_read: int | None = None, rows_written: int | None = None, conn: sqlite3.Connection | None = None) -> None:
    _finish(run_id, STATUS_COMPLETED, rows_read=rows_read, rows_written=rows_written, conn=conn)


def fail_scrape_run(run_id: str, exc: BaseException | str, *, conn: sqlite3.Connection | None = None) -> None:
    cls = exc.__class__.__name__ if isinstance(exc, BaseException) else "Error"
    _finish(run_id, STATUS_FAILED, error_class=cls, error_summary=sanitize_error_summary(exc), conn=conn)


def recent_scrape_runs(*, limit: int = 50, scrape_type: str | None = None, status: str | None = None, conn: sqlite3.Connection | None = None) -> list[ScrapeRun]:
    if status is not None:
        validate_status(status)
    db, close = _conn(conn)
    try:
        clauses, params = [], []
        if scrape_type:
            clauses.append("scrape_type=?"); params.append(scrape_type)
        if status:
            clauses.append("status=?"); params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        cur = db.execute(f"SELECT * FROM scrape_runs{where} ORDER BY started_at DESC LIMIT ?", (*params, limit))
        return [ScrapeRun(**dict(zip([d[0] for d in cur.description], row))) for row in cur.fetchall()]
    finally:
        if close:
            db.close()


def recover_stale_running_runs(*, older_than: datetime, reason: str = "Startup recovery marked stale running scrape as failed", conn: sqlite3.Connection | None = None) -> int:
    db, close = _conn(conn)
    finished = utc_now()
    if older_than.tzinfo is None:
        older_than = older_than.replace(tzinfo=timezone.utc)
    try:
        rows = db.execute("SELECT run_id, started_at FROM scrape_runs WHERE status=? AND started_at < ?", (STATUS_RUNNING, older_than.astimezone(timezone.utc).isoformat())).fetchall()
        for row in rows:
            started = datetime.fromisoformat(row[1])
            duration_ms = max(0, int((datetime.fromisoformat(finished) - started).total_seconds() * 1000))
            db.execute("UPDATE scrape_runs SET status=?, finished_at=?, duration_ms=?, error_class=?, error_summary=? WHERE run_id=? AND status=?", (STATUS_FAILED, finished, duration_ms, "StaleScrapeRun", sanitize_error_summary(reason), row[0], STATUS_RUNNING))
        db.commit()
        return len(rows)
    finally:
        if close:
            db.close()


@contextmanager
def audited_scrape_run(scrape_type: str, *, target_type: str | None = None, target_identifier: Any = None, trigger_source: str | None = None, correlation_id: str | None = None, conn: sqlite3.Connection | None = None) -> Iterator[dict[str, Any]]:
    if os.getenv("AFL_SCRAPE_RUN_ACTIVE"):
        yield {"run_id": None, "rows_read": None, "rows_written": None}
        return
    try:
        run_id = start_scrape_run(scrape_type, target_type=target_type, target_identifier=target_identifier, trigger_source=trigger_source, correlation_id=correlation_id, conn=conn)
    except FileNotFoundError:
        run_id = None
    counts: dict[str, Any] = {"run_id": run_id, "rows_read": None, "rows_written": None}
    previous_active = os.environ.get("AFL_SCRAPE_RUN_ACTIVE")
    os.environ["AFL_SCRAPE_RUN_ACTIVE"] = "1"
    try:
        yield counts
    except Exception as exc:
        if run_id is not None:
            fail_scrape_run(run_id, exc, conn=conn)
        raise
    else:
        if run_id is not None:
            complete_scrape_run(run_id, rows_read=counts.get("rows_read"), rows_written=counts.get("rows_written"), conn=conn)
    finally:
        if previous_active is None:
            os.environ.pop("AFL_SCRAPE_RUN_ACTIVE", None)
        else:
            os.environ["AFL_SCRAPE_RUN_ACTIVE"] = previous_active
