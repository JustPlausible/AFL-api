"""Analytics retention and daily roll-up (Issue #205).

Keeps bounded raw detail for a useful investigation window
(``config.AFL_ANALYTICS_RETENTION_DAYS``, default 14 days), then folds
observations older than that window into per-day aggregates and deletes the
raw rows -- see ``docs/analytics_framework.md`` "Storage schema and
retention". This is the one piece of genuinely new scheduler infrastructure
Stage 1 adds: a single daily job (registered once in
``scheduler/scheduled_tasks.py``) that calls :func:`run_rollup_and_retention`.
Adding a new analytics module/resource never needs a second one -- the job
rolls up every resource/route already present in the raw tables.

Idempotent by construction: rolling up a date recomputes its aggregate from
whatever raw rows currently exist for that date and upserts
(``ON CONFLICT ... DO UPDATE``), then deletes exactly those rows. Running
the job twice in a row (or after a crash mid-run) is safe -- the second run
simply finds no raw rows left for already-rolled-up dates and is a no-op for
them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
from analytics import storage
from db.connection import get_db_connection
from logging_sources import LOG_SOURCES
from utils.log import setup_logger

_source = LOG_SOURCES["analytics"]
log = setup_logger(_source.logger_name, _source.filename)


def _cutoff_date(now: datetime, retention_days: int) -> str:
    cutoff = now.astimezone(timezone.utc) - timedelta(days=retention_days)
    return cutoff.date().isoformat()


def run_rollup_and_retention(conn=None, *, now: datetime | None = None,
                             retention_days: int | None = None) -> dict[str, int]:
    """Roll up and purge raw analytics rows older than the retention window.

    Returns a summary dict (``upstream_dates_rolled``, ``consumer_dates_rolled``)
    for logging/tests. Safe to call repeatedly (see module docstring).
    """
    owned = conn is None
    db = conn if conn is not None else get_db_connection()
    clock = now or datetime.now(timezone.utc)
    days = config.AFL_ANALYTICS_RETENTION_DAYS if retention_days is None else retention_days
    cutoff = _cutoff_date(clock, days)
    try:
        upstream_dates = storage.dates_needing_rollup(db, table="analytics_upstream_polls", before_date=cutoff)
        for observation_date in upstream_dates:
            storage.rollup_upstream_date(db, observation_date)
        consumer_dates = storage.dates_needing_rollup(db, table="analytics_consumer_requests", before_date=cutoff)
        for observation_date in consumer_dates:
            storage.rollup_consumer_date(db, observation_date)
        db.commit()
        if upstream_dates or consumer_dates:
            log.info(
                "analytics rollup complete cutoff=%s upstream_dates=%s consumer_dates=%s",
                cutoff, len(upstream_dates), len(consumer_dates),
            )
        return {"upstream_dates_rolled": len(upstream_dates), "consumer_dates_rolled": len(consumer_dates)}
    finally:
        if owned:
            db.close()
