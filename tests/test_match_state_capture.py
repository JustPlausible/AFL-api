"""Offline tests for the opt-in diagnostic match-state capture scheduler job.

Exercises the sequential live-match poller and its APScheduler registration
against a fake CFS client and a migrated temporary SQLite database. No live
AFL/CFS access is required or attempted.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from afl_json.client import AflJsonHttpError
from db.migration_runner import migrate_database
from scheduler.match_state_capture import (
    MatchStateCaptureSettings,
    capture_live_match_state,
    register_match_state_capture_job,
)

NOW = datetime(2026, 8, 15, 3, 0, tzinfo=timezone.utc)


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
    monkeypatch.setattr(config, "AFL_CAPTURE_MATCH_STATE_EVIDENCE", True, raising=False)
    monkeypatch.setattr(config, "AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS", interval, raising=False)
    if kickoff_tolerance is not None:
        monkeypatch.setattr(config, "AFL_MATCH_STATE_CAPTURE_KICKOFF_TOLERANCE_SECONDS", kickoff_tolerance, raising=False)
    if post_live_grace is not None:
        monkeypatch.setattr(config, "AFL_MATCH_STATE_CAPTURE_POST_LIVE_GRACE_SECONDS", post_live_grace, raising=False)


def _disable(monkeypatch):
    import config
    monkeypatch.setattr(config, "AFL_CAPTURE_MATCH_STATE_EVIDENCE", False, raising=False)


def match_item_payload(periods):
    return {
        "match": {"status": "LIVE"},
        "score": {"status": "LIVE"},
        "matchClock": {"periods": periods},
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
            raise AflJsonHttpError("no more fixture responses", endpoint="match_item_diagnostic", status_code=500)
        payload = queue.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return SimpleNamespace(data=payload)


def test_capture_disabled_by_default_returns_empty_and_makes_no_calls(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _disable(monkeypatch)
    client = FakeClient({"CD_M1": [match_item_payload([])]})
    assert capture_live_match_state(client=client) == []
    assert client.calls == []


def test_capture_skips_when_no_live_matches(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="SCHEDULED")
    _enable(monkeypatch)
    client = FakeClient({"CD_M1": [match_item_payload([])]})
    assert capture_live_match_state(client=client) == []
    assert client.calls == []


def test_capture_polls_live_match_and_persists_first_observation(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [match_item_payload([{"periodNumber": 1, "periodSeconds": 10, "periodCompleted": False}])],
    })
    results = capture_live_match_state(client=client)
    assert len(results) == 1
    assert results[0]["match_id"] == 8001
    assert results[0]["transitions"] == ["first_observation"]
    assert len(client.calls) == 1

    row = conn.execute(
        "SELECT latest_period_number, latest_period_seconds, is_transition FROM match_state_evidence_observations WHERE match_provider_id='CD_M1'"
    ).fetchone()
    assert (row["latest_period_number"], row["latest_period_seconds"], row["is_transition"]) == (1, 10, 1)


def test_capture_detects_period_completed_transition_across_two_polls(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [
            match_item_payload([{"periodNumber": 1, "periodSeconds": 1780, "periodCompleted": False}]),
            match_item_payload([{"periodNumber": 1, "periodSeconds": 1789, "periodCompleted": True}]),
        ],
    })
    capture_live_match_state(client=client)
    second = capture_live_match_state(client=client)
    assert second[0]["transitions"] == ["latest_period_completed"]

    count = conn.execute(
        "SELECT COUNT(*) FROM match_state_evidence_observations WHERE match_provider_id='CD_M1'"
    ).fetchone()[0]
    assert count == 2


def test_capture_continues_after_one_match_fails(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1")
    add_match(conn, 8002, "CD_M2")
    _enable(monkeypatch)
    client = FakeClient({
        "CD_M1": [AflJsonHttpError("boom", endpoint="match_item_diagnostic", status_code=500)],
        "CD_M2": [match_item_payload([{"periodNumber": 2, "periodSeconds": 5, "periodCompleted": False}])],
    })
    results = capture_live_match_state(client=client)
    assert len(results) == 1
    assert results[0]["match_provider_id"] == "CD_M2"
    assert conn.execute(
        "SELECT COUNT(*) FROM match_state_evidence_observations"
    ).fetchone()[0] == 1


# --- Kickoff tolerance boundary (local status lags real LIVE transition) --

def test_capture_polls_scheduled_match_whose_kickoff_has_passed_within_tolerance(db, monkeypatch):
    conn, _ = db
    start = NOW - timedelta(seconds=300)
    add_match(conn, 8001, "CD_M1", status="SCHEDULED", start=start.isoformat())
    _enable(monkeypatch, kickoff_tolerance=600)
    client = FakeClient({
        "CD_M1": [match_item_payload([{"periodNumber": 1, "periodSeconds": 5, "periodCompleted": False}])],
    })
    results = capture_live_match_state(client=client, clock=lambda: NOW)
    assert len(results) == 1
    assert len(client.calls) == 1


def test_capture_skips_scheduled_match_whose_kickoff_is_beyond_tolerance(db, monkeypatch):
    conn, _ = db
    start = NOW - timedelta(seconds=1200)
    add_match(conn, 8001, "CD_M1", status="SCHEDULED", start=start.isoformat())
    _enable(monkeypatch, kickoff_tolerance=600)
    client = FakeClient({"CD_M1": [match_item_payload([])]})
    assert capture_live_match_state(client=client, clock=lambda: NOW) == []
    assert client.calls == []


def test_capture_skips_scheduled_match_with_future_kickoff(db, monkeypatch):
    conn, _ = db
    start = NOW + timedelta(seconds=300)
    add_match(conn, 8001, "CD_M1", status="SCHEDULED", start=start.isoformat())
    _enable(monkeypatch, kickoff_tolerance=600)
    client = FakeClient({"CD_M1": [match_item_payload([])]})
    assert capture_live_match_state(client=client, clock=lambda: NOW) == []
    assert client.calls == []


def test_capture_kickoff_tolerance_disabled_by_zero_setting(db, monkeypatch):
    conn, _ = db
    start = NOW - timedelta(seconds=60)
    add_match(conn, 8001, "CD_M1", status="SCHEDULED", start=start.isoformat())
    _enable(monkeypatch, kickoff_tolerance=0)
    client = FakeClient({"CD_M1": [match_item_payload([])]})
    assert capture_live_match_state(client=client, clock=lambda: NOW) == []
    assert client.calls == []


# --- Post-LIVE grace boundary (local status can leave LIVE before the final
# Q4/full-time matchItem transition is captured) ----------------------------

def test_capture_continues_after_local_status_leaves_live_within_grace(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="LIVE")
    _enable(monkeypatch, post_live_grace=600)
    client = FakeClient({
        "CD_M1": [match_item_payload([{"periodNumber": 4, "periodSeconds": 1780, "periodCompleted": False}])],
    })
    first = capture_live_match_state(client=client, clock=lambda: NOW)
    assert len(first) == 1

    # The independently-scheduled ~5 minute status refresh moves matches.status
    # away from LIVE before the next 15s evidence poll fires.
    conn.execute("UPDATE matches SET status='POSTGAME' WHERE match_id=8001")
    conn.commit()

    later = NOW + timedelta(seconds=15)
    client.calls.clear()
    client._payloads["CD_M1"] = [
        match_item_payload([{"periodNumber": 4, "periodSeconds": 1789, "periodCompleted": True}])
    ]
    second = capture_live_match_state(client=client, clock=lambda: later)
    assert len(second) == 1
    assert second[0]["transitions"] == ["latest_period_completed"]
    assert len(client.calls) == 1


def test_capture_stops_once_post_live_grace_expires(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="LIVE")
    _enable(monkeypatch, post_live_grace=60)
    client = FakeClient({
        "CD_M1": [match_item_payload([{"periodNumber": 4, "periodSeconds": 1780, "periodCompleted": False}])],
    })
    capture_live_match_state(client=client, clock=lambda: NOW)

    conn.execute("UPDATE matches SET status='POSTGAME' WHERE match_id=8001")
    conn.commit()

    long_after = NOW + timedelta(seconds=600)
    client.calls.clear()
    assert capture_live_match_state(client=client, clock=lambda: long_after) == []
    assert client.calls == []


def test_capture_candidates_deduplicate_when_live_and_recently_live_overlap(db, monkeypatch):
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="LIVE")
    _enable(monkeypatch, post_live_grace=600)
    client = FakeClient({
        "CD_M1": [match_item_payload([{"periodNumber": 1, "periodSeconds": 5, "periodCompleted": False}])] * 2,
    })
    capture_live_match_state(client=client, clock=lambda: NOW)
    # Still LIVE locally on the next poll: must not be polled twice via both
    # the primary LIVE query and the post-LIVE grace query.
    second = capture_live_match_state(client=client, clock=lambda: NOW + timedelta(seconds=15))
    assert len(second) == 1


# --- Restart safety: no in-memory state carries between independent calls --

def test_capture_state_is_fully_durable_across_independent_process_style_calls(db, monkeypatch):
    """Simulates a scheduler restart: a brand-new client (as the pooled
    singleton would be after a process restart) and a fresh top-level call,
    sharing only the durable database. Sequencing and transition detection
    must be unaffected."""
    conn, _ = db
    add_match(conn, 8001, "CD_M1", status="LIVE")
    _enable(monkeypatch)

    client_before_restart = FakeClient({
        "CD_M1": [match_item_payload([{"periodNumber": 1, "periodSeconds": 10, "periodCompleted": False}])],
    })
    capture_live_match_state(client=client_before_restart, clock=lambda: NOW)

    # "Restart": a new client instance, no shared Python state except the DB.
    client_after_restart = FakeClient({
        "CD_M1": [match_item_payload([{"periodNumber": 1, "periodSeconds": 25, "periodCompleted": False}])],
    })
    results = capture_live_match_state(client=client_after_restart, clock=lambda: NOW + timedelta(seconds=15))
    assert results[0]["poll_sequence"] == 2
    assert results[0]["transitions"] == []

    rows = conn.execute(
        "SELECT poll_sequence, latest_period_seconds FROM match_state_evidence_observations "
        "WHERE match_provider_id='CD_M1' ORDER BY poll_sequence"
    ).fetchall()
    assert [(r["poll_sequence"], r["latest_period_seconds"]) for r in rows] == [(1, 10), (2, 25)]


def test_settings_reject_non_positive_interval(monkeypatch):
    import config
    monkeypatch.setattr(config, "AFL_CAPTURE_MATCH_STATE_EVIDENCE", True, raising=False)
    monkeypatch.setattr(config, "AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS", 0, raising=False)
    with pytest.raises(ValueError):
        MatchStateCaptureSettings.from_config()


@pytest.mark.parametrize("field", [
    "AFL_MATCH_STATE_CAPTURE_KICKOFF_TOLERANCE_SECONDS",
    "AFL_MATCH_STATE_CAPTURE_POST_LIVE_GRACE_SECONDS",
])
def test_settings_reject_negative_boundary_windows(monkeypatch, field):
    import config
    monkeypatch.setattr(config, "AFL_CAPTURE_MATCH_STATE_EVIDENCE", True, raising=False)
    monkeypatch.setattr(config, field, -1, raising=False)
    with pytest.raises(ValueError):
        MatchStateCaptureSettings.from_config()


class FakeScheduler:
    def __init__(self):
        self.jobs: list[dict] = []

    def add_job(self, func, *, trigger=None, args=None, id=None, name=None, replace_existing=True, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, "args": args, "id": id, "name": name})
        return SimpleNamespace(id=id)


def test_register_job_is_noop_when_capture_disabled(db, monkeypatch):
    _disable(monkeypatch)
    scheduler = FakeScheduler()
    assert register_match_state_capture_job(scheduler) is False
    assert scheduler.jobs == []


def test_register_job_adds_interval_job_when_enabled(db, monkeypatch):
    _enable(monkeypatch, interval=20)
    scheduler = FakeScheduler()
    assert register_match_state_capture_job(scheduler) is True
    assert len(scheduler.jobs) == 1
    job = scheduler.jobs[0]
    assert job["id"] == "match_state_evidence_capture"
    assert job["trigger"].interval.total_seconds() == 20
