"""Offline tests for the production match-interchange polling scheduler (Issue #204).

Exercises the sequential in-window match poller against a fake CFS client
and a migrated temporary SQLite database. No live AFL/CFS access is required
or attempted. Mirrors tests/test_match_commentary_production_scheduler.py's
shape (Issue #201's promotion, the architectural template for this one).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from afl_json.client import (
    AflJsonAuthenticationError,
    AflJsonHttpError,
    AflJsonInvalidResponse,
    AflJsonResourceUnavailable,
    AflJsonTransportError,
)
from db.migration_runner import migrate_database
from scheduler.match_interchange_production import _capture_one, poll_match_interchange

NOW = datetime(2026, 8, 24, 3, 0, tzinfo=timezone.utc)


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
    conn.execute(
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, scraped_at) VALUES(1,'R24',73,1,?)",
        (NOW.isoformat(),),
    )
    conn.commit()
    yield conn, path
    conn.close()


def add_match(conn, match_id, provider, status="LIVE", start=None):
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, start_time_utc, season_id, scraped_at) "
        "VALUES(?,?,1,'A','B','V',?,?,73,?)",
        (match_id, provider, status, start or NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()


def _enabled(monkeypatch, *, interval=20, kickoff_tolerance=None):
    import config
    monkeypatch.setattr(config, "AFL_INTERCHANGE_PRODUCTION_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_INTERCHANGE_PRODUCTION_INTERVAL_SECONDS", interval, raising=False)
    if kickoff_tolerance is not None:
        monkeypatch.setattr(config, "AFL_INTERCHANGE_PRODUCTION_KICKOFF_TOLERANCE_SECONDS", kickoff_tolerance, raising=False)


def _disabled(monkeypatch):
    import config
    monkeypatch.setattr(config, "AFL_INTERCHANGE_PRODUCTION_ENABLED", False, raising=False)


def _entry(player_id, *, team_id="CD_T10", count=1, bench_reason="ROTATION", tog=100, tob=10, power=3):
    return {
        "teamId": team_id,
        "player": {"playerId": player_id, "playerName": {"givenName": "Alex", "surname": "Player"},
                   "captain": False, "playerJumperNumber": 1},
        "interchangeCount": count, "benchReason": bench_reason,
        "timeOnGround": tog, "timeOnBench": tob, "powerRating": power,
    }


def interchange_payload(*, home=None, away=None, match_id="CD_M1"):
    return {
        "matchId": match_id,
        "homeInterchange": home if home is not None else [],
        "awayInterchange": away if away is not None else [],
        "homeInterchangeCounts": {}, "awayInterchangeCounts": {},
    }


class FakeClient:
    """Fake AflJsonClient exposing only the .request() surface used by capture."""

    def __init__(self, payloads_by_match: dict[str, list]):
        self._payloads = {k: list(v) for k, v in payloads_by_match.items()}
        self.calls: list[tuple[str, dict]] = []

    def request(self, endpoint, *, path_parameters=None, **_kwargs):
        match_provider_id = path_parameters["match_provider_id"]
        self.calls.append((getattr(endpoint, "name", endpoint), path_parameters))
        queue = self._payloads.get(match_provider_id)
        if not queue:
            raise AflJsonHttpError("no more fixture responses", endpoint="match_interchange", status_code=500)
        payload = queue.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return SimpleNamespace(data=payload)


def test_disabled_by_default_override_returns_empty_and_makes_no_calls(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _disabled(monkeypatch)
    client = FakeClient({"CD_M1": [interchange_payload()]})
    assert poll_match_interchange(client=client) == []
    assert client.calls == []


def test_no_calls_when_no_in_window_matches(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="SCHEDULED")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [interchange_payload()]})
    assert poll_match_interchange(client=client) == []
    assert client.calls == []


def test_polls_live_match_and_persists_first_observation(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [interchange_payload(home=[_entry("CD_I1")])]})
    results = poll_match_interchange(client=client, clock=lambda: NOW)
    assert len(results) == 1
    assert results[0]["outcome"] == "success"
    assert len(results[0]["appeared"]) == 1
    state_count = conn.execute("SELECT COUNT(*) FROM match_interchange_state").fetchone()[0]
    assert state_count == 1


def test_polls_postgame_match_for_its_one_reconciliation_poll(db, monkeypatch):
    """A match that reaches POSTGAME with no prior POSTGAME poll gets
    exactly one reconciliation poll."""
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="POSTGAME")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [interchange_payload(home=[_entry("CD_I1")])]})
    results = poll_match_interchange(client=client, clock=lambda: NOW)
    assert len(results) == 1
    assert results[0]["outcome"] == "success"


def test_postgame_match_is_not_polled_again_after_its_reconciliation_poll(db, monkeypatch):
    """Real Round 24 evidence showed matchInterchange state freezes
    completely at the LIVE -> POSTGAME transition, so a second poll cycle
    while the match is still POSTGAME must not poll it again -- no grace
    window, no repeated re-observation of an identical payload."""
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="POSTGAME")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [interchange_payload(home=[_entry("CD_I1")])]})
    first = poll_match_interchange(client=client, clock=lambda: NOW)
    assert len(first) == 1

    # Still POSTGAME on the next poll cycle -- must not be re-polled.
    client2 = FakeClient({"CD_M1": [interchange_payload(home=[_entry("CD_I1", count=2)])]})
    second = poll_match_interchange(client=client2, clock=lambda: NOW + timedelta(seconds=20))
    assert second == []
    assert client2.calls == []
    # The stale state from the one reconciliation poll is left untouched.
    row = conn.execute("SELECT interchange_count FROM match_interchange_state WHERE match_provider_id='CD_M1'").fetchone()
    assert row["interchange_count"] == 1


def test_module_has_no_opinion_on_concluded_matches(db, monkeypatch):
    """Once a match reaches CONCLUDED, this module never polls it again --
    CONCLUDED and match finality generally are the authoritative pipeline's
    concern, not interchange collection's."""
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="POSTGAME")
    _enabled(monkeypatch)
    poll_match_interchange(client=FakeClient({"CD_M1": [interchange_payload(home=[_entry("CD_I1")])]}), clock=lambda: NOW)

    conn.execute("UPDATE matches SET status='CONCLUDED' WHERE match_id=8001")
    conn.commit()
    client2 = FakeClient({"CD_M1": [interchange_payload(home=[_entry("CD_I1", count=2)])]})
    results = poll_match_interchange(client=client2, clock=lambda: NOW + timedelta(seconds=30))
    assert results == []
    assert client2.calls == []


def test_multiple_transitions_across_two_polls(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({
        "CD_M1": [
            interchange_payload(home=[_entry("CD_I1"), _entry("CD_I3")]),
            interchange_payload(home=[_entry("CD_I1", count=2), _entry("CD_I4")]),
        ],
    })
    poll_match_interchange(client=client, clock=lambda: NOW)
    second = poll_match_interchange(client=client, clock=lambda: NOW + timedelta(seconds=20))
    assert len(second[0]["appeared"]) == 1  # CD_I4
    assert len(second[0]["disappeared"]) == 1  # CD_I3
    assert len(second[0]["changed"]) == 1  # CD_I1 count


def test_continues_after_one_match_fails(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    add_match(conn, 8002, "CD_M2")
    _enabled(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonHttpError("boom", endpoint="match_interchange", status_code=500)],
        "CD_M2": [interchange_payload(home=[_entry("CD_I1")], match_id="CD_M2")],
    })
    results = poll_match_interchange(client=client, clock=lambda: NOW)
    outcomes = {r["match_provider_id"]: r["outcome"] for r in results}
    assert outcomes["CD_M1"] == "http_error"
    assert outcomes["CD_M2"] == "success"
    assert conn.execute("SELECT COUNT(*) FROM match_interchange_polls WHERE match_provider_id='CD_M1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM match_interchange_state WHERE match_id=8002").fetchone()[0] == 1


def test_distinguishes_and_persists_not_published(db, monkeypatch):
    """Endpoint-unavailable-before-publication handling."""
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [AflJsonResourceUnavailable("not yet", endpoint="match_interchange", status_code=404)]})
    results = poll_match_interchange(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "not_published"
    row = conn.execute("SELECT outcome FROM match_interchange_polls WHERE match_provider_id='CD_M1'").fetchone()
    assert row["outcome"] == "not_published"


def test_distinguishes_transport_failure(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [AflJsonTransportError("connection reset", endpoint="match_interchange")]})
    results = poll_match_interchange(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "transport_error"


def test_distinguishes_authentication_failure(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [AflJsonAuthenticationError("auth failed", endpoint="match_interchange", status_code=401)]})
    results = poll_match_interchange(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "auth_error"


def test_distinguishes_invalid_response(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [AflJsonInvalidResponse("bad json", endpoint="match_interchange", status_code=200)]})
    results = poll_match_interchange(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "invalid_response"


def test_distinguishes_malformed_top_level_payload(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [["not", "an", "object"]]})
    result = _capture_one(client, 8001, "CD_M1", clock=lambda: NOW)
    assert result["outcome"] == "malformed_payload"
    row = conn.execute("SELECT outcome FROM match_interchange_polls WHERE match_provider_id='CD_M1'").fetchone()
    assert row["outcome"] == "malformed_payload"
    assert conn.execute("SELECT COUNT(*) FROM match_interchange_state").fetchone()[0] == 0


def test_distinguishes_payload_for_a_different_match_as_malformed(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [interchange_payload(home=[_entry("CD_I1")], match_id="CD_M_WRONG")]})
    result = _capture_one(client, 8001, "CD_M1", clock=lambda: NOW)
    assert result["outcome"] == "malformed_payload"
    assert conn.execute("SELECT COUNT(*) FROM match_interchange_state").fetchone()[0] == 0


def test_polls_scheduled_match_whose_kickoff_has_passed_within_tolerance(db, monkeypatch):
    conn, _ = db
    start = NOW - timedelta(seconds=300)
    add_match(conn, 8001, "CD_M1", status="SCHEDULED", start=start.isoformat())
    _enabled(monkeypatch, kickoff_tolerance=600)
    client = FakeClient({"CD_M1": [interchange_payload()]})
    results = poll_match_interchange(client=client, clock=lambda: NOW)
    assert len(results) == 1


def test_container_restart_recovery_is_idempotent(db, monkeypatch):
    """A fresh poller instance (new client, same DB) after a simulated
    restart must not duplicate state/events for an unchanged payload."""
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    payload = interchange_payload(home=[_entry("CD_I1", count=3)])
    poll_match_interchange(client=FakeClient({"CD_M1": [payload]}), clock=lambda: NOW)
    # Simulate restart: brand-new client, same unchanged payload polled again.
    poll_match_interchange(client=FakeClient({"CD_M1": [payload]}), clock=lambda: NOW + timedelta(seconds=45))
    assert conn.execute("SELECT COUNT(*) FROM match_interchange_state").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM match_interchange_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM match_interchange_polls WHERE match_provider_id='CD_M1'").fetchone()[0] == 2


def test_settings_reject_non_positive_interval(monkeypatch):
    import config
    from scheduler.match_interchange_production import MatchInterchangeProductionSettings
    monkeypatch.setattr(config, "AFL_INTERCHANGE_PRODUCTION_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_INTERCHANGE_PRODUCTION_INTERVAL_SECONDS", 0, raising=False)
    monkeypatch.setattr(config, "AFL_INTERCHANGE_PRODUCTION_KICKOFF_TOLERANCE_SECONDS", 600, raising=False)
    with pytest.raises(ValueError):
        MatchInterchangeProductionSettings.from_config()
