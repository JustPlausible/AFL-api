"""Offline tests for the production match-roster polling scheduler (Issue #219).

Exercises the round-scoped candidate-window poller against a fake CFS client
and a migrated temporary SQLite database. No live AFL/CFS access is required
or attempted. Mirrors tests/test_match_interchange_production_scheduler.py's
shape (Issue #204's promotion, reused here as the architectural template),
adapted for round-level (not per-match) candidate selection and for
MatchRosterCollector's ``.get()``-based client surface (rather than
``.request()``) and its raise-before-result malformed-payload contract.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from afl_json.client import AflJsonHttpError, AflJsonInvalidResponse, AflJsonResourceUnavailable
from db.migration_runner import migrate_database
from scheduler.match_roster_production import poll_match_rosters

NOW = datetime(2026, 7, 25, 3, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"

ROUND_ID = 1
ROUND_PROVIDER_ID = "CD_R18"
MATCH_ID = 100
MATCH_PROVIDER_ID = "CD_M100"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "afl.db"
    monkeypatch.setenv("DB_PATH", str(path))
    import config
    monkeypatch.setattr(config, "DB_PATH", str(path), raising=False)
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}','now')")
    conn.execute(
        "INSERT INTO afl_seasons(afl_id,provider_id,competition_id,year,is_current,updated_at) "
        "VALUES(85,'CD_S2026014',1,2026,NULL,'now')"
    )
    conn.execute(
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, provider_id) "
        "VALUES(?,'R18',85,1,?)", (ROUND_ID, ROUND_PROVIDER_ID),
    )
    conn.commit()
    yield conn, path
    conn.close()


def add_match(conn, match_id, provider, *, round_id=ROUND_ID, status="SCHEDULED", start=None):
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, status, "
        "start_time_utc, scraped_at) VALUES(?,?,?,'Cats','Dogs',?,?,?)",
        (match_id, provider, round_id, status, start or NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()


def _enabled(monkeypatch, *, interval=900, pre_round_window=86400, kickoff_tolerance=600):
    import config
    monkeypatch.setattr(config, "AFL_ROSTER_PRODUCTION_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_ROSTER_PRODUCTION_INTERVAL_SECONDS", interval, raising=False)
    monkeypatch.setattr(config, "AFL_ROSTER_PRODUCTION_PRE_ROUND_WINDOW_SECONDS", pre_round_window, raising=False)
    monkeypatch.setattr(config, "AFL_ROSTER_PRODUCTION_KICKOFF_TOLERANCE_SECONDS", kickoff_tolerance, raising=False)


def _disabled(monkeypatch):
    import config
    monkeypatch.setattr(config, "AFL_ROSTER_PRODUCTION_ENABLED", False, raising=False)


class FakeClient:
    """Fake AflJsonClient exposing only the .get() surface MatchRosterCollector uses."""

    def __init__(self, payloads_by_round: dict[str, list]):
        self._payloads = {key: list(value) for key, value in payloads_by_round.items()}
        self.calls: list[str] = []

    def get(self, name, path_parameters=None):
        round_provider_id = path_parameters["round_provider_id"]
        self.calls.append(round_provider_id)
        queue = self._payloads.get(round_provider_id)
        if not queue:
            raise AflJsonHttpError("no more fixture responses", endpoint="match_rosters", status_code=500)
        payload = queue.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return SimpleNamespace(data=payload)


def test_disabled_by_default_override_returns_empty_and_makes_no_calls(db, monkeypatch):
    conn, _ = db
    add_match(conn, MATCH_ID, MATCH_PROVIDER_ID, status="LIVE")
    _disabled(monkeypatch)
    client = FakeClient({ROUND_PROVIDER_ID: [_fixture("match_rosters_available.json")]})
    assert poll_match_rosters(client=client) == []
    assert client.calls == []


def test_no_calls_when_no_in_window_rounds(db, monkeypatch):
    conn, _ = db
    far_future = (NOW + timedelta(days=30)).isoformat()
    add_match(conn, MATCH_ID, MATCH_PROVIDER_ID, status="SCHEDULED", start=far_future)
    _enabled(monkeypatch)
    client = FakeClient({ROUND_PROVIDER_ID: [_fixture("match_rosters_available.json")]})
    assert poll_match_rosters(client=client, clock=lambda: NOW) == []
    assert client.calls == []


def test_round_with_all_matches_concluded_is_never_polled(db, monkeypatch):
    conn, _ = db
    add_match(conn, MATCH_ID, MATCH_PROVIDER_ID, status="CONCLUDED")
    _enabled(monkeypatch)
    client = FakeClient({ROUND_PROVIDER_ID: [_fixture("match_rosters_available.json")]})
    assert poll_match_rosters(client=client, clock=lambda: NOW) == []
    assert client.calls == []


def test_polls_round_with_live_match_and_persists(db, monkeypatch):
    conn, _ = db
    add_match(conn, MATCH_ID, MATCH_PROVIDER_ID, status="LIVE")
    _enabled(monkeypatch)
    client = FakeClient({ROUND_PROVIDER_ID: [_fixture("match_rosters_available.json")]})
    results = poll_match_rosters(client=client, clock=lambda: NOW)
    assert len(results) == 1
    assert results[0]["outcome"] == "success"
    assert results[0]["rosters_written"] == 2
    assert conn.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0] == 4


def test_polls_round_within_pre_round_window_before_first_bounce(db, monkeypatch):
    conn, _ = db
    upcoming = (NOW + timedelta(hours=12)).isoformat()
    add_match(conn, MATCH_ID, MATCH_PROVIDER_ID, status="SCHEDULED", start=upcoming)
    _enabled(monkeypatch, pre_round_window=86400)
    client = FakeClient({ROUND_PROVIDER_ID: [_fixture("match_rosters_available.json")]})
    results = poll_match_rosters(client=client, clock=lambda: NOW)
    assert len(results) == 1
    assert results[0]["outcome"] == "success"


def test_round_outside_pre_round_window_is_not_yet_polled(db, monkeypatch):
    conn, _ = db
    upcoming = (NOW + timedelta(hours=48)).isoformat()
    add_match(conn, MATCH_ID, MATCH_PROVIDER_ID, status="SCHEDULED", start=upcoming)
    _enabled(monkeypatch, pre_round_window=86400)
    client = FakeClient({ROUND_PROVIDER_ID: [_fixture("match_rosters_available.json")]})
    assert poll_match_rosters(client=client, clock=lambda: NOW) == []
    assert client.calls == []


def test_kickoff_tolerance_catches_delayed_local_status_flip(db, monkeypatch):
    conn, _ = db
    started = (NOW - timedelta(minutes=5)).isoformat()
    add_match(conn, MATCH_ID, MATCH_PROVIDER_ID, status="SCHEDULED", start=started)
    _enabled(monkeypatch, kickoff_tolerance=600)
    client = FakeClient({ROUND_PROVIDER_ID: [_fixture("match_rosters_available.json")]})
    results = poll_match_rosters(client=client, clock=lambda: NOW)
    assert len(results) == 1
    assert results[0]["outcome"] == "success"


def test_unavailable_response_does_not_erase_prior_persisted_roster(db, monkeypatch):
    conn, _ = db
    add_match(conn, MATCH_ID, MATCH_PROVIDER_ID, status="LIVE")
    _enabled(monkeypatch)
    client = FakeClient({ROUND_PROVIDER_ID: [_fixture("match_rosters_available.json")]})
    poll_match_rosters(client=client, clock=lambda: NOW)
    before = conn.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0]
    assert before == 4

    client2 = FakeClient({ROUND_PROVIDER_ID: [AflJsonResourceUnavailable("not yet published", endpoint="match_rosters")]})
    results = poll_match_rosters(client=client2, clock=lambda: NOW + timedelta(minutes=15))
    # MatchRosterCollector.collect() absorbs a not-yet-published round into
    # RosterCollectionResult(..., UNAVAILABLE, ...) rather than raising, so
    # the poll cycle itself succeeds (outcome="success") while the granular
    # business status reported is "unavailable" -- see result.status below.
    assert results[0]["outcome"] == "success"
    assert results[0]["status"] == "unavailable"
    assert conn.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0] == before


def test_malformed_response_does_not_erase_prior_persisted_roster(db, monkeypatch):
    conn, _ = db
    add_match(conn, MATCH_ID, MATCH_PROVIDER_ID, status="LIVE")
    _enabled(monkeypatch)
    client = FakeClient({ROUND_PROVIDER_ID: [_fixture("match_rosters_available.json")]})
    poll_match_rosters(client=client, clock=lambda: NOW)
    before = conn.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0]
    assert before == 4

    client2 = FakeClient({ROUND_PROVIDER_ID: [{"not": "a list"}]})
    results = poll_match_rosters(client=client2, clock=lambda: NOW + timedelta(minutes=15))
    assert results[0]["outcome"] == "malformed_payload"
    assert conn.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0] == before


def test_empty_list_response_does_not_erase_prior_persisted_roster(db, monkeypatch):
    conn, _ = db
    add_match(conn, MATCH_ID, MATCH_PROVIDER_ID, status="LIVE")
    _enabled(monkeypatch)
    client = FakeClient({ROUND_PROVIDER_ID: [_fixture("match_rosters_available.json")]})
    poll_match_rosters(client=client, clock=lambda: NOW)
    before = conn.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0]
    assert before == 4

    client2 = FakeClient({ROUND_PROVIDER_ID: [[]]})
    results = poll_match_rosters(client=client2, clock=lambda: NOW + timedelta(minutes=15))
    assert results[0]["status"] == "empty"
    assert conn.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0] == before


def test_repeated_identical_poll_is_idempotent(db, monkeypatch):
    conn, _ = db
    add_match(conn, MATCH_ID, MATCH_PROVIDER_ID, status="LIVE")
    _enabled(monkeypatch)
    payload = _fixture("match_rosters_available.json")
    client = FakeClient({ROUND_PROVIDER_ID: [payload, payload]})
    telemetry = []
    monkeypatch.setattr(
        "scheduler.match_roster_production.record_upstream_poll",
        lambda **values: telemetry.append(values),
    )
    poll_match_rosters(client=client, clock=lambda: NOW)
    poll_match_rosters(client=client, clock=lambda: NOW + timedelta(minutes=15))
    assert conn.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0] == 4
    assert [item["changed"] for item in telemetry] == [True, False]
    assert telemetry[0]["change_magnitude"] > 0
    assert telemetry[1]["change_magnitude"] == 0


def test_continues_after_one_round_fails(db, monkeypatch):
    conn, _ = db
    add_match(conn, MATCH_ID, MATCH_PROVIDER_ID, round_id=ROUND_ID, status="LIVE")
    conn.execute(
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, provider_id) "
        "VALUES(2,'R19',85,1,'CD_R19')"
    )
    add_match(conn, 200, "CD_M200", round_id=2, status="LIVE")
    conn.commit()
    _enabled(monkeypatch)
    client = FakeClient({
        ROUND_PROVIDER_ID: [AflJsonHttpError("boom", endpoint="match_rosters", status_code=500)],
        "CD_R19": [_fixture("match_rosters_available.json")],
    })
    results = poll_match_rosters(client=client, clock=lambda: NOW)
    outcomes = {r["round_provider_id"]: r["outcome"] for r in results}
    assert outcomes[ROUND_PROVIDER_ID] == "http_error"
    assert outcomes["CD_R19"] == "success"


def test_no_calls_at_all_when_matches_have_no_start_time_and_none_are_live(db, monkeypatch):
    conn, _ = db
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, status, scraped_at) "
        "VALUES(?,?,?,'Cats','Dogs','SCHEDULED',?)", (MATCH_ID, MATCH_PROVIDER_ID, ROUND_ID, NOW.isoformat()),
    )
    conn.commit()
    _enabled(monkeypatch)
    client = FakeClient({ROUND_PROVIDER_ID: [_fixture("match_rosters_available.json")]})
    assert poll_match_rosters(client=client, clock=lambda: NOW) == []
    assert client.calls == []
