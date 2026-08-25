"""Failure-isolated analytics recording (Issue #205).

This is the only module a collector or API route needs to import.
:func:`record_upstream_poll` and :func:`record_consumer_request` are the
whole public surface: build an observation from facts you already have and
call the matching function. Both:

* return immediately -- the observation is handed to a bounded in-memory
  queue (``queue.put_nowait``) and a single background daemon thread does
  the actual SQLite write, so a slow or momentarily-locked database can
  never add latency to the collector or the API request being described;
* never raise -- disabled-by-config, a full queue, and any storage failure
  are all handled the same way: the observation is dropped and counted in
  :func:`dropped_observation_count`, never propagated to the caller.

This is a deliberately simple fire-and-forget design, not a general
task-queue abstraction: one queue, one worker thread, two message shapes.
See ``docs/analytics_framework.md`` "Analytics overhead" for the measured
cost of a call to either function.
"""

from __future__ import annotations

import queue
import threading
from datetime import datetime, timezone

import config
from analytics import storage
from analytics.contracts import ConsumerRequestObservation, UpstreamOutcome, UpstreamPollObservation
from db.connection import get_db_connection
from logging_sources import LOG_SOURCES
from utils.log import setup_logger

_source = LOG_SOURCES["analytics"]
log = setup_logger(_source.logger_name, _source.filename)

_UPSTREAM = "upstream"
_CONSUMER = "consumer"

_queue: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=max(1, config.AFL_ANALYTICS_QUEUE_MAX_SIZE))
_worker_lock = threading.Lock()
_worker_started = False
_dropped_count = 0
_dropped_lock = threading.Lock()
_last_observed_at: dict[tuple[str, int | None, str | None], datetime] = {}
_last_observed_at_lock = threading.Lock()


def _ensure_worker_started() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(target=_worker_loop, name="analytics-writer", daemon=True)
        thread.start()
        _worker_started = True


def _record_drop(reason: str) -> None:
    global _dropped_count
    with _dropped_lock:
        _dropped_count += 1
    log.debug("analytics observation dropped reason=%s", reason)


def _actual_interval_seconds(resource: str, match_id: int | None, match_provider_id: str | None,
                             observed_at: datetime) -> float | None:
    key = (resource, match_id, match_provider_id)
    with _last_observed_at_lock:
        previous = _last_observed_at.get(key)
        _last_observed_at[key] = observed_at
    if previous is None:
        return None
    return max(0.0, (observed_at - previous).total_seconds())


def _worker_loop() -> None:
    # A fresh connection per write (matching db/scrape_runs.py's convention)
    # rather than a cached long-lived one: writes are infrequent enough that
    # open/close overhead is immaterial (see docs/analytics_framework.md
    # "Analytics overhead"), and this keeps the worker correct if the
    # configured database path is ever reconfigured mid-process (as tests do).
    while True:
        kind, observation = _queue.get()
        try:
            conn = get_db_connection()
            try:
                if kind == _UPSTREAM:
                    storage.insert_upstream_poll(conn, observation)
                else:
                    storage.insert_consumer_request(conn, observation)
            finally:
                conn.close()
        except Exception:
            log.debug("analytics write failed kind=%s", kind, exc_info=True)
            _record_drop("write_failed")
        finally:
            _queue.task_done()


def dropped_observation_count() -> int:
    """Total observations dropped since process start (disabled, full queue, or write failure)."""
    with _dropped_lock:
        return _dropped_count


def wait_until_idle(timeout: float = 5.0) -> bool:
    """Block until the background write queue is empty. Test-only helper."""
    done = threading.Event()

    def _waiter() -> None:
        _queue.join()
        done.set()

    threading.Thread(target=_waiter, daemon=True).start()
    return done.wait(timeout=timeout)


def record_upstream_poll(*, resource: str, observed_at: datetime, duration_ms: float,
                         outcome: UpstreamOutcome, match_id: int | None = None,
                         match_provider_id: str | None = None, lifecycle_state: str | None = None,
                         configured_interval_seconds: float | None = None,
                         http_status: int | None = None, changed: bool | None = None,
                         change_magnitude: int | None = None, note: str | None = None) -> None:
    """Record one upstream poll observation. Never raises. See module docstring."""
    if not config.AFL_ANALYTICS_ENABLED:
        return
    try:
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        actual_interval_seconds = _actual_interval_seconds(resource, match_id, match_provider_id, observed_at)
        observation = UpstreamPollObservation(
            resource=resource, match_id=match_id, match_provider_id=match_provider_id,
            observed_at=observed_at, lifecycle_state=lifecycle_state,
            configured_interval_seconds=configured_interval_seconds,
            actual_interval_seconds=actual_interval_seconds, duration_ms=duration_ms,
            outcome=outcome, http_status=http_status, changed=changed,
            change_magnitude=change_magnitude, note=note,
        )
        _ensure_worker_started()
        _queue.put_nowait((_UPSTREAM, observation))
    except queue.Full:
        _record_drop("queue_full")
    except Exception:
        log.debug("record_upstream_poll failed", exc_info=True)
        _record_drop("record_failed")


def record_consumer_request(*, route: str, observed_at: datetime, duration_ms: float, status_code: int,
                            api_key_id: int | None = None, request_mode: str | None = None) -> None:
    """Record one consumer /api/v1 request observation. Never raises. See module docstring."""
    if not config.AFL_ANALYTICS_CONSUMER_ENABLED:
        return
    try:
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        observation = ConsumerRequestObservation(
            route=route, observed_at=observed_at, duration_ms=duration_ms, status_code=status_code,
            api_key_id=api_key_id, request_mode=request_mode,
        )
        _ensure_worker_started()
        _queue.put_nowait((_CONSUMER, observation))
    except queue.Full:
        _record_drop("queue_full")
    except Exception:
        log.debug("record_consumer_request failed", exc_info=True)
        _record_drop("record_failed")
