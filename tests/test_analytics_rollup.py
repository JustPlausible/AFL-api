"""Analytics retention/roll-up behaviour (Issue #205).

Verifies that raw observations older than the retention window are folded
into daily rollups with correct aggregate counts, that raw rows for those
dates are purged, that recent rows within the window are left alone, and
that re-running the job is a safe no-op (idempotent).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import config
from analytics.rollup import run_rollup_and_retention
from db.migration_runner import migrate_database

TODAY = datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "rollup.db"
    migrate_database(path)
    monkeypatch.setattr(config, "DB_PATH", str(path))
    monkeypatch.setattr(config, "AFL_ANALYTICS_RETENTION_DAYS", 14)
    return path


def _insert_poll(conn, *, resource, lifecycle_state, observation_date, outcome, changed):
    conn.execute(
        """
        INSERT INTO analytics_upstream_polls (
            resource, observed_at, observation_date, lifecycle_state, duration_ms, outcome, changed
        ) VALUES (?,?,?,?,10,?,?)
        """,
        (resource, f"{observation_date}T12:00:00+00:00", observation_date, lifecycle_state, outcome,
         None if changed is None else int(changed)),
    )


def _insert_request(conn, *, route, observation_date, status_code):
    conn.execute(
        "INSERT INTO analytics_consumer_requests (route, observed_at, observation_date, duration_ms, status_code) "
        "VALUES (?,?,?,5,?)",
        (route, f"{observation_date}T12:00:00+00:00", observation_date, status_code),
    )


def test_old_observations_are_rolled_up_and_purged(db_path):
    old_date = (TODAY - timedelta(days=20)).date().isoformat()
    conn = sqlite3.connect(db_path)
    for _ in range(3):
        _insert_poll(conn, resource="cfs_player_stats", lifecycle_state="LIVE",
                    observation_date=old_date, outcome="success", changed=True)
    for _ in range(7):
        _insert_poll(conn, resource="cfs_player_stats", lifecycle_state="LIVE",
                    observation_date=old_date, outcome="success", changed=False)
    _insert_poll(conn, resource="cfs_player_stats", lifecycle_state="LIVE",
                observation_date=old_date, outcome="http_error", changed=None)
    _insert_request(conn, route="/api/v1/seasons", observation_date=old_date, status_code=200)
    conn.commit()
    conn.close()

    summary = run_rollup_and_retention(now=TODAY)
    assert summary == {"upstream_dates_rolled": 1, "consumer_dates_rolled": 1}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    raw = conn.execute("SELECT * FROM analytics_upstream_polls WHERE observation_date=?", (old_date,)).fetchall()
    assert raw == []
    raw_requests = conn.execute(
        "SELECT * FROM analytics_consumer_requests WHERE observation_date=?", (old_date,)
    ).fetchall()
    assert raw_requests == []

    rollup = conn.execute(
        "SELECT * FROM analytics_upstream_daily_rollups WHERE resource='cfs_player_stats' AND lifecycle_state='LIVE' "
        "AND observation_date=?", (old_date,),
    ).fetchone()
    assert rollup["polls"] == 11
    assert rollup["successes"] == 10
    assert rollup["changed"] == 3
    assert rollup["unchanged"] == 7
    assert rollup["failures"] == 1

    request_rollup = conn.execute(
        "SELECT * FROM analytics_consumer_daily_rollups WHERE route='/api/v1/seasons' AND observation_date=?",
        (old_date,),
    ).fetchone()
    assert request_rollup["requests"] == 1
    assert request_rollup["status_2xx"] == 1
    conn.close()


def test_recent_observations_within_retention_window_are_left_alone(db_path):
    recent_date = (TODAY - timedelta(days=1)).date().isoformat()
    conn = sqlite3.connect(db_path)
    _insert_poll(conn, resource="match_commentary", lifecycle_state="LIVE",
                observation_date=recent_date, outcome="success", changed=True)
    conn.commit()
    conn.close()

    summary = run_rollup_and_retention(now=TODAY)
    assert summary == {"upstream_dates_rolled": 0, "consumer_dates_rolled": 0}

    conn = sqlite3.connect(db_path)
    raw = conn.execute(
        "SELECT COUNT(*) FROM analytics_upstream_polls WHERE observation_date=?", (recent_date,)
    ).fetchone()[0]
    assert raw == 1
    conn.close()


def test_rerunning_rollup_is_idempotent(db_path):
    old_date = (TODAY - timedelta(days=30)).date().isoformat()
    conn = sqlite3.connect(db_path)
    _insert_poll(conn, resource="match_interchange", lifecycle_state="POSTGAME",
                observation_date=old_date, outcome="success", changed=False)
    conn.commit()
    conn.close()

    first = run_rollup_and_retention(now=TODAY)
    second = run_rollup_and_retention(now=TODAY)
    assert first == {"upstream_dates_rolled": 1, "consumer_dates_rolled": 0}
    assert second == {"upstream_dates_rolled": 0, "consumer_dates_rolled": 0}

    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM analytics_upstream_daily_rollups WHERE resource='match_interchange' "
        "AND observation_date=?", (old_date,),
    ).fetchone()[0]
    assert count == 1  # UNIQUE constraint would fail on double-insert; upsert kept it to one row
    conn.close()
