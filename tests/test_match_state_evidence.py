"""Offline tests for diagnostic matchItem evidence capture (Issue #148).

No live AFL/CFS access is required: parsing and transition detection are
pure functions, and persistence is exercised against a migrated temporary
SQLite database.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from collection.match_state_evidence import (
    MATCH_ITEM_ENDPOINT,
    MatchStateEvidenceError,
    TRANSITION_FIRST_OBSERVATION,
    TRANSITION_MATCH_STATUS_CHANGED,
    TRANSITION_NEW_PERIOD,
    TRANSITION_PERIOD_COMPLETED,
    TRANSITION_PERIOD_NUMBER_CHANGED,
    TRANSITION_SCORE_STATUS_CHANGED,
    TRANSITION_SECONDS_RESUMED,
    TRANSITION_SECONDS_STALLED,
    detect_transitions,
    evidence_rows,
    load_previous_observation,
    parse_match_item,
    persist_observation,
    recently_live_match_provider_ids,
)
from db.migration_runner import migrate_database

NOW = datetime(2026, 8, 15, 3, 0, tzinfo=timezone.utc)


def _iso(offset_seconds: int = 0) -> str:
    return datetime.fromtimestamp(NOW.timestamp() + offset_seconds, tz=timezone.utc).isoformat()


def match_item_payload(*, match_status="LIVE", score_status="LIVE", periods=None):
    return {
        "match": {"matchId": "CD_M20260142206", "status": match_status},
        "score": {"matchId": "CD_M20260142206", "status": score_status},
        "matchClock": {"periods": periods if periods is not None else []},
    }


# --- Endpoint isolation -----------------------------------------------------

def test_match_item_endpoint_is_unverified_and_not_in_shared_registry():
    from afl_json.contracts import ENDPOINTS
    assert MATCH_ITEM_ENDPOINT.verified is False
    assert MATCH_ITEM_ENDPOINT.name not in ENDPOINTS
    assert MATCH_ITEM_ENDPOINT.required_path_parameters == ("match_provider_id",)
    assert MATCH_ITEM_ENDPOINT.requires_auth is True


# --- Parsing ------------------------------------------------------------

def test_parse_match_item_extracts_latest_period_and_statuses():
    payload = match_item_payload(
        match_status="LIVE", score_status="LIVE",
        periods=[
            {"periodNumber": 1, "periodSeconds": 1789, "periodCompleted": True},
            {"periodNumber": 2, "periodSeconds": 1894, "periodCompleted": True},
            {"periodNumber": 3, "periodSeconds": 404, "periodCompleted": False},
        ],
    )
    observation = parse_match_item(payload, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    assert observation.match_status == "LIVE"
    assert observation.score_status == "LIVE"
    assert observation.latest_period_number == 3
    assert observation.latest_period_seconds == 404
    assert observation.latest_period_completed is False
    assert len(observation.periods) == 3
    assert observation.raw == payload


def test_parse_match_item_handles_missing_matchclock_and_empty_periods():
    payload = {"match": {"status": "SCHEDULED"}, "score": {"status": "SCHEDULED"}}
    observation = parse_match_item(payload, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    assert observation.periods == []
    assert observation.latest_period_number is None
    assert observation.latest_period_seconds is None
    assert observation.latest_period_completed is None


def test_parse_match_item_picks_highest_period_number_regardless_of_list_order():
    payload = match_item_payload(periods=[
        {"periodNumber": 2, "periodSeconds": 50, "periodCompleted": False},
        {"periodNumber": 1, "periodSeconds": 1800, "periodCompleted": True},
    ])
    observation = parse_match_item(payload, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    assert observation.latest_period_number == 2
    assert observation.latest_period_seconds == 50


def test_parse_match_item_rejects_non_object_payload():
    with pytest.raises(MatchStateEvidenceError):
        parse_match_item(None, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    with pytest.raises(MatchStateEvidenceError):
        parse_match_item([], match_id=1, match_provider_id="CD_M1", observed_at=_iso())


# --- Transition detection (pure) ----------------------------------------

def obs(match_id=1, provider="CD_M1", match_status="LIVE", score_status="LIVE",
        periods=None, latest_number=1, latest_seconds=100, latest_completed=False):
    from collection.match_state_evidence import MatchStateObservation
    return MatchStateObservation(
        observed_at=_iso(), match_id=match_id, match_provider_id=provider,
        match_status=match_status, score_status=score_status,
        periods=periods if periods is not None else [{"periodNumber": latest_number}],
        latest_period_number=latest_number, latest_period_seconds=latest_seconds,
        latest_period_completed=latest_completed, raw={},
    )


def test_first_observation_is_flagged_and_nothing_else():
    current = obs()
    assert detect_transitions(None, [], current) == [TRANSITION_FIRST_OBSERVATION]


def test_period_completed_transition_detected_when_flag_flips_true():
    previous = obs(latest_number=1, latest_seconds=1780, latest_completed=False)
    current = obs(latest_number=1, latest_seconds=1789, latest_completed=True,
                   periods=[{"periodNumber": 1}])
    flags = detect_transitions(previous, [], current)
    assert TRANSITION_PERIOD_COMPLETED in flags
    assert TRANSITION_PERIOD_NUMBER_CHANGED not in flags


def test_new_period_and_period_number_changed_detected_together():
    previous = obs(latest_number=1, latest_seconds=1800, latest_completed=True,
                   periods=[{"periodNumber": 1}])
    current = obs(latest_number=2, latest_seconds=5, latest_completed=False,
                   periods=[{"periodNumber": 1}, {"periodNumber": 2}])
    flags = detect_transitions(previous, [], current)
    assert TRANSITION_NEW_PERIOD in flags
    assert TRANSITION_PERIOD_NUMBER_CHANGED in flags
    # A brand new period cannot itself be "completed" going true.
    assert TRANSITION_PERIOD_COMPLETED not in flags


def test_period_seconds_stall_then_resume_across_consecutive_polls():
    previous = obs(latest_number=3, latest_seconds=400, latest_completed=False)
    stalled = obs(latest_number=3, latest_seconds=400, latest_completed=False)
    stalled_flags = detect_transitions(previous, [], stalled)
    assert stalled_flags == [TRANSITION_SECONDS_STALLED]

    resumed = obs(latest_number=3, latest_seconds=415, latest_completed=False)
    resumed_flags = detect_transitions(stalled, stalled_flags, resumed)
    assert TRANSITION_SECONDS_RESUMED in resumed_flags
    assert TRANSITION_SECONDS_STALLED not in resumed_flags


def test_seconds_unchanged_while_completed_is_not_flagged_as_an_anomalous_stall():
    previous = obs(latest_number=1, latest_seconds=1800, latest_completed=True)
    current = obs(latest_number=1, latest_seconds=1800, latest_completed=True)
    assert detect_transitions(previous, [], current) == []


def test_match_and_score_status_change_detected_independently():
    previous = obs(match_status="LIVE", score_status="LIVE", latest_seconds=100)
    current = obs(match_status="POSTGAME", score_status="LIVE", latest_seconds=115)
    flags = detect_transitions(previous, [], current)
    assert flags == [TRANSITION_MATCH_STATUS_CHANGED]

    current2 = obs(match_status="LIVE", score_status="POSTGAME", latest_seconds=115)
    flags2 = detect_transitions(previous, [], current2)
    assert flags2 == [TRANSITION_SCORE_STATUS_CHANGED]


def test_steady_progression_with_no_status_change_yields_no_transitions():
    previous = obs(latest_seconds=100)
    current = obs(latest_seconds=115)
    assert detect_transitions(previous, [], current) == []


def test_seconds_unchanged_between_polls_is_itself_flagged_as_a_stall():
    previous = obs(latest_seconds=100, latest_completed=False)
    current = obs(latest_seconds=100, latest_completed=False)
    assert detect_transitions(previous, [], current) == [TRANSITION_SECONDS_STALLED]


# --- Persistence ----------------------------------------------------------

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
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, start_time_utc, season_id, scraped_at) "
        "VALUES(8001,'CD_M1',1,'A','B','V','LIVE',?,73,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    yield conn
    conn.close()


def test_persist_observation_increments_poll_sequence_and_retains_raw_only_on_transitions(db):
    first = parse_match_item(
        match_item_payload(periods=[{"periodNumber": 1, "periodSeconds": 10, "periodCompleted": False}]),
        match_id=8001, match_provider_id="CD_M1", observed_at=_iso(0),
    )
    outcome1 = persist_observation(db, first, ["first_observation"])
    assert outcome1["poll_sequence"] == 1
    assert outcome1["is_transition"] is True

    second = parse_match_item(
        match_item_payload(periods=[{"periodNumber": 1, "periodSeconds": 25, "periodCompleted": False}]),
        match_id=8001, match_provider_id="CD_M1", observed_at=_iso(15),
    )
    outcome2 = persist_observation(db, second, [])
    assert outcome2["poll_sequence"] == 2
    assert outcome2["is_transition"] is False
    db.commit()

    rows = evidence_rows(db, match_provider_id="CD_M1")
    assert [row["poll_sequence"] for row in rows] == [1, 2]
    assert rows[0]["raw_match_item"] is not None
    assert rows[1]["raw_match_item"] is None
    assert rows[1]["latest_period_seconds"] == 25


def test_load_previous_observation_round_trips_state_for_change_detection(db):
    first = parse_match_item(
        match_item_payload(periods=[{"periodNumber": 1, "periodSeconds": 10, "periodCompleted": False}]),
        match_id=8001, match_provider_id="CD_M1", observed_at=_iso(0),
    )
    persist_observation(db, first, ["first_observation"])
    db.commit()

    previous, flags = load_previous_observation(db, "CD_M1")
    assert previous is not None
    assert previous.latest_period_number == 1
    assert previous.latest_period_seconds == 10
    assert flags == ["first_observation"]

    missing, missing_flags = load_previous_observation(db, "CD_M_unknown")
    assert missing is None
    assert missing_flags == []


def test_evidence_rows_filters_by_match_id_and_transitions_only(db):
    for seconds, completed, flags in [(10, False, ["first_observation"]), (25, False, []),
                                       (1800, True, ["latest_period_completed"])]:
        payload = match_item_payload(periods=[{"periodNumber": 1, "periodSeconds": seconds, "periodCompleted": completed}])
        observation = parse_match_item(payload, match_id=8001, match_provider_id="CD_M1", observed_at=_iso(seconds))
        persist_observation(db, observation, flags)
    db.commit()

    all_rows = evidence_rows(db, match_id=8001)
    assert len(all_rows) == 3
    transitions = evidence_rows(db, match_id=8001, transitions_only=True)
    assert [row["poll_sequence"] for row in transitions] == [1, 3]
    none_for_other_match = evidence_rows(db, match_id=9999)
    assert none_for_other_match == []


# --- Post-LIVE grace continuation query -----------------------------------

def _persist_at(db, *, match_id, match_provider_id, offset_seconds, match_status, score_status):
    payload = match_item_payload(
        match_status=match_status, score_status=score_status,
        periods=[{"periodNumber": 4, "periodSeconds": 1800, "periodCompleted": True}],
    )
    observation = parse_match_item(
        payload, match_id=match_id, match_provider_id=match_provider_id, observed_at=_iso(offset_seconds)
    )
    persist_observation(db, observation, [])


def test_recently_live_match_provider_ids_is_empty_when_never_observed_live(db):
    _persist_at(db, match_id=8001, match_provider_id="CD_M1", offset_seconds=0,
                match_status="SCHEDULED", score_status="SCHEDULED")
    db.commit()
    assert recently_live_match_provider_ids(db, now=NOW, grace_seconds=600) == []


def test_recently_live_match_provider_ids_continues_within_grace_after_last_live_sighting(db):
    _persist_at(db, match_id=8001, match_provider_id="CD_M1", offset_seconds=-40,
                match_status="LIVE", score_status="LIVE")
    # Local matches.status has since moved on; CFS-side LIVE was last seen 40s ago.
    _persist_at(db, match_id=8001, match_provider_id="CD_M1", offset_seconds=-10,
                match_status="POSTGAME", score_status="POSTGAME")
    db.commit()

    within_grace = recently_live_match_provider_ids(db, now=NOW, grace_seconds=600)
    assert within_grace == [(8001, "CD_M1")]

    expired_grace = recently_live_match_provider_ids(db, now=NOW, grace_seconds=30)
    assert expired_grace == []


def test_recently_live_match_provider_ids_zero_grace_disables_continuation(db):
    _persist_at(db, match_id=8001, match_provider_id="CD_M1", offset_seconds=0,
                match_status="LIVE", score_status="LIVE")
    db.commit()
    assert recently_live_match_provider_ids(db, now=NOW, grace_seconds=0) == []
