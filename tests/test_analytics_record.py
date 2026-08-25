"""Core analytics recording behaviour (Issue #205).

Covers the framework-level contract from ``analytics/record.py``: enabled/
disabled gating, success vs changed/unchanged classification, failure
outcome recording, repeated polls (actual-interval bookkeeping), lifecycle
context, and write-failure isolation. Uses a real migrated SQLite database
(matching the rest of the suite's convention) and drains the background
write queue with ``wait_until_idle`` for deterministic assertions.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import config
from analytics import record, storage
from analytics.contracts import UpstreamOutcome
from db.migration_runner import migrate_database

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _database(tmp_path, monkeypatch):
    path = tmp_path / "analytics.db"
    migrate_database(path)
    monkeypatch.setattr(config, "DB_PATH", str(path))
    monkeypatch.setattr(config, "AFL_ANALYTICS_ENABLED", True)
    monkeypatch.setattr(config, "AFL_ANALYTICS_CONSUMER_ENABLED", True)
    # Each test gets an isolated key namespace for the in-process
    # actual-interval tracker so tests never see another test's "previous"
    # observation for the same (resource, match) pair.
    monkeypatch.setattr(record, "_last_observed_at", {})
    return path


def _rows(db_path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM analytics_upstream_polls ORDER BY id").fetchall()
    finally:
        conn.close()


def _consumer_rows(db_path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM analytics_consumer_requests ORDER BY id").fetchall()
    finally:
        conn.close()


def _seed_match(db_path, match_id: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, "
            "start_time_utc, season_id, scraped_at) VALUES(?,?,1,'A','B','V','LIVE',?,73,?)",
            (match_id, f"CD_M{match_id}", NOW.isoformat(), NOW.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def test_disabled_records_nothing(_database, monkeypatch):
    monkeypatch.setattr(config, "AFL_ANALYTICS_ENABLED", False)
    record.record_upstream_poll(
        resource="cfs_player_stats", observed_at=NOW, duration_ms=12.0, outcome=UpstreamOutcome.SUCCESS,
    )
    assert record.wait_until_idle()
    assert _rows(_database) == []


def test_successful_changed_poll_is_recorded(_database):
    _seed_match(_database, 1)
    record.record_upstream_poll(
        resource="cfs_player_stats", match_id=1, match_provider_id="CD_M1", observed_at=NOW,
        lifecycle_state="LIVE", configured_interval_seconds=60, duration_ms=42.5,
        outcome=UpstreamOutcome.SUCCESS, http_status=200, changed=True, change_magnitude=7,
    )
    assert record.wait_until_idle()
    rows = _rows(_database)
    assert len(rows) == 1
    row = rows[0]
    assert row["resource"] == "cfs_player_stats"
    assert row["match_id"] == 1
    assert row["lifecycle_state"] == "LIVE"
    assert row["outcome"] == "success"
    assert row["changed"] == 1
    assert row["change_magnitude"] == 7
    assert row["configured_interval_seconds"] == 60


def test_successful_unchanged_poll_is_distinguished_from_changed(_database):
    _seed_match(_database, 1)
    record.record_upstream_poll(
        resource="cfs_player_stats", match_id=1, observed_at=NOW, duration_ms=10.0,
        outcome=UpstreamOutcome.SUCCESS, changed=False, change_magnitude=0,
    )
    assert record.wait_until_idle()
    row = _rows(_database)[0]
    assert row["outcome"] == "success"
    assert row["changed"] == 0
    assert row["change_magnitude"] == 0


@pytest.mark.parametrize("outcome", [
    UpstreamOutcome.NOT_PUBLISHED, UpstreamOutcome.UNAVAILABLE, UpstreamOutcome.AUTH_ERROR,
    UpstreamOutcome.TRANSPORT_ERROR, UpstreamOutcome.HTTP_ERROR, UpstreamOutcome.INVALID_RESPONSE,
])
def test_failure_outcomes_are_recorded_distinctly(_database, outcome):
    _seed_match(_database, 2)
    record.record_upstream_poll(
        resource="match_commentary", match_id=2, observed_at=NOW, duration_ms=5.0, outcome=outcome,
    )
    assert record.wait_until_idle()
    row = _rows(_database)[0]
    assert row["outcome"] == outcome.value
    assert row["changed"] is None


def test_repeated_polls_compute_actual_interval_since_previous(_database):
    _seed_match(_database, 3)
    record.record_upstream_poll(
        resource="cfs_player_stats", match_id=3, match_provider_id="CD_M3", observed_at=NOW, duration_ms=1.0,
        outcome=UpstreamOutcome.SUCCESS, changed=False,
    )
    assert record.wait_until_idle()
    later = NOW + timedelta(seconds=63)
    record.record_upstream_poll(
        resource="cfs_player_stats", match_id=3, match_provider_id="CD_M3", observed_at=later, duration_ms=1.0,
        outcome=UpstreamOutcome.SUCCESS, changed=True,
    )
    assert record.wait_until_idle()
    rows = _rows(_database)
    assert rows[0]["actual_interval_seconds"] is None  # no previous observation yet
    assert rows[1]["actual_interval_seconds"] == pytest.approx(63.0)


def test_lifecycle_context_is_persisted(_database):
    _seed_match(_database, 4)
    for state in ("SCHEDULED", "LIVE", "POSTGAME", "CONCLUDED"):
        record.record_upstream_poll(
            resource="match_interchange", match_id=4, observed_at=NOW, duration_ms=1.0,
            outcome=UpstreamOutcome.SUCCESS, lifecycle_state=state, changed=False,
        )
    assert record.wait_until_idle()
    observed_states = {row["lifecycle_state"] for row in _rows(_database)}
    assert observed_states == {"SCHEDULED", "LIVE", "POSTGAME", "CONCLUDED"}


def test_storage_write_failure_is_dropped_not_raised(_database, monkeypatch):
    before = record.dropped_observation_count()

    def _boom(conn, observation):
        raise sqlite3.OperationalError("simulated failure")

    monkeypatch.setattr(storage, "insert_upstream_poll", _boom)
    record.record_upstream_poll(
        resource="cfs_player_stats", observed_at=NOW, duration_ms=1.0, outcome=UpstreamOutcome.SUCCESS,
    )
    assert record.wait_until_idle()
    assert record.dropped_observation_count() == before + 1
    assert _rows(_database) == []


def test_consumer_request_disabled_records_nothing(_database, monkeypatch):
    monkeypatch.setattr(config, "AFL_ANALYTICS_CONSUMER_ENABLED", False)
    record.record_consumer_request(
        route="/api/v1/seasons", observed_at=NOW, duration_ms=5.0, status_code=200,
    )
    assert record.wait_until_idle()
    assert _consumer_rows(_database) == []


def test_consumer_request_success_and_error_are_recorded(_database):
    record.record_consumer_request(
        route="/api/v1/seasons", observed_at=NOW, duration_ms=5.0, status_code=200, api_key_id=42,
    )
    record.record_consumer_request(
        route="/api/v1/matches/{match_id}", observed_at=NOW, duration_ms=8.0, status_code=404,
    )
    assert record.wait_until_idle()
    rows = _consumer_rows(_database)
    assert len(rows) == 2
    assert {row["status_code"] for row in rows} == {200, 404}
    assert rows[0]["api_key_id"] == 42
    assert rows[1]["api_key_id"] is None
