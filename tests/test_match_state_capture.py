"""Offline tests for the opt-in diagnostic match-state capture scheduler job.

Exercises the sequential live-match poller and its APScheduler registration
against a fake CFS client and a migrated temporary SQLite database. No live
AFL/CFS access is required or attempted.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
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


def add_match(conn, match_id, provider, status="LIVE"):
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, start_time_utc, season_id, scraped_at) "
        "VALUES(?,?,1,'A','B','V',?,?,73,?)",
        (match_id, provider, status, NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()


def _enable(monkeypatch, *, interval=15):
    import config
    monkeypatch.setattr(config, "AFL_CAPTURE_MATCH_STATE_EVIDENCE", True, raising=False)
    monkeypatch.setattr(config, "AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS", interval, raising=False)


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


def test_settings_reject_non_positive_interval(monkeypatch):
    import config
    monkeypatch.setattr(config, "AFL_CAPTURE_MATCH_STATE_EVIDENCE", True, raising=False)
    monkeypatch.setattr(config, "AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS", 0, raising=False)
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
