"""Offline tests for diagnostic matchItem evidence capture (Issue #148).

No live AFL/CFS access is required: parsing and transition detection are
pure functions, and persistence is exercised against a migrated temporary
SQLite database.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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
    reparse_stored_raw_observations,
)
from db.migration_runner import migrate_database

NOW = datetime(2026, 8, 15, 3, 0, tzinfo=timezone.utc)
MATCH_ITEM_FIXTURES = Path(__file__).parent / "fixtures" / "afl" / "match_item"


def match_item_fixture(name: str) -> dict:
    return json.loads((MATCH_ITEM_FIXTURES / name).read_text())


def _iso(offset_seconds: int = 0) -> str:
    return datetime.fromtimestamp(NOW.timestamp() + offset_seconds, tz=timezone.utc).isoformat()


def match_item_payload(*, match_status="LIVE", score_status="LIVE", periods=None):
    # matchClock is nested under score in the real upstream payload, not a
    # top-level sibling of match/score -- confirmed from live capture.
    return {
        "match": {"matchId": "CD_M20260142206", "status": match_status},
        "score": {
            "matchId": "CD_M20260142206", "status": score_status,
            "matchClock": {"periods": periods if periods is not None else []},
        },
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


def test_parse_match_item_reads_matchclock_nested_under_score_not_top_level():
    """Top-level matchClock (the pre-fix, incorrect shape) must not be read."""
    payload = {
        "match": {"status": "LIVE"},
        "score": {"status": "LIVE", "matchClock": {"periods": [
            {"periodNumber": 1, "periodSeconds": 42, "periodCompleted": False},
        ]}},
        # A stray top-level matchClock must be ignored, not merged/preferred.
        "matchClock": {"periods": [{"periodNumber": 9, "periodSeconds": 9, "periodCompleted": True}]},
    }
    observation = parse_match_item(payload, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    assert observation.latest_period_number == 1
    assert observation.latest_period_seconds == 42
    assert observation.latest_period_completed is False


def test_parse_match_item_extracts_from_realistic_captured_payload_shape():
    """Exercises a fixture matching the real structure captured live on
    2026-08-16: score.matchClock.periods, plus round/venue siblings and
    unmodelled per-period fields (nextPeriodStart)."""
    payload = match_item_fixture("match_item_live_q2_underway.json")
    observation = parse_match_item(payload, match_id=8001, match_provider_id="CD_M20260149999", observed_at=_iso())
    assert observation.match_status == "LIVE"
    assert observation.score_status == "LIVE"
    assert len(observation.periods) == 2
    assert observation.latest_period_number == 2
    assert observation.latest_period_seconds == 240
    assert observation.latest_period_completed is False
    # Unmodelled per-period fields are preserved verbatim, not dropped.
    assert observation.periods[0]["nextPeriodStart"] == "2026-08-16T05:30:00.000+0000"
    assert observation.periods[0]["periodCompleted"] is True


# --- Representative match-progression states (Issue #148) -----------------

def test_state_q1_underway_period_1_present_not_completed():
    payload = match_item_payload(periods=[
        {"periodNumber": 1, "periodSeconds": 610, "periodCompleted": False},
    ])
    observation = parse_match_item(payload, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    assert observation.latest_period_number == 1
    assert observation.latest_period_seconds == 610
    assert observation.latest_period_completed is False


def test_state_quarter_time_period_1_present_completed():
    payload = match_item_payload(periods=[
        {"periodNumber": 1, "periodSeconds": 1789, "periodCompleted": True},
    ])
    observation = parse_match_item(payload, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    assert observation.latest_period_number == 1
    assert observation.latest_period_seconds == 1789
    assert observation.latest_period_completed is True


def test_state_q2_underway_periods_1_and_2_present_latest_is_2():
    payload = match_item_payload(periods=[
        {"periodNumber": 1, "periodSeconds": 1789, "periodCompleted": True},
        {"periodNumber": 2, "periodSeconds": 90, "periodCompleted": False},
    ])
    observation = parse_match_item(payload, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    assert len(observation.periods) == 2
    assert observation.latest_period_number == 2
    assert observation.latest_period_seconds == 90
    assert observation.latest_period_completed is False
    assert observation.periods[0]["periodCompleted"] is True


def test_state_final_postgame_all_supplied_completed_periods_present():
    payload = match_item_payload(
        match_status="POSTGAME", score_status="POSTGAME",
        periods=[
            {"periodNumber": 1, "periodSeconds": 1789, "periodCompleted": True},
            {"periodNumber": 2, "periodSeconds": 1894, "periodCompleted": True},
            {"periodNumber": 3, "periodSeconds": 1820, "periodCompleted": True},
            {"periodNumber": 4, "periodSeconds": 1805, "periodCompleted": True},
        ],
    )
    observation = parse_match_item(payload, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    assert observation.match_status == "POSTGAME"
    assert observation.score_status == "POSTGAME"
    assert len(observation.periods) == 4
    assert observation.latest_period_number == 4
    assert observation.latest_period_seconds == 1805
    assert observation.latest_period_completed is True
    assert all(period["periodCompleted"] for period in observation.periods)


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


# --- Reparse/backfill of already-stored raw evidence -----------------------

def _insert_pre_fix_row(db, *, match_id, match_provider_id, poll_sequence, raw_payload,
                        with_raw=True, is_transition=1):
    """Simulate a row exactly as the pre-fix parser would have written it:
    raw_match_item_json retained (or not), but periods/latest_period_* wrongly
    empty/null because matchClock was looked up at the wrong path."""
    db.execute(
        """INSERT INTO match_state_evidence_observations(
               match_id, match_provider_id, poll_sequence, observed_at, match_status, score_status,
               periods_json, latest_period_number, latest_period_seconds, latest_period_completed,
               is_transition, transition_flags_json, raw_match_item_json, collector_version
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            match_id, match_provider_id, poll_sequence, _iso(poll_sequence),
            raw_payload["match"]["status"], raw_payload["score"]["status"],
            "[]", None, None, None,
            is_transition, json.dumps(["first_observation"] if poll_sequence == 1 else []),
            json.dumps(raw_payload) if with_raw else None,
            "match_state_evidence_v1",
        ),
    )


def test_reparse_recovers_period_fields_from_retained_raw_payload_only(db):
    live_payload = match_item_payload(periods=[
        {"periodNumber": 1, "periodSeconds": 1789, "periodCompleted": True},
    ])
    # A transition row with raw JSON retained (recoverable).
    _insert_pre_fix_row(db, match_id=8001, match_provider_id="CD_M1", poll_sequence=1,
                        raw_payload=live_payload, with_raw=True, is_transition=1)
    # An ordinary lightweight row with no raw JSON retained (not recoverable).
    _insert_pre_fix_row(db, match_id=8001, match_provider_id="CD_M1", poll_sequence=2,
                        raw_payload=live_payload, with_raw=False, is_transition=0)
    db.commit()

    results = reparse_stored_raw_observations(db)
    db.commit()

    assert len(results) == 1
    assert results[0]["id"] == 1
    assert results[0]["changed"] is True
    assert results[0]["after"]["latest_period_number"] == 1
    assert results[0]["after"]["latest_period_seconds"] == 1789
    assert results[0]["after"]["latest_period_completed"] is True

    recovered = db.execute(
        "SELECT latest_period_number, latest_period_seconds, latest_period_completed, "
        "is_transition, transition_flags_json, poll_sequence, observed_at "
        "FROM match_state_evidence_observations WHERE poll_sequence=1"
    ).fetchone()
    assert (recovered["latest_period_number"], recovered["latest_period_seconds"],
            bool(recovered["latest_period_completed"])) == (1, 1789, True)
    # Only period fields change; identity/audit columns are untouched.
    assert recovered["is_transition"] == 1
    assert json.loads(recovered["transition_flags_json"]) == ["first_observation"]

    untouched = db.execute(
        "SELECT periods_json, latest_period_number FROM match_state_evidence_observations WHERE poll_sequence=2"
    ).fetchone()
    assert untouched["periods_json"] == "[]"
    assert untouched["latest_period_number"] is None


def test_reparse_dry_run_reports_without_writing(db):
    live_payload = match_item_payload(periods=[
        {"periodNumber": 1, "periodSeconds": 1789, "periodCompleted": True},
    ])
    _insert_pre_fix_row(db, match_id=8001, match_provider_id="CD_M1", poll_sequence=1, raw_payload=live_payload)
    db.commit()

    results = reparse_stored_raw_observations(db, dry_run=True)
    assert results[0]["changed"] is True

    unchanged = db.execute(
        "SELECT latest_period_number FROM match_state_evidence_observations WHERE poll_sequence=1"
    ).fetchone()
    assert unchanged["latest_period_number"] is None


def test_reparse_is_idempotent(db):
    live_payload = match_item_payload(periods=[
        {"periodNumber": 1, "periodSeconds": 1789, "periodCompleted": True},
    ])
    _insert_pre_fix_row(db, match_id=8001, match_provider_id="CD_M1", poll_sequence=1, raw_payload=live_payload)
    db.commit()

    first = reparse_stored_raw_observations(db)
    db.commit()
    assert first[0]["changed"] is True

    second = reparse_stored_raw_observations(db)
    assert second[0]["changed"] is False


def test_reparse_filters_by_match_provider_id(db):
    db.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, start_time_utc, season_id, scraped_at) "
        "VALUES(8002,'CD_M2',1,'C','D','V','LIVE',?,73,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    db.commit()
    payload_a = match_item_payload(periods=[{"periodNumber": 1, "periodSeconds": 10, "periodCompleted": False}])
    payload_b = match_item_payload(periods=[{"periodNumber": 2, "periodSeconds": 20, "periodCompleted": False}])
    _insert_pre_fix_row(db, match_id=8001, match_provider_id="CD_M1", poll_sequence=1, raw_payload=payload_a)
    _insert_pre_fix_row(db, match_id=8002, match_provider_id="CD_M2", poll_sequence=1, raw_payload=payload_b)
    db.commit()

    results = reparse_stored_raw_observations(db, match_provider_id="CD_M2")
    assert len(results) == 1
    assert results[0]["match_provider_id"] == "CD_M2"
