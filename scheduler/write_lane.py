"""Process-local serial persistence lane for the single Scheduler instance."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import TypeVar

from db.connection import get_db_connection

T = TypeVar("T")
logger = logging.getLogger("scheduler.write_lane")
DIAGNOSTIC_EVENT = "scheduler_write"


def _safe_log(level: str, message: str, *args, **kwargs) -> None:
    """Diagnostics must never replace a persistence result or exception."""
    try:
        getattr(logger, level)(message, *args, **kwargs)
    except Exception:  # pragma: no cover - defensive against custom handlers
        pass


class WriteLaneClosed(RuntimeError):
    pass


class NestedWriteLaneError(RuntimeError):
    pass


class SchedulerWriteLane:
    """Serialize Scheduler callbacks; callbacks receive a fresh owned connection.

    The lane commits on return, rolls back on exception, closes the connection,
    and propagates the callback's return value or exception.  Callbacks must be
    bounded persistence only: HTTP and parsing belong before ``execute``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = threading.Condition()
        self._local = threading.local()
        self._accepting = True
        self._pending = 0
        self._active = 0

    @property
    def pending_count(self) -> int:
        with self._state:
            return self._pending

    @property
    def active_count(self) -> int:
        with self._state:
            return self._active

    def execute(self, operation_name: str, target_id: object, callback: Callable[[sqlite3.Connection], T]) -> T:
        return self._execute(operation_name, target_id, callback, begin_immediate=False)

    def execute_immediate(self, operation_name: str, target_id: object, callback: Callable[[sqlite3.Connection], T]) -> T:
        """Run a bounded callback inside a lane-owned BEGIN IMMEDIATE transaction."""
        return self._execute(operation_name, target_id, callback, begin_immediate=True)

    def _execute(self, operation_name: str, target_id: object, callback: Callable[[sqlite3.Connection], T], *, begin_immediate: bool) -> T:
        if getattr(self._local, "active", False):
            raise NestedWriteLaneError("nested Scheduler write-lane use is not allowed")
        queued_at = time.monotonic()
        with self._state:
            if not self._accepting:
                raise WriteLaneClosed("Scheduler write lane is draining")
            self._pending += 1
            queued_ahead = self._pending - 1
        with self._lock:
            wait_ms = (time.monotonic() - queued_at) * 1000
            with self._state:
                self._pending -= 1
                self._active += 1
            started = time.monotonic()
            conn = None
            outcome = "success"
            failure_class = None
            self._local.active = True
            try:
                conn = get_db_connection()
                if begin_immediate:
                    conn.execute("BEGIN IMMEDIATE")
                result = callback(conn)
                conn.commit()
                return result
            except Exception as exc:
                outcome = "rollback"
                failure_class = "sqlite_busy" if isinstance(exc, sqlite3.OperationalError) and ("locked" in str(exc).lower() or "busy" in str(exc).lower()) else "application"
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        _safe_log("exception", "scheduler_write rollback_failure operation=%s target_id=%s", operation_name, target_id)
                raise
            finally:
                duration_ms = (time.monotonic() - started) * 1000
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        _safe_log("exception", "scheduler_write connection_close_failure")
                self._local.active = False
                with self._state:
                    self._active -= 1
                    self._state.notify_all()
                fields = {
                    "event": DIAGNOSTIC_EVENT, "operation": operation_name,
                    "target_id": str(target_id), "lane_wait_ms": wait_ms,
                    "transaction_ms": duration_ms, "result": outcome,
                    "failure_class": failure_class, "retry": False,
                    "queued_writers": queued_ahead,
                }
                _safe_log("info", "scheduler_write", extra={"scheduler_write": fields})

    def drain(self, timeout: float | None = None) -> bool:
        """Reject new work and wait for claimed/queued work to finish."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._state:
            self._accepting = False
            while self._active or self._pending:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    _safe_log("error", "scheduler_write drain_timeout", extra={
                        "scheduler_write": {"event": "scheduler_write_drain", "result": "timeout",
                                            "active_writers": self._active,
                                            "queued_writers": self._pending}})
                    return False
                self._state.wait(remaining)
            return True


write_lane = SchedulerWriteLane()
