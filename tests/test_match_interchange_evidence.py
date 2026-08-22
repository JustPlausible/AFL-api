"""Offline tests for diagnostic matchInterchange evidence capture (Issue #193).

No live AFL/CFS access is required: parsing and transition detection are
pure functions, and persistence is exercised against a migrated temporary
SQLite database. Reuses the concluded matchInterchange fixture captured from
an earlier endpoint investigation, plus small synthetic live snapshots to
demonstrate transitions.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collection.match_interchange_evidence import (
    MATCH_INTERCHANGE_ENDPOINT,
    NOISY_TRANSITIONS,
    TRANSITION_AWAY_QUARTER_INTERCHANGE_COUNT_CHANGED,
    TRANSITION_AWAY_TOTAL_INTERCHANGE_COUNT_CHANGED,
    TRANSITION_FIRST_OBSERVATION,
    TRANSITION_HOME_QUARTER_INTERCHANGE_COUNT_CHANGED,
    TRANSITION_HOME_TOTAL_INTERCHANGE_COUNT_CHANGED,
    TRANSITION_PLAYER_APPEARED_AWAY,
    TRANSITION_PLAYER_APPEARED_HOME,
    TRANSITION_PLAYER_BENCH_REASON_CHANGED,
    TRANSITION_PLAYER_DISAPPEARED_HOME,
    TRANSITION_PLAYER_INTERCHANGE_COUNT_CHANGED,
    TRANSITION_PLAYER_TIME_ON_BENCH_CHANGED,
    TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED,
    MatchInterchangeEvidenceError,
    detect_transitions,
    evidence_rows,
    load_previous_observation,
    parse_match_interchange,
    persist_observation,
    recently_live_match_provider_ids,
)
from db.migration_runner import migrate_database

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)
INTERCHANGE_FIXTURES = Path(__file__).parent / "fixtures" / "afl" / "interchange"


def interchange_fixture(name: str) -> dict:
    return json.loads((INTERCHANGE_FIXTURES / name).read_text())


def _iso(offset_seconds: int = 0) -> str:
    return datetime.fromtimestamp(NOW.timestamp() + offset_seconds, tz=timezone.utc).isoformat()


def _player(player_id, *, given="Alex", surname="Player", jumper=1, count=1,
            bench_reason="ROTATION", tog=100, tob=10, power=3):
    return {
        "teamId": "CD_T10",
        "player": {
            "playerId": player_id,
            "playerName": {"givenName": given, "surname": surname},
            "captain": False,
            "playerJumperNumber": jumper,
        },
        "interchangeCount": count,
        "benchReason": bench_reason,
        "timeOnGround": tog,
        "timeOnBench": tob,
        "powerRating": power,
    }


def interchange_payload(*, home=None, away=None, home_counts=None, away_counts=None, match_id="CD_M20260142402"):
    return {
        "matchId": match_id,
        "homeInterchange": home if home is not None else [],
        "awayInterchange": away if away is not None else [],
        "homeInterchangeCounts": home_counts if home_counts is not None else {
            "totalInterchangeCount": 0.0, "interchangeCap": 75.0,
            "interchangeCountQ1": 0.0, "interchangeCountQ2": 0.0,
            "interchangeCountQ3": 0.0, "interchangeCountQ4": 0.0,
        },
        "awayInterchangeCounts": away_counts if away_counts is not None else {
            "totalInterchangeCount": 0.0, "interchangeCap": 75.0,
            "interchangeCountQ1": 0.0, "interchangeCountQ2": 0.0,
            "interchangeCountQ3": 0.0, "interchangeCountQ4": 0.0,
        },
    }


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "afl.db"
    monkeypatch.setenv("DB_PATH", str(path))
    import config
    monkeypatch.setattr(config, "DB_PATH", str(path), raising=False)
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, scraped_at) VALUES(1,'R1',73,1,?)",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, "
        "start_time_utc, season_id, scraped_at) VALUES(9001,'CD_M20260142402',1,'A','B','V','LIVE',?,73,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    yield conn, path
    conn.close()


# --- Endpoint isolation -----------------------------------------------------

def test_match_interchange_endpoint_is_unverified_and_not_in_shared_registry():
    from afl_json.contracts import ENDPOINTS
    assert MATCH_INTERCHANGE_ENDPOINT.verified is False
    assert MATCH_INTERCHANGE_ENDPOINT.name not in ENDPOINTS
    assert MATCH_INTERCHANGE_ENDPOINT.required_path_parameters == ("match_provider_id",)
    assert MATCH_INTERCHANGE_ENDPOINT.requires_auth is True


# --- Parsing ------------------------------------------------------------

def test_parse_match_interchange_extracts_players_and_counts_from_concluded_fixture():
    payload = interchange_fixture("match_interchange_8216_concluded.json")
    observation = parse_match_interchange(
        payload, match_id=1, match_provider_id="CD_M20260142001", observed_at=_iso(),
        match_status_at_poll="POSTGAME",
    )
    assert observation.match_status_at_poll == "POSTGAME"
    assert len(observation.home_interchange) == 5
    assert len(observation.away_interchange) == 5
    assert observation.home_counts["totalInterchangeCount"] == 75.0
    assert observation.away_counts["totalInterchangeCount"] == 73.0
    assert observation.raw == payload


def test_parse_match_interchange_handles_missing_arrays_and_counts():
    observation = parse_match_interchange(
        {"matchId": "CD_M1"}, match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    assert observation.home_interchange == []
    assert observation.away_interchange == []
    assert observation.home_counts == {}
    assert observation.away_counts == {}


def test_parse_match_interchange_rejects_non_object_payload():
    with pytest.raises(MatchInterchangeEvidenceError):
        parse_match_interchange(None, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    with pytest.raises(MatchInterchangeEvidenceError):
        parse_match_interchange([], match_id=1, match_provider_id="CD_M1", observed_at=_iso())


# --- Transition detection: first observation --------------------------------

def test_detect_transitions_first_observation():
    current = parse_match_interchange(interchange_payload(), match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    assert detect_transitions(None, current) == [TRANSITION_FIRST_OBSERVATION]


# --- Transition detection: player appearance/disappearance (by playerId) ----

def test_detect_transitions_player_appears_and_disappears_by_player_id_not_name():
    previous = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1")]), match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    # Same playerId, different (re-fetched) name spelling must not register as appear/disappear.
    current = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", given="Alexander"), _player("CD_I2")]),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(15),
    )
    flags = detect_transitions(previous, current)
    assert TRANSITION_PLAYER_APPEARED_HOME in flags
    assert TRANSITION_PLAYER_DISAPPEARED_HOME not in flags


def test_detect_transitions_player_disappears_from_home_interchange():
    previous = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1"), _player("CD_I2")]),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    current = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1")]), match_id=1, match_provider_id="CD_M1", observed_at=_iso(15),
    )
    flags = detect_transitions(previous, current)
    assert flags == [TRANSITION_PLAYER_DISAPPEARED_HOME]


def test_detect_transitions_player_appears_in_away_interchange():
    previous = parse_match_interchange(interchange_payload(away=[]), match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    current = parse_match_interchange(
        interchange_payload(away=[_player("CD_I9")]), match_id=1, match_provider_id="CD_M1", observed_at=_iso(15),
    )
    assert detect_transitions(previous, current) == [TRANSITION_PLAYER_APPEARED_AWAY]


# --- Transition detection: per-player field changes --------------------------

def test_detect_transitions_interchange_count_changed():
    previous = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", count=3, tog=100, tob=10)]),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    current = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", count=4, tog=100, tob=10)]),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(15),
    )
    assert detect_transitions(previous, current) == [TRANSITION_PLAYER_INTERCHANGE_COUNT_CHANGED]


def test_detect_transitions_bench_reason_changed():
    previous = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", bench_reason="ROTATION", tog=100, tob=10)]),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    current = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", bench_reason="INJURY", tog=100, tob=10)]),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(15),
    )
    assert detect_transitions(previous, current) == [TRANSITION_PLAYER_BENCH_REASON_CHANGED]


def test_detect_transitions_time_on_ground_and_bench_are_noisy_but_still_reported():
    previous = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", tog=100, tob=10)]),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    current = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", tog=115, tob=10)]),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(15),
    )
    flags = detect_transitions(previous, current)
    assert flags == [TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED]
    assert set(flags) <= NOISY_TRANSITIONS


def test_detect_transitions_time_on_bench_changed():
    previous = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", tog=100, tob=10)]),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    current = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", tog=100, tob=25)]),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(15),
    )
    assert detect_transitions(previous, current) == [TRANSITION_PLAYER_TIME_ON_BENCH_CHANGED]


# --- Transition detection: team-level totals ---------------------------------

def test_detect_transitions_home_total_interchange_count_changed():
    previous = parse_match_interchange(
        interchange_payload(home_counts={"totalInterchangeCount": 10.0, "interchangeCap": 75.0,
                                          "interchangeCountQ1": 10.0, "interchangeCountQ2": 0.0,
                                          "interchangeCountQ3": 0.0, "interchangeCountQ4": 0.0}),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    current = parse_match_interchange(
        interchange_payload(home_counts={"totalInterchangeCount": 11.0, "interchangeCap": 75.0,
                                          "interchangeCountQ1": 11.0, "interchangeCountQ2": 0.0,
                                          "interchangeCountQ3": 0.0, "interchangeCountQ4": 0.0}),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(15),
    )
    flags = detect_transitions(previous, current)
    assert TRANSITION_HOME_TOTAL_INTERCHANGE_COUNT_CHANGED in flags
    assert TRANSITION_HOME_QUARTER_INTERCHANGE_COUNT_CHANGED in flags


def test_detect_transitions_away_total_interchange_count_changed():
    previous = parse_match_interchange(
        interchange_payload(away_counts={"totalInterchangeCount": 5.0, "interchangeCap": 75.0,
                                          "interchangeCountQ1": 5.0, "interchangeCountQ2": 0.0,
                                          "interchangeCountQ3": 0.0, "interchangeCountQ4": 0.0}),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    current = parse_match_interchange(
        interchange_payload(away_counts={"totalInterchangeCount": 6.0, "interchangeCap": 75.0,
                                          "interchangeCountQ1": 6.0, "interchangeCountQ2": 0.0,
                                          "interchangeCountQ3": 0.0, "interchangeCountQ4": 0.0}),
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(15),
    )
    assert detect_transitions(previous, current) == [
        TRANSITION_AWAY_TOTAL_INTERCHANGE_COUNT_CHANGED,
        TRANSITION_AWAY_QUARTER_INTERCHANGE_COUNT_CHANGED,
    ]


def test_detect_transitions_no_changes_yields_no_flags():
    payload = interchange_payload(home=[_player("CD_I1")], away=[_player("CD_I9")])
    previous = parse_match_interchange(payload, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    current = parse_match_interchange(payload, match_id=1, match_provider_id="CD_M1", observed_at=_iso(15))
    assert detect_transitions(previous, current) == []


# --- Persistence: raw retention policy (meaningful vs noisy) -----------------

def test_persist_observation_first_observation_retains_raw_and_is_meaningful(db):
    conn, _ = db
    current = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1")]), match_id=9001, match_provider_id="CD_M20260142402",
        observed_at=_iso(), match_status_at_poll="LIVE",
    )
    outcome = persist_observation(conn, current, [TRANSITION_FIRST_OBSERVATION])
    conn.commit()
    assert outcome["is_transition"] is True
    row = conn.execute(
        "SELECT raw_match_interchange_json FROM match_interchange_evidence_observations WHERE poll_sequence=1"
    ).fetchone()
    assert row["raw_match_interchange_json"] is not None


def test_persist_observation_noisy_only_transition_does_not_retain_raw_or_set_is_transition(db):
    conn, _ = db
    current = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", tog=200)]), match_id=9001, match_provider_id="CD_M20260142402",
        observed_at=_iso(), match_status_at_poll="LIVE",
    )
    outcome = persist_observation(conn, current, [TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED])
    conn.commit()
    assert outcome["is_transition"] is False
    assert outcome["meaningful_transitions"] == []
    row = conn.execute(
        "SELECT raw_match_interchange_json, transition_flags_json, is_transition "
        "FROM match_interchange_evidence_observations WHERE poll_sequence=1"
    ).fetchone()
    assert row["raw_match_interchange_json"] is None
    assert row["is_transition"] == 0
    # Still persisted in the flags column, just not treated as meaningful.
    assert json.loads(row["transition_flags_json"]) == [TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED]


def test_persist_observation_meaningful_transition_retains_raw(db):
    conn, _ = db
    current = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", count=5)]), match_id=9001, match_provider_id="CD_M20260142402",
        observed_at=_iso(), match_status_at_poll="LIVE",
    )
    outcome = persist_observation(conn, current, [TRANSITION_PLAYER_INTERCHANGE_COUNT_CHANGED])
    conn.commit()
    assert outcome["is_transition"] is True
    row = conn.execute(
        "SELECT raw_match_interchange_json FROM match_interchange_evidence_observations WHERE poll_sequence=1"
    ).fetchone()
    assert row["raw_match_interchange_json"] is not None


# --- Persistence: poll-sequence continuation / restart safety ----------------

def test_poll_sequence_continues_across_independent_persist_calls(db):
    conn, _ = db
    first = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", count=1)]), match_id=9001, match_provider_id="CD_M20260142402",
        observed_at=_iso(), match_status_at_poll="LIVE",
    )
    outcome1 = persist_observation(conn, first, [TRANSITION_FIRST_OBSERVATION])
    conn.commit()
    assert outcome1["poll_sequence"] == 1

    loaded_previous = load_previous_observation(conn, "CD_M20260142402")
    assert loaded_previous is not None
    assert loaded_previous.home_interchange[0]["interchangeCount"] == 1

    second = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", count=2)]), match_id=9001, match_provider_id="CD_M20260142402",
        observed_at=_iso(15), match_status_at_poll="LIVE",
    )
    transitions = detect_transitions(loaded_previous, second)
    outcome2 = persist_observation(conn, second, transitions)
    conn.commit()
    assert outcome2["poll_sequence"] == 2
    assert outcome2["meaningful_transitions"] == [TRANSITION_PLAYER_INTERCHANGE_COUNT_CHANGED]

    count = conn.execute(
        "SELECT COUNT(*) FROM match_interchange_evidence_observations WHERE match_provider_id='CD_M20260142402'"
    ).fetchone()[0]
    assert count == 2


# --- recently_live_match_provider_ids (self-contained post-live grace) -------

def test_recently_live_match_provider_ids_uses_local_status_snapshot_not_match_clock_table(db):
    conn, _ = db
    obs = parse_match_interchange(
        interchange_payload(), match_id=9001, match_provider_id="CD_M20260142402",
        observed_at=_iso(), match_status_at_poll="LIVE",
    )
    persist_observation(conn, obs, [TRANSITION_FIRST_OBSERVATION])
    conn.commit()

    result = recently_live_match_provider_ids(conn, now=NOW, grace_seconds=600)
    assert result == [(9001, "CD_M20260142402")]

    # A later poll where the local status has already moved away from LIVE
    # does not further extend the window based on this row.
    later_obs = parse_match_interchange(
        interchange_payload(), match_id=9001, match_provider_id="CD_M20260142402",
        observed_at=_iso(15), match_status_at_poll="POSTGAME",
    )
    persist_observation(conn, later_obs, [])
    conn.commit()

    from datetime import timedelta
    expired = recently_live_match_provider_ids(conn, now=NOW + timedelta(seconds=700), grace_seconds=600)
    assert expired == []


def test_recently_live_match_provider_ids_disabled_by_zero_grace(db):
    conn, _ = db
    assert recently_live_match_provider_ids(conn, now=NOW, grace_seconds=0) == []


# --- evidence_rows filtering --------------------------------------------------

def test_evidence_rows_transitions_only_excludes_noisy_only_rows(db):
    conn, _ = db
    first = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", count=1, tog=100)]), match_id=9001,
        match_provider_id="CD_M20260142402", observed_at=_iso(), match_status_at_poll="LIVE",
    )
    persist_observation(conn, first, [TRANSITION_FIRST_OBSERVATION])
    noisy = parse_match_interchange(
        interchange_payload(home=[_player("CD_I1", count=1, tog=115)]), match_id=9001,
        match_provider_id="CD_M20260142402", observed_at=_iso(15), match_status_at_poll="LIVE",
    )
    persist_observation(conn, noisy, [TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED])
    conn.commit()

    all_rows = evidence_rows(conn, match_provider_id="CD_M20260142402")
    assert len(all_rows) == 2
    meaningful_only = evidence_rows(conn, match_provider_id="CD_M20260142402", transitions_only=True)
    assert len(meaningful_only) == 1
    assert meaningful_only[0]["poll_sequence"] == 1
