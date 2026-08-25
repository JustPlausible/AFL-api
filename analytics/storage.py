"""Low-level SQLite persistence for analytics observations (Issue #205).

Every function here takes an already-open connection and performs exactly
the writes/reads its name says -- no connection management, no retry, no
failure handling. That belongs to the caller: :mod:`analytics.record` for
writes (failure-isolated, off the calling thread) and
:mod:`analytics.reporting`/``scripts/report_analytics.py`` for reads.

Table shapes mirror the rest of the codebase's conventions (see
``db/scrape_runs.py`` and the commentary/interchange production tables):
``INTEGER PRIMARY KEY AUTOINCREMENT``, ISO-8601 TEXT timestamps,
``CHECK``-constrained enum-like columns, booleans as ``INTEGER CHECK(...
IN (0,1))``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from analytics.contracts import ConsumerRequestObservation, UpstreamPollObservation


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def insert_upstream_poll(conn: sqlite3.Connection, observation: UpstreamPollObservation) -> int:
    cursor = conn.execute(
        """
        INSERT INTO analytics_upstream_polls (
            resource, match_id, match_provider_id, observed_at, observation_date,
            lifecycle_state, configured_interval_seconds, actual_interval_seconds,
            duration_ms, outcome, http_status, changed, change_magnitude, note
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            observation.resource, observation.match_id, observation.match_provider_id,
            _iso(observation.observed_at), _iso(observation.observed_at)[:10],
            observation.lifecycle_state, observation.configured_interval_seconds,
            observation.actual_interval_seconds, observation.duration_ms, observation.outcome.value,
            observation.http_status, None if observation.changed is None else int(observation.changed),
            observation.change_magnitude, observation.note,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_consumer_request(conn: sqlite3.Connection, observation: ConsumerRequestObservation) -> int:
    cursor = conn.execute(
        """
        INSERT INTO analytics_consumer_requests (
            route, observed_at, observation_date, duration_ms, status_code, api_key_id, request_mode
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            observation.route, _iso(observation.observed_at), _iso(observation.observed_at)[:10],
            observation.duration_ms, observation.status_code, observation.api_key_id,
            observation.request_mode,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def dates_needing_rollup(conn: sqlite3.Connection, *, table: str, before_date: str) -> list[str]:
    rows = conn.execute(
        f"SELECT DISTINCT observation_date FROM {table} WHERE observation_date < ? ORDER BY observation_date",
        (before_date,),
    ).fetchall()
    return [row[0] for row in rows]


def rollup_upstream_date(conn: sqlite3.Connection, observation_date: str) -> None:
    rows = conn.execute(
        """
        SELECT resource, COALESCE(lifecycle_state, 'UNKNOWN') AS lifecycle_state,
               COUNT(*) AS polls,
               SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS successes,
               SUM(CASE WHEN outcome='success' AND changed=1 THEN 1 ELSE 0 END) AS changed,
               SUM(CASE WHEN outcome='success' AND changed=0 THEN 1 ELSE 0 END) AS unchanged,
               SUM(CASE WHEN outcome!='success' THEN 1 ELSE 0 END) AS failures,
               SUM(duration_ms) AS total_duration_ms
        FROM analytics_upstream_polls
        WHERE observation_date = ?
        GROUP BY resource, lifecycle_state
        """,
        (observation_date,),
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            INSERT INTO analytics_upstream_daily_rollups (
                resource, lifecycle_state, observation_date, polls, successes,
                changed, unchanged, failures, total_duration_ms
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(resource, lifecycle_state, observation_date) DO UPDATE SET
                polls=excluded.polls, successes=excluded.successes, changed=excluded.changed,
                unchanged=excluded.unchanged, failures=excluded.failures,
                total_duration_ms=excluded.total_duration_ms
            """,
            (
                row["resource"], row["lifecycle_state"], observation_date, row["polls"], row["successes"],
                row["changed"], row["unchanged"], row["failures"], row["total_duration_ms"],
            ),
        )
    conn.execute("DELETE FROM analytics_upstream_polls WHERE observation_date = ?", (observation_date,))


def rollup_consumer_date(conn: sqlite3.Connection, observation_date: str) -> None:
    rows = conn.execute(
        """
        SELECT route, COUNT(*) AS requests,
               SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS status_2xx,
               SUM(CASE WHEN status_code BETWEEN 400 AND 499 THEN 1 ELSE 0 END) AS status_4xx,
               SUM(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END) AS status_5xx,
               SUM(duration_ms) AS total_duration_ms
        FROM analytics_consumer_requests
        WHERE observation_date = ?
        GROUP BY route
        """,
        (observation_date,),
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            INSERT INTO analytics_consumer_daily_rollups (
                route, observation_date, requests, status_2xx, status_4xx, status_5xx, total_duration_ms
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(route, observation_date) DO UPDATE SET
                requests=excluded.requests, status_2xx=excluded.status_2xx,
                status_4xx=excluded.status_4xx, status_5xx=excluded.status_5xx,
                total_duration_ms=excluded.total_duration_ms
            """,
            (
                row["route"], observation_date, row["requests"], row["status_2xx"], row["status_4xx"],
                row["status_5xx"], row["total_duration_ms"],
            ),
        )
    conn.execute("DELETE FROM analytics_consumer_requests WHERE observation_date = ?", (observation_date,))
