"""Offline tests for the opt-in diagnostic match-commentary capture scheduler job.

Exercises the sequential live-match poller and its APScheduler registration
against a fake CFS client and a migrated temporary SQLite database. No live
AFL/CFS access is required or attempted. Mirrors
tests/test_match_interchange_capture.py's shape, with additional coverage
for commentary's own choice to persist a poll row for every outcome
(success or failure) -- see collection/match_commentary_evidence.py.
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
from diagnostics.profiles.commentary import CommentaryProfile
from scheduler.match_commentary_capture import (
    MatchCommentaryCaptureSettings,
    capture_live_match_commentary,
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
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_PROFILES", ("commentary",), raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_COMMENTARY_INTERVAL_SECONDS", interval, raising=False)
    if kickoff_tolerance is not None:
        monkeypatch.setattr(config, "AFL_DIAGNOSTIC_COMMENTARY_KICKOFF_TOLERANCE_SECONDS", kickoff_tolerance, raising=False)
    if post_live_grace is not None:
        monkeypatch.setattr(config, "AFL_DIAGNOSTIC_COMMENTARY_POST_LIVE_GRACE_SECONDS", post_live_grace, raising=False)


def _disable(monkeypatch):
    import config
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", False, raising=False)


def _event(*, comment="Q1 is now underway.", period_number=1, period_seconds=0,
           player_id=None, team_id=None, score_event=False):
    return {
        "comment": comment, "periodNumber": period_number, "periodSeconds": period_seconds,
        "playerId": player_id, "teamId": team_id, "scoreEvent": score_event,
    }


def commentary_payload(*, events=None, match_id="CD_M1"):
    return {"matchId": match_id, "lastUpdated": "2026-08-22T03:00:00.000+0000",
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
            raise AflJsonHttpError("no more fixture responses", endpoint="match_commentary_diagnostic", status_code=500)
        payload = queue.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return SimpleNamespace(data=payload)


def test_capture_disabled_by_default_returns_empty_and_makes_no_calls(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _disable(monkeypatch)
    client = FakeClient({"CD_M1": [commentary_payload()]})
    assert capture_live_match_commentary(client=client) == []
    assert client.calls == []


def test_capture_skips_when_no_live_matches(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="SCHEDULED")
    _enable(monkeypatch)
    client = FakeClient({"CD_M1": [commentary_payload()]})
    assert capture_live_match_commentary(client=client) == []
    assert client.calls == []


def test_capture_polls_live_match_and_persists_first_observation(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({"CD_M1": [commentary_payload(events=[_event()])]})
    results = capture_live_match_commentary(client=client, clock=lambda: NOW)
    assert len(results) == 1
    assert results[0]["outcome"] == "success"
    assert results[0]["new_event_count"] == 1
    assert len(client.calls) == 1

    row = conn.execute(
        "SELECT is_transition, match_status_at_poll FROM commentary_evidence_polls WHERE match_provider_id='CD_M1'"
    ).fetchone()
    assert (row["is_transition"], row["match_status_at_poll"]) == (1, "LIVE")
    event_count = conn.execute("SELECT COUNT(*) FROM commentary_evidence_events").fetchone()[0]
    assert event_count == 1


def test_capture_deduplicates_accumulated_feed_across_two_polls(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
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
    capture_live_match_commentary(client=client, clock=lambda: NOW)
    second = capture_live_match_commentary(client=client, clock=lambda: NOW + timedelta(seconds=15))
    assert second[0]["new_event_count"] == 1

    count = conn.execute("SELECT COUNT(*) FROM commentary_evidence_events WHERE match_provider_id='CD_M1'").fetchone()[0]
    assert count == 2


def test_capture_records_a_separate_observed_at_per_match_not_one_shared_cycle_timestamp(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    add_match(conn, 8002, "CD_M2")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [commentary_payload(events=[_event()], match_id="CD_M1")],
        "CD_M2": [commentary_payload(events=[_event()], match_id="CD_M2")],
    })
    ticks = iter([NOW, NOW, NOW + timedelta(seconds=8)])
    capture_live_match_commentary(client=client, clock=lambda: next(ticks))

    rows = {
        row["match_provider_id"]: row["observed_at"]
        for row in conn.execute("SELECT match_provider_id, observed_at FROM commentary_evidence_polls").fetchall()
    }
    assert rows["CD_M1"] == NOW.isoformat()
    assert rows["CD_M2"] == (NOW + timedelta(seconds=8)).isoformat()


# --- Per-match failure isolation ---------------------------------------------

def test_capture_continues_after_one_match_fails(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    add_match(conn, 8002, "CD_M2")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonHttpError("boom", endpoint="match_commentary_diagnostic", status_code=500)],
        "CD_M2": [commentary_payload(events=[_event()], match_id="CD_M2")],
    })
    results = capture_live_match_commentary(client=client, clock=lambda: NOW)
    outcomes = {r["match_provider_id"]: r["outcome"] for r in results}
    assert outcomes["CD_M1"] == "http_error"
    assert outcomes["CD_M2"] == "success"
    # Unlike interchange, commentary persists the failure poll row too.
    assert conn.execute("SELECT COUNT(*) FROM commentary_evidence_polls WHERE match_provider_id='CD_M1'").fetchone()[0] == 1
    assert conn.execute("SELECT outcome FROM commentary_evidence_polls WHERE match_provider_id='CD_M1'").fetchone()[0] == "http_error"
    assert conn.execute("SELECT COUNT(*) FROM commentary_evidence_events WHERE match_id=8002").fetchone()[0] == 1


# --- Endpoint availability/error outcomes distinguished and persisted -------

def test_capture_distinguishes_and_persists_not_published(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonResourceUnavailable("not yet", endpoint="match_commentary_diagnostic", status_code=404)],
    })
    results = capture_live_match_commentary(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "not_published"
    row = conn.execute("SELECT outcome, is_transition FROM commentary_evidence_polls WHERE match_provider_id='CD_M1'").fetchone()
    assert (row["outcome"], row["is_transition"]) == ("not_published", 1)


def test_capture_distinguishes_transport_failure(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonTransportError("connection reset", endpoint="match_commentary_diagnostic")],
    })
    results = capture_live_match_commentary(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "transport_error"


def test_capture_distinguishes_authentication_failure(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonAuthenticationError("auth failed", endpoint="match_commentary_diagnostic", status_code=401)],
    })
    results = capture_live_match_commentary(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "auth_error"


def test_capture_distinguishes_malformed_top_level_payload(db, monkeypatch):
    """A non-object payload is reported/persisted as malformed_payload, and
    must not block the capture cycle."""
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    from scheduler.match_commentary_capture import _capture_one
    client = FakeClient({"CD_M1": [["not", "an", "object"]]})
    result = _capture_one(client, 8001, "CD_M1", clock=lambda: NOW)
    assert result["outcome"] == "malformed_payload"
    row = conn.execute("SELECT outcome FROM commentary_evidence_polls WHERE match_provider_id='CD_M1'").fetchone()
    assert row["outcome"] == "malformed_payload"
    assert conn.execute("SELECT COUNT(*) FROM commentary_evidence_events").fetchone()[0] == 0


def test_capture_missing_commentary_array_is_not_a_hard_failure(db, monkeypatch):
    """A dict payload with a missing/malformed commentaryEvent field parses
    successfully (network + JSON shape are fine); only the array itself is
    unknown for this poll."""
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({"CD_M1": [{"matchId": "CD_M1"}]})
    results = capture_live_match_commentary(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "success"
    assert results[0]["event_count_in_feed"] is None
    row = conn.execute("SELECT event_count_in_feed FROM commentary_evidence_polls WHERE match_provider_id='CD_M1'").fetchone()
    assert row["event_count_in_feed"] is None


def test_capture_invalid_json_response_reported_distinctly(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonInvalidResponse("bad json", endpoint="match_commentary_diagnostic", status_code=200)],
    })
    results = capture_live_match_commentary(client=client, clock=lambda: NOW)
    assert results[0]["outcome"] == "invalid_response"


# --- Kickoff tolerance / post-LIVE grace (reused/self-contained) ------------

def test_capture_polls_scheduled_match_whose_kickoff_has_passed_within_tolerance(db, monkeypatch):
    conn, _ = db
    start = NOW - timedelta(seconds=300)
    add_match(conn, 8001, "CD_M1", status="SCHEDULED", start=start.isoformat())
    _enable(monkeypatch, kickoff_tolerance=600)
    client = FakeClient({"CD_M1": [commentary_payload()]})
    results = capture_live_match_commentary(client=client, clock=lambda: NOW)
    assert len(results) == 1
    assert len(client.calls) == 1


def test_capture_skips_scheduled_match_whose_kickoff_is_beyond_tolerance(db, monkeypatch):
    conn, _ = db
    start = NOW - timedelta(seconds=1200)
    add_match(conn, 8001, "CD_M1", status="SCHEDULED", start=start.isoformat())
    _enable(monkeypatch, kickoff_tolerance=600)
    client = FakeClient({"CD_M1": [commentary_payload()]})
    assert capture_live_match_commentary(client=client, clock=lambda: NOW) == []
    assert client.calls == []


def test_capture_continues_after_local_status_leaves_live_within_grace(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="LIVE")
    _enable(monkeypatch, post_live_grace=600)
    client = FakeClient({"CD_M1": [commentary_payload(events=[_event()])]})
    first = capture_live_match_commentary(client=client, clock=lambda: NOW)
    assert len(first) == 1

    conn.execute("UPDATE matches SET status='POSTGAME' WHERE match_id=8001")
    conn.commit()

    later = NOW + timedelta(seconds=15)
    client.calls.clear()
    client._payloads["CD_M1"] = [commentary_payload(events=[_event()])]
    second = capture_live_match_commentary(client=client, clock=lambda: later)
    assert len(second) == 1
    assert len(client.calls) == 1


def test_capture_stops_once_post_live_grace_expires(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="LIVE")
    _enable(monkeypatch, post_live_grace=60)
    client = FakeClient({"CD_M1": [commentary_payload(events=[_event()])]})
    capture_live_match_commentary(client=client, clock=lambda: NOW)

    conn.execute("UPDATE matches SET status='POSTGAME' WHERE match_id=8001")
    conn.commit()

    long_after = NOW + timedelta(seconds=600)
    client.calls.clear()
    assert capture_live_match_commentary(client=client, clock=lambda: long_after) == []
    assert client.calls == []


# --- Restart safety: no in-memory state carries between independent calls --

def test_capture_state_is_fully_durable_across_independent_process_style_calls(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="LIVE")
    _enable(monkeypatch)

    client_before_restart = FakeClient({"CD_M1": [commentary_payload(events=[_event()])]})
    capture_live_match_commentary(client=client_before_restart, clock=lambda: NOW)

    client_after_restart = FakeClient({"CD_M1": [commentary_payload(events=[
        _event(),
        _event(comment="GOAL - Crows (Toby Murray)", period_number=1, period_seconds=500,
               player_id="CD_I1", team_id="CD_T10", score_event=True),
    ])]})
    results = capture_live_match_commentary(client=client_after_restart, clock=lambda: NOW + timedelta(seconds=15))
    assert results[0]["poll_sequence"] == 2
    assert results[0]["new_event_count"] == 1

    rows = conn.execute(
        "SELECT poll_sequence FROM commentary_evidence_polls WHERE match_provider_id='CD_M1' ORDER BY poll_sequence"
    ).fetchall()
    assert [r["poll_sequence"] for r in rows] == [1, 2]


# --- Settings validation ------------------------------------------------

def test_settings_reject_non_positive_interval(monkeypatch):
    import config
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_PROFILES", ("commentary",), raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_COMMENTARY_INTERVAL_SECONDS", 0, raising=False)
    with pytest.raises(ValueError):
        MatchCommentaryCaptureSettings.from_config()


@pytest.mark.parametrize("field", [
    "AFL_DIAGNOSTIC_COMMENTARY_KICKOFF_TOLERANCE_SECONDS",
    "AFL_DIAGNOSTIC_COMMENTARY_POST_LIVE_GRACE_SECONDS",
])
def test_settings_reject_negative_boundary_windows(monkeypatch, field):
    import config
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_PROFILES", ("commentary",), raising=False)
    monkeypatch.setattr(config, field, -1, raising=False)
    with pytest.raises(ValueError):
        MatchCommentaryCaptureSettings.from_config()


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
    assert register_diagnostic_profile_job(scheduler, CommentaryProfile()) is False
    assert scheduler.jobs == []


def test_register_job_adds_interval_job_when_enabled(db, monkeypatch):
    _enable(monkeypatch, interval=20)
    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, CommentaryProfile()) is True
    assert len(scheduler.jobs) == 1
    job = scheduler.jobs[0]
    assert job["id"] == "diagnostic_commentary"
    assert job["trigger"].interval.total_seconds() == 20


def test_commentary_and_other_profiles_are_independently_selectable(db, monkeypatch):
    """Enabling only 'commentary' must not implicitly enable/require
    match_clock or interchange, and vice versa."""
    import config
    from diagnostics.profiles.interchange import InterchangeProfile
    from diagnostics.profiles.match_clock import MatchClockProfile
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_PROFILES", ("commentary",), raising=False)
    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, CommentaryProfile()) is True
    assert register_diagnostic_profile_job(scheduler, MatchClockProfile()) is False
    assert register_diagnostic_profile_job(scheduler, InterchangeProfile()) is False
    assert [job["id"] for job in scheduler.jobs] == ["diagnostic_commentary"]


def test_all_three_profiles_register_together(db, monkeypatch):
    import config
    from diagnostics.profiles.interchange import InterchangeProfile
    from diagnostics.profiles.match_clock import MatchClockProfile
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_PROFILES", ("match_clock", "interchange", "commentary"), raising=False)
    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, MatchClockProfile()) is True
    assert register_diagnostic_profile_job(scheduler, InterchangeProfile()) is True
    assert register_diagnostic_profile_job(scheduler, CommentaryProfile()) is True
    assert {job["id"] for job in scheduler.jobs} == {
        "diagnostic_match_clock", "diagnostic_interchange", "diagnostic_commentary",
    }
