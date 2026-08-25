"""Analytics reporting and modular registration (Issue #205).

Covers :mod:`analytics.reporting` (combining bounded raw detail with older
daily rollups transparently, match-level vs resource-level grouping) and
:mod:`analytics.contracts`'s registration functions (stable identifiers,
collision detection on re-registration with different metadata).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import config
from analytics.contracts import register_resource, register_route
from analytics.reporting import AnalyticsReporter
from analytics.rollup import run_rollup_and_retention
from db.migration_runner import migrate_database

TODAY = datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    path = tmp_path / "reporting.db"
    migrate_database(path)
    monkeypatch.setattr(config, "DB_PATH", str(path))
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def _insert_poll(connection, *, resource, lifecycle_state, observation_date, outcome, changed, match_id=None):
    connection.execute(
        "INSERT INTO analytics_upstream_polls (resource, match_id, observed_at, observation_date, "
        "lifecycle_state, duration_ms, outcome, changed) VALUES (?,?,?,?,?,10,?,?)",
        (resource, match_id, f"{observation_date}T12:00:00+00:00", observation_date, lifecycle_state, outcome,
         None if changed is None else int(changed)),
    )
    connection.commit()


def test_resource_summary_combines_raw_and_rolled_up_rows(conn, tmp_path):
    recent_date = (TODAY - timedelta(days=1)).date().isoformat()
    old_date = (TODAY - timedelta(days=20)).date().isoformat()
    _insert_poll(conn, resource="cfs_player_stats", lifecycle_state="LIVE",
                observation_date=old_date, outcome="success", changed=True)
    _insert_poll(conn, resource="cfs_player_stats", lifecycle_state="LIVE",
                observation_date=recent_date, outcome="success", changed=False)
    run_rollup_and_retention(conn, now=TODAY, retention_days=14)

    reporter = AnalyticsReporter(conn, clock=lambda: TODAY)
    summary = reporter.resource_summary()
    assert len(summary) == 1
    row = summary[0]
    assert row.resource == "cfs_player_stats"
    assert row.polls == 2
    assert row.changed == 1
    assert row.unchanged == 1
    assert row.polls_per_change == pytest.approx(2.0)


def test_resource_summary_filters_by_date_range(conn):
    for day_offset, changed in ((1, True), (2, False), (3, True)):
        date = (TODAY - timedelta(days=day_offset)).date().isoformat()
        _insert_poll(conn, resource="match_commentary", lifecycle_state="LIVE",
                    observation_date=date, outcome="success", changed=changed)
    reporter = AnalyticsReporter(conn, clock=lambda: TODAY)
    since = (TODAY - timedelta(days=2)).date().isoformat()
    summary = reporter.resource_summary(since_date=since)
    assert summary[0].polls == 2  # offsets 1 and 2, not 3


def test_group_by_lifecycle_breaks_out_states(conn):
    _insert_poll(conn, resource="match_interchange", lifecycle_state="LIVE",
                observation_date=TODAY.date().isoformat(), outcome="success", changed=True)
    _insert_poll(conn, resource="match_interchange", lifecycle_state="POSTGAME",
                observation_date=TODAY.date().isoformat(), outcome="success", changed=False)
    reporter = AnalyticsReporter(conn, clock=lambda: TODAY)
    grouped = reporter.resource_summary(group_by_lifecycle=True)
    assert {row.lifecycle_state for row in grouped} == {"LIVE", "POSTGAME"}
    ungrouped = reporter.resource_summary(group_by_lifecycle=False)
    assert len(ungrouped) == 1
    assert ungrouped[0].lifecycle_state == "ALL"


def test_missing_lifecycle_state_is_normalized_consistently_across_retention_boundary(conn):
    """A raw row with no lifecycle_state (NULL) and a rolled-up row for an
    older NULL-lifecycle observation (stored as 'UNKNOWN', per the rollup
    table's NOT NULL column) must merge into one 'UNKNOWN' group, not split
    into separate None/'UNKNOWN' groups depending on which side of the
    retention boundary each observation happens to fall on."""
    recent_date = (TODAY - timedelta(days=1)).date().isoformat()
    old_date = (TODAY - timedelta(days=20)).date().isoformat()
    _insert_poll(conn, resource="cfs_player_stats", lifecycle_state=None,
                observation_date=recent_date, outcome="success", changed=True)
    _insert_poll(conn, resource="cfs_player_stats", lifecycle_state=None,
                observation_date=old_date, outcome="success", changed=False)
    run_rollup_and_retention(conn, now=TODAY, retention_days=14)

    reporter = AnalyticsReporter(conn, clock=lambda: TODAY)
    grouped = reporter.resource_summary(group_by_lifecycle=True)
    assert len(grouped) == 1
    assert grouped[0].lifecycle_state == "UNKNOWN"
    assert grouped[0].polls == 2

    filtered = reporter.resource_summary(lifecycle_state="UNKNOWN", group_by_lifecycle=True)
    assert len(filtered) == 1
    assert filtered[0].polls == 2


def test_match_summary_is_raw_only(conn):
    connection = conn
    connection.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, "
        "start_time_utc, season_id, scraped_at) VALUES(9,'CD_M9',1,'A','B','V','LIVE',?,73,?)",
        (TODAY.isoformat(), TODAY.isoformat()),
    )
    connection.commit()
    _insert_poll(connection, resource="cfs_player_stats", lifecycle_state="LIVE",
                observation_date=TODAY.date().isoformat(), outcome="success", changed=True, match_id=9)
    reporter = AnalyticsReporter(connection, clock=lambda: TODAY)
    summary = reporter.match_summary(9)
    assert len(summary) == 1
    assert summary[0].polls == 1


def test_stable_resource_identifiers_have_registered_display_names(conn):
    reporter = AnalyticsReporter(conn, clock=lambda: TODAY)
    _insert_poll(conn, resource="cfs_player_stats", lifecycle_state="LIVE",
                observation_date=TODAY.date().isoformat(), outcome="success", changed=True)
    summary = reporter.resource_summary()
    assert summary[0].display_name == "CFS player statistics"


def test_register_resource_is_idempotent_for_identical_metadata():
    register_resource("test_idempotent_resource", display_name="Test Resource", description="x")
    register_resource("test_idempotent_resource", display_name="Test Resource", description="x")


def test_register_resource_rejects_conflicting_redefinition():
    register_resource("test_conflict_resource", display_name="First")
    with pytest.raises(ValueError):
        register_resource("test_conflict_resource", display_name="Second")


def test_register_route_rejects_conflicting_redefinition():
    register_route("/api/v1/test-conflict-route", display_name="First")
    with pytest.raises(ValueError):
        register_route("/api/v1/test-conflict-route", display_name="Second")
