"""End-to-end analytics wiring for one production match-scheduler collector (Issue #205).

Exercises ``scheduler.match_commentary_production.poll_match_commentary``
against a fake CFS client and asserts the resulting
``analytics_upstream_polls`` rows -- this is the representative
"instrumentation actually fires" test for the Stage 2 wiring; the other two
principal collectors (``cfs_player_stats``, ``match_interchange``) follow the
identical pattern (see their own outcome-mapping tables in
``scheduler/player_stat_polling.py`` and
``scheduler/match_interchange_production.py``), and their full existing test
suites (``tests/test_player_stat_polling.py``,
``tests/test_match_interchange_production_scheduler.py``) continue to pass
unmodified, demonstrating the instrumentation did not change collector
behaviour.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import config
from afl_json.client import AflJsonHttpError
from analytics import record, storage
from db.migration_runner import migrate_database
from scheduler.match_commentary_production import poll_match_commentary

NOW = datetime(2026, 8, 23, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "afl.db"
    migrate_database(path)
    monkeypatch.setattr(config, "DB_PATH", str(path))
    monkeypatch.setattr(config, "AFL_COMMENTARY_PRODUCTION_ENABLED", True)
    monkeypatch.setattr(config, "AFL_ANALYTICS_ENABLED", True)
    monkeypatch.setattr(record, "_last_observed_at", {})
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, scraped_at) VALUES(1,'R24',73,1,?)",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, "
        "start_time_utc, season_id, scraped_at) VALUES(8001,'CD_M1',1,'A','B','V','LIVE',?,73,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    conn.close()
    return path


def _event(seconds=0):
    return {"comment": "Q1 is now underway.", "periodNumber": 1, "periodSeconds": seconds,
            "playerId": None, "teamId": None, "scoreEvent": False}


def _payload(events):
    return {"matchId": "CD_M1", "lastUpdated": "2026-08-23T03:00:00.000+0000", "commentaryEvent": events}


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)

    def request(self, endpoint, *, path_parameters=None, **_kwargs):
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(data=response)


def _analytics_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM analytics_upstream_polls WHERE resource='match_commentary' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_first_poll_with_events_is_a_changed_success(db):
    client = FakeClient([_payload([_event(0)])])
    poll_match_commentary(client=client, clock=lambda: NOW)
    assert record.wait_until_idle()
    rows = _analytics_rows(db)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "success"
    assert rows[0]["changed"] == 1
    assert rows[0]["change_magnitude"] == 1
    assert rows[0]["lifecycle_state"] == "LIVE"
    assert rows[0]["match_id"] == 8001


def test_repeated_identical_poll_is_an_unchanged_success(db):
    client = FakeClient([_payload([_event(0)]), _payload([_event(0)])])
    poll_match_commentary(client=client, clock=lambda: NOW)
    poll_match_commentary(client=client, clock=lambda: NOW)
    assert record.wait_until_idle()
    rows = _analytics_rows(db)
    assert len(rows) == 2
    assert rows[0]["changed"] == 1
    assert rows[1]["changed"] == 0
    assert rows[1]["change_magnitude"] == 0


def test_http_failure_is_recorded_as_http_error_outcome(db):
    client = FakeClient([AflJsonHttpError("boom", endpoint="match_commentary", status_code=500)])
    poll_match_commentary(client=client, clock=lambda: NOW)
    assert record.wait_until_idle()
    rows = _analytics_rows(db)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "http_error"
    assert rows[0]["changed"] is None


def test_analytics_write_failure_never_blocks_collector_persistence(db, monkeypatch):
    def _boom(conn, observation):
        raise sqlite3.OperationalError("simulated analytics failure")

    monkeypatch.setattr(storage, "insert_upstream_poll", _boom)
    client = FakeClient([_payload([_event(0)])])
    results = poll_match_commentary(client=client, clock=lambda: NOW)
    assert record.wait_until_idle()

    # The collector's own result/persistence is unaffected by the analytics
    # write failure -- it still reports the poll as a successful outcome.
    assert results[0]["outcome"] == "success"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    commentary_rows = conn.execute("SELECT * FROM match_commentary_events").fetchall()
    conn.close()
    assert len(commentary_rows) == 1
    # And no analytics row exists, since the simulated write failure dropped it.
    assert _analytics_rows(db) == []
