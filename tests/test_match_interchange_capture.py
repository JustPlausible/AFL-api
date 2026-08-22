"""Offline tests for the opt-in diagnostic match-interchange capture scheduler job.

Exercises the sequential live-match poller and its APScheduler registration
against a fake CFS client and a migrated temporary SQLite database. No live
AFL/CFS access is required or attempted.
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
from diagnostics.framework import register_diagnostic_profile_job
from diagnostics.profiles.interchange import InterchangeProfile
from scheduler.match_interchange_capture import (
    MatchInterchangeCaptureSettings,
    capture_live_match_interchange,
)

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


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
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, scraped_at) VALUES(1,'R1',73,1,?)",
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


def _enable(monkeypatch, *, interval=15, kickoff_tolerance=None, post_live_grace=None):
    import config
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_PROFILES", ("interchange",), raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_INTERCHANGE_INTERVAL_SECONDS", interval, raising=False)
    if kickoff_tolerance is not None:
        monkeypatch.setattr(config, "AFL_DIAGNOSTIC_INTERCHANGE_KICKOFF_TOLERANCE_SECONDS", kickoff_tolerance, raising=False)
    if post_live_grace is not None:
        monkeypatch.setattr(config, "AFL_DIAGNOSTIC_INTERCHANGE_POST_LIVE_GRACE_SECONDS", post_live_grace, raising=False)


def _disable(monkeypatch):
    import config
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", False, raising=False)


def interchange_payload(*, home=None, away=None):
    return {
        "matchId": "CD_M1",
        "homeInterchange": home if home is not None else [],
        "awayInterchange": away if away is not None else [],
        "homeInterchangeCounts": {"totalInterchangeCount": 0.0, "interchangeCap": 75.0,
                                   "interchangeCountQ1": 0.0, "interchangeCountQ2": 0.0,
                                   "interchangeCountQ3": 0.0, "interchangeCountQ4": 0.0},
        "awayInterchangeCounts": {"totalInterchangeCount": 0.0, "interchangeCap": 75.0,
                                   "interchangeCountQ1": 0.0, "interchangeCountQ2": 0.0,
                                   "interchangeCountQ3": 0.0, "interchangeCountQ4": 0.0},
    }


def _player(player_id, *, count=1):
    return {
        "teamId": "CD_T10",
        "player": {"playerId": player_id, "playerName": {"givenName": "A", "surname": "B"}, "captain": False, "playerJumperNumber": 1},
        "interchangeCount": count, "benchReason": "ROTATION", "timeOnGround": 100, "timeOnBench": 10, "powerRating": 3,
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
            raise AflJsonHttpError("no more fixture responses", endpoint="match_interchange_diagnostic", status_code=500)
        payload = queue.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return SimpleNamespace(data=payload)


def test_capture_disabled_by_default_returns_empty_and_makes_no_calls(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _disable(monkeypatch)
    client = FakeClient({"CD_M1": [interchange_payload()]})
    assert capture_live_match_interchange(client=client) == []
    assert client.calls == []


def test_capture_skips_when_no_live_matches(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="SCHEDULED")
    _enable(monkeypatch)
    client = FakeClient({"CD_M1": [interchange_payload()]})
    assert capture_live_match_interchange(client=client) == []
    assert client.calls == []


def test_capture_polls_live_match_and_persists_first_observation(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({"CD_M1": [interchange_payload(home=[_player("CD_I1")])]})
    results = capture_live_match_interchange(client=client, clock=lambda: NOW)
    assert len(results) == 1
    assert results[0]["outcome"] == "success"
    assert results[0]["transitions"] == ["first_observation"]
    assert len(client.calls) == 1

    row = conn.execute(
        "SELECT is_transition, match_status_at_poll FROM match_interchange_evidence_observations WHERE match_provider_id='CD_M1'"
    ).fetchone()
    assert (row["is_transition"], row["match_status_at_poll"]) == (1, "LIVE")


def test_capture_detects_interchange_count_change_across_two_polls(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [
            interchange_payload(home=[_player("CD_I1", count=1)]),
            interchange_payload(home=[_player("CD_I1", count=2)]),
        ],
    })
    capture_live_match_interchange(client=client, clock=lambda: NOW)
    second = capture_live_match_interchange(client=client, clock=lambda: NOW + timedelta(seconds=15))
    assert second[0]["meaningful_transitions"] == ["player_interchange_count_changed"]

    count = conn.execute(
        "SELECT COUNT(*) FROM match_interchange_evidence_observations WHERE match_provider_id='CD_M1'"
    ).fetchone()[0]
    assert count == 2


def test_capture_records_a_separate_observed_at_per_match_not_one_shared_cycle_timestamp(db, monkeypatch):
    """Each match's observed_at must reflect when its own response actually
    arrived, not a single timestamp captured once before the poll loop --
    otherwise sequential per-match latency (retries, slow requests) would
    misrepresent transition cadence and this profile's own observed_at-driven
    post-LIVE grace window."""
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    add_match(conn, 8002, "CD_M2")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [interchange_payload(home=[_player("CD_I1")])],
        "CD_M2": [interchange_payload(home=[_player("CD_I2")])],
    })
    # A stepping clock simulates each request taking measurably longer than
    # the last -- e.g. retries/backoff on one match's request. The first tick
    # is consumed by candidate-window selection; one further tick per match.
    ticks = iter([NOW, NOW, NOW + timedelta(seconds=8)])
    capture_live_match_interchange(client=client, clock=lambda: next(ticks))

    rows = {
        row["match_provider_id"]: row["observed_at"]
        for row in conn.execute(
            "SELECT match_provider_id, observed_at FROM match_interchange_evidence_observations"
        ).fetchall()
    }
    assert rows["CD_M1"] == NOW.isoformat()
    assert rows["CD_M2"] == (NOW + timedelta(seconds=8)).isoformat()


def test_capture_continues_after_one_match_fails(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    add_match(conn, 8002, "CD_M2")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonHttpError("boom", endpoint="match_interchange_diagnostic", status_code=500)],
        "CD_M2": [interchange_payload(home=[_player("CD_I2")])],
    })
    results = capture_live_match_interchange(client=client, clock=lambda: NOW)
    outcomes = {r["match_provider_id"]: r["outcome"] for r in results}
    assert outcomes["CD_M1"] == "http_error"
    assert outcomes["CD_M2"] == "success"
    assert conn.execute(
        "SELECT COUNT(*) FROM match_interchange_evidence_observations"
    ).fetchone()[0] == 1


# --- Endpoint availability/error outcomes distinguished -----------------

def test_capture_distinguishes_not_published_from_other_errors(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonResourceUnavailable("not yet", endpoint="match_interchange_diagnostic", status_code=404)],
    })
    results = capture_live_match_interchange(client=client, clock=lambda: NOW)
    assert results == [{"match_id": 8001, "match_provider_id": "CD_M1", "outcome": "not_published"}]
    assert conn.execute("SELECT COUNT(*) FROM match_interchange_evidence_observations").fetchone()[0] == 0


def test_capture_distinguishes_transport_failure(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonTransportError("connection reset", endpoint="match_interchange_diagnostic")],
    })
    results = capture_live_match_interchange(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "transport_error"


def test_capture_distinguishes_authentication_failure(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonAuthenticationError("auth failed", endpoint="match_interchange_diagnostic", status_code=401)],
    })
    results = capture_live_match_interchange(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "auth_error"


def test_capture_distinguishes_malformed_payload(db, monkeypatch):
    """A non-object payload must be reported as malformed, not raise, and
    must not block the capture cycle, nor be persisted as an observation."""
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    from scheduler.match_interchange_capture import _capture_one
    client = FakeClient({"CD_M1": [["not", "an", "object"]]})
    result = _capture_one(client, 8001, "CD_M1", clock=lambda: NOW)
    assert result["outcome"] == "malformed_payload"
    assert conn.execute("SELECT COUNT(*) FROM match_interchange_evidence_observations").fetchone()[0] == 0


def test_capture_invalid_json_response_reported_distinctly(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonInvalidResponse("bad json", endpoint="match_interchange_diagnostic", status_code=200)],
    })
    results = capture_live_match_interchange(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "invalid_response"


# --- Kickoff tolerance / post-LIVE grace (reused/self-contained) ------------

def test_capture_polls_scheduled_match_whose_kickoff_has_passed_within_tolerance(db, monkeypatch):
    conn, _ = db
    start = NOW - timedelta(seconds=300)
    add_match(conn, 8001, "CD_M1", status="SCHEDULED", start=start.isoformat())
    _enable(monkeypatch, kickoff_tolerance=600)
    client = FakeClient({"CD_M1": [interchange_payload()]})
    results = capture_live_match_interchange(client=client, clock=lambda: NOW)
    assert len(results) == 1
    assert len(client.calls) == 1


def test_capture_skips_scheduled_match_whose_kickoff_is_beyond_tolerance(db, monkeypatch):
    conn, _ = db
    start = NOW - timedelta(seconds=1200)
    add_match(conn, 8001, "CD_M1", status="SCHEDULED", start=start.isoformat())
    _enable(monkeypatch, kickoff_tolerance=600)
    client = FakeClient({"CD_M1": [interchange_payload()]})
    assert capture_live_match_interchange(client=client, clock=lambda: NOW) == []
    assert client.calls == []


def test_capture_continues_after_local_status_leaves_live_within_grace(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="LIVE")
    _enable(monkeypatch, post_live_grace=600)
    client = FakeClient({"CD_M1": [interchange_payload(home=[_player("CD_I1")])]})
    first = capture_live_match_interchange(client=client, clock=lambda: NOW)
    assert len(first) == 1

    # The independently-scheduled local status refresh moves matches.status
    # away from LIVE before the next poll fires.
    conn.execute("UPDATE matches SET status='POSTGAME' WHERE match_id=8001")
    conn.commit()

    later = NOW + timedelta(seconds=15)
    client.calls.clear()
    client._payloads["CD_M1"] = [interchange_payload(home=[_player("CD_I1", count=2)])]
    second = capture_live_match_interchange(client=client, clock=lambda: later)
    assert len(second) == 1
    assert len(client.calls) == 1


def test_capture_stops_once_post_live_grace_expires(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="LIVE")
    _enable(monkeypatch, post_live_grace=60)
    client = FakeClient({"CD_M1": [interchange_payload(home=[_player("CD_I1")])]})
    capture_live_match_interchange(client=client, clock=lambda: NOW)

    conn.execute("UPDATE matches SET status='POSTGAME' WHERE match_id=8001")
    conn.commit()

    long_after = NOW + timedelta(seconds=600)
    client.calls.clear()
    assert capture_live_match_interchange(client=client, clock=lambda: long_after) == []
    assert client.calls == []


def test_capture_candidates_deduplicate_when_live_and_recently_live_overlap(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="LIVE")
    _enable(monkeypatch, post_live_grace=600)
    client = FakeClient({
        "CD_M1": [interchange_payload(home=[_player("CD_I1")])] * 2,
    })
    capture_live_match_interchange(client=client, clock=lambda: NOW)
    second = capture_live_match_interchange(client=client, clock=lambda: NOW + timedelta(seconds=15))
    assert len(second) == 1


# --- Restart safety: no in-memory state carries between independent calls --

def test_capture_state_is_fully_durable_across_independent_process_style_calls(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="LIVE")
    _enable(monkeypatch)

    client_before_restart = FakeClient({"CD_M1": [interchange_payload(home=[_player("CD_I1", count=1)])]})
    capture_live_match_interchange(client=client_before_restart, clock=lambda: NOW)

    # "Restart": a new client instance, no shared Python state except the DB.
    client_after_restart = FakeClient({"CD_M1": [interchange_payload(home=[_player("CD_I1", count=2)])]})
    results = capture_live_match_interchange(client=client_after_restart, clock=lambda: NOW + timedelta(seconds=15))
    assert results[0]["poll_sequence"] == 2
    assert results[0]["meaningful_transitions"] == ["player_interchange_count_changed"]

    rows = conn.execute(
        "SELECT poll_sequence FROM match_interchange_evidence_observations WHERE match_provider_id='CD_M1' ORDER BY poll_sequence"
    ).fetchall()
    assert [r["poll_sequence"] for r in rows] == [1, 2]


# --- Settings validation ------------------------------------------------

def test_settings_reject_non_positive_interval(monkeypatch):
    import config
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_PROFILES", ("interchange",), raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_INTERCHANGE_INTERVAL_SECONDS", 0, raising=False)
    with pytest.raises(ValueError):
        MatchInterchangeCaptureSettings.from_config()


@pytest.mark.parametrize("field", [
    "AFL_DIAGNOSTIC_INTERCHANGE_KICKOFF_TOLERANCE_SECONDS",
    "AFL_DIAGNOSTIC_INTERCHANGE_POST_LIVE_GRACE_SECONDS",
])
def test_settings_reject_negative_boundary_windows(monkeypatch, field):
    import config
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_PROFILES", ("interchange",), raising=False)
    monkeypatch.setattr(config, field, -1, raising=False)
    with pytest.raises(ValueError):
        MatchInterchangeCaptureSettings.from_config()


# --- Registration through the diagnostics framework -------------------------

class FakeScheduler:
    def __init__(self):
        self.jobs: list[dict] = []

    def add_job(self, func, *, trigger=None, args=None, id=None, name=None, replace_existing=True, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, "args": args, "id": id, "name": name})
        return SimpleNamespace(id=id)


def test_register_job_is_noop_when_capture_disabled(db, monkeypatch):
    _disable(monkeypatch)
    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, InterchangeProfile()) is False
    assert scheduler.jobs == []


def test_register_job_adds_interval_job_when_enabled(db, monkeypatch):
    _enable(monkeypatch, interval=20)
    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, InterchangeProfile()) is True
    assert len(scheduler.jobs) == 1
    job = scheduler.jobs[0]
    assert job["id"] == "diagnostic_interchange"
    assert job["trigger"].interval.total_seconds() == 20


def test_interchange_and_match_clock_are_independently_selectable(db, monkeypatch):
    """Enabling only 'interchange' must not implicitly enable/require match_clock,
    and vice versa -- the two profiles are independently schedulable."""
    import config
    from diagnostics.profiles.match_clock import MatchClockProfile
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_PROFILES", ("interchange",), raising=False)
    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, InterchangeProfile()) is True
    assert register_diagnostic_profile_job(scheduler, MatchClockProfile()) is False
    assert [job["id"] for job in scheduler.jobs] == ["diagnostic_interchange"]
