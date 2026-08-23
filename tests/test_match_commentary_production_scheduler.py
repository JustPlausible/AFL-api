"""Offline tests for the production match-commentary polling scheduler (Issue #201).

Exercises the sequential in-window match poller against a fake CFS client
and a migrated temporary SQLite database. No live AFL/CFS access is required
or attempted. Mirrors tests/test_match_commentary_capture.py's shape (the
diagnostic counterpart this module is deliberately independent from), plus
coverage specific to production: POSTGAME candidacy and the always-on
(non-diagnostics-gated) enable flag.
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
from scheduler.match_commentary_production import _capture_one, poll_match_commentary

NOW = datetime(2026, 8, 23, 3, 0, tzinfo=timezone.utc)


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


def _enabled(monkeypatch, *, interval=20, kickoff_tolerance=None, postgame_grace=None):
    import config
    monkeypatch.setattr(config, "AFL_COMMENTARY_PRODUCTION_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_COMMENTARY_PRODUCTION_INTERVAL_SECONDS", interval, raising=False)
    if kickoff_tolerance is not None:
        monkeypatch.setattr(config, "AFL_COMMENTARY_PRODUCTION_KICKOFF_TOLERANCE_SECONDS", kickoff_tolerance, raising=False)
    if postgame_grace is not None:
        monkeypatch.setattr(config, "AFL_COMMENTARY_PRODUCTION_POSTGAME_GRACE_SECONDS", postgame_grace, raising=False)


def _disabled(monkeypatch):
    import config
    monkeypatch.setattr(config, "AFL_COMMENTARY_PRODUCTION_ENABLED", False, raising=False)


def _event(*, comment="Q1 is now underway.", period_number=1, period_seconds=0,
           player_id=None, team_id=None, score_event=False):
    return {
        "comment": comment, "periodNumber": period_number, "periodSeconds": period_seconds,
        "playerId": player_id, "teamId": team_id, "scoreEvent": score_event,
    }


def commentary_payload(*, events=None, match_id="CD_M1"):
    return {"matchId": match_id, "lastUpdated": "2026-08-23T03:00:00.000+0000",
            "commentaryEvent": events if events is not None else []}


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
            raise AflJsonHttpError("no more fixture responses", endpoint="match_commentary", status_code=500)
        payload = queue.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return SimpleNamespace(data=payload)


def test_disabled_by_default_override_returns_empty_and_makes_no_calls(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _disabled(monkeypatch)
    client = FakeClient({"CD_M1": [commentary_payload()]})
    assert poll_match_commentary(client=client) == []
    assert client.calls == []


def test_no_calls_when_no_in_window_matches(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="SCHEDULED")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [commentary_payload()]})
    assert poll_match_commentary(client=client) == []
    assert client.calls == []


def test_polls_live_match_and_persists_first_observation(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [commentary_payload(events=[_event()])]})
    results = poll_match_commentary(client=client, clock=lambda: NOW)
    assert len(results) == 1
    assert results[0]["outcome"] == "success"
    assert results[0]["new_event_count"] == 1
    event_count = conn.execute("SELECT COUNT(*) FROM match_commentary_events").fetchone()[0]
    assert event_count == 1


def test_polls_postgame_match_not_only_live(db, monkeypatch):
    """Confirms Issue #201's requirement to keep polling through POSTGAME so
    a late-arriving scoring-outcome change is not missed."""
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="POSTGAME")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [commentary_payload(events=[_event()])]})
    results = poll_match_commentary(client=client, clock=lambda: NOW)
    assert len(results) == 1
    assert results[0]["outcome"] == "success"


def test_deduplicates_accumulated_feed_across_two_polls(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({
        "CD_M1": [
            commentary_payload(events=[_event()]),
            commentary_payload(events=[
                _event(comment="GOAL - Crows (Toby Murray)", period_number=1, period_seconds=500,
                       player_id="CD_I1", team_id="CD_T10", score_event=True),
                _event(),
            ]),
        ],
    })
    poll_match_commentary(client=client, clock=lambda: NOW)
    second = poll_match_commentary(client=client, clock=lambda: NOW + timedelta(seconds=20))
    assert second[0]["new_event_count"] == 1
    count = conn.execute("SELECT COUNT(*) FROM match_commentary_events WHERE match_provider_id='CD_M1'").fetchone()[0]
    assert count == 2


def test_continues_after_one_match_fails(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    add_match(conn, 8002, "CD_M2")
    _enabled(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonHttpError("boom", endpoint="match_commentary", status_code=500)],
        "CD_M2": [commentary_payload(events=[_event()], match_id="CD_M2")],
    })
    results = poll_match_commentary(client=client, clock=lambda: NOW)
    outcomes = {r["match_provider_id"]: r["outcome"] for r in results}
    assert outcomes["CD_M1"] == "http_error"
    assert outcomes["CD_M2"] == "success"
    assert conn.execute("SELECT COUNT(*) FROM match_commentary_polls WHERE match_provider_id='CD_M1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM match_commentary_events WHERE match_id=8002").fetchone()[0] == 1


def test_distinguishes_and_persists_not_published(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [AflJsonResourceUnavailable("not yet", endpoint="match_commentary", status_code=404)]})
    results = poll_match_commentary(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "not_published"
    row = conn.execute("SELECT outcome FROM match_commentary_polls WHERE match_provider_id='CD_M1'").fetchone()
    assert row["outcome"] == "not_published"


def test_distinguishes_transport_failure(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [AflJsonTransportError("connection reset", endpoint="match_commentary")]})
    results = poll_match_commentary(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "transport_error"


def test_distinguishes_authentication_failure(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [AflJsonAuthenticationError("auth failed", endpoint="match_commentary", status_code=401)]})
    results = poll_match_commentary(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "auth_error"


def test_distinguishes_invalid_response(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [AflJsonInvalidResponse("bad json", endpoint="match_commentary", status_code=200)]})
    results = poll_match_commentary(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "invalid_response"


def test_distinguishes_malformed_top_level_payload(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [["not", "an", "object"]]})
    result = _capture_one(client, 8001, "CD_M1", clock=lambda: NOW)
    assert result["outcome"] == "malformed_payload"
    row = conn.execute("SELECT outcome FROM match_commentary_polls WHERE match_provider_id='CD_M1'").fetchone()
    assert row["outcome"] == "malformed_payload"
    assert conn.execute("SELECT COUNT(*) FROM match_commentary_events").fetchone()[0] == 0


def test_distinguishes_payload_for_a_different_match_as_malformed(db, monkeypatch):
    """A CFS response whose own matchId disagrees with the requested match
    must never be persisted against the candidate match -- it is treated
    the same as any other malformed payload."""
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enabled(monkeypatch)
    client = FakeClient({"CD_M1": [commentary_payload(events=[_event()], match_id="CD_M_WRONG")]})
    result = _capture_one(client, 8001, "CD_M1", clock=lambda: NOW)
    assert result["outcome"] == "malformed_payload"
    assert conn.execute("SELECT COUNT(*) FROM match_commentary_events").fetchone()[0] == 0


def test_polls_scheduled_match_whose_kickoff_has_passed_within_tolerance(db, monkeypatch):
    conn, _ = db
    start = NOW - timedelta(seconds=300)
    add_match(conn, 8001, "CD_M1", status="SCHEDULED", start=start.isoformat())
    _enabled(monkeypatch, kickoff_tolerance=600)
    client = FakeClient({"CD_M1": [commentary_payload()]})
    results = poll_match_commentary(client=client, clock=lambda: NOW)
    assert len(results) == 1


def test_settings_reject_non_positive_interval(monkeypatch):
    import config
    from scheduler.match_commentary_production import MatchCommentaryProductionSettings
    monkeypatch.setattr(config, "AFL_COMMENTARY_PRODUCTION_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_COMMENTARY_PRODUCTION_INTERVAL_SECONDS", 0, raising=False)
    monkeypatch.setattr(config, "AFL_COMMENTARY_PRODUCTION_KICKOFF_TOLERANCE_SECONDS", 600, raising=False)
    monkeypatch.setattr(config, "AFL_COMMENTARY_PRODUCTION_POSTGAME_GRACE_SECONDS", 1800, raising=False)
    with pytest.raises(ValueError):
        MatchCommentaryProductionSettings.from_config()
