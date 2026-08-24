"""Offline tests for production CFS match-interchange persistence (Issue #204).

No live AFL/CFS access is required: parsing and canonical resolution are
pure/DB-read functions, and persistence is exercised against a migrated
temporary SQLite database, following the same pattern as
tests/test_match_interchange_evidence.py (the diagnostic counterpart this
module is deliberately independent from) and
tests/test_match_commentary_production.py (Issue #201's promotion, the
architectural template for this promotion).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from afl_json.match_interchange import (
    EVENT_APPEARED,
    EVENT_BENCH_REASON_CHANGED,
    EVENT_DISAPPEARED,
    EVENT_INTERCHANGE_COUNT_CHANGED,
    MATCH_INTERCHANGE_ENDPOINT,
    OUTCOME_SUCCESS,
    MatchInterchangeError,
    current_state_rows,
    event_rows,
    parse_match_interchange,
    persist_match_interchange,
    persist_poll_outcome,
    recently_active_match_provider_ids,
    resolve_canonical_match_id,
    resolve_canonical_player,
    resolve_canonical_team,
)
from db.migration_runner import migrate_database

NOW = datetime(2026, 8, 24, 3, 0, tzinfo=timezone.utc)
INTERCHANGE_FIXTURES = Path(__file__).parent / "fixtures" / "afl" / "interchange"

MATCH_ID = 9201
MATCH_PROVIDER_ID = "CD_M20260142001"


def interchange_fixture(name: str) -> dict:
    return json.loads((INTERCHANGE_FIXTURES / name).read_text())


def _iso(offset_seconds: int = 0) -> str:
    return datetime.fromtimestamp(NOW.timestamp() + offset_seconds, tz=timezone.utc).isoformat()


def _entry(player_id, *, team_id="CD_T10", count=1, bench_reason="ROTATION", tog=100, tob=10, power=3):
    return {
        "teamId": team_id,
        "player": {"playerId": player_id, "playerName": {"givenName": "Alex", "surname": "Player"},
                   "captain": False, "playerJumperNumber": 1},
        "interchangeCount": count, "benchReason": bench_reason,
        "timeOnGround": tog, "timeOnBench": tob, "powerRating": power,
    }


def interchange_payload(*, home=None, away=None, match_id=MATCH_PROVIDER_ID):
    return {
        "matchId": match_id,
        "homeInterchange": home if home is not None else [],
        "awayInterchange": away if away is not None else [],
        "homeInterchangeCounts": {"totalInterchangeCount": 0.0, "interchangeCap": 75.0,
                                   "interchangeCountQ1": 0.0, "interchangeCountQ2": 0.0,
                                   "interchangeCountQ3": 0.0, "interchangeCountQ4": 0.0},
        "awayInterchangeCounts": {"totalInterchangeCount": 0.0, "interchangeCap": 75.0,
                                   "interchangeCountQ1": 0.0, "interchangeCountQ2": 0.0,
                                   "interchangeCountQ3": 0.0, "interchangeCountQ4": 0.0},
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
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, scraped_at) VALUES(1,'R24',73,1,?)",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, "
        "start_time_utc, season_id, scraped_at) VALUES(?,?,1,'A','B','V','LIVE',?,73,?)",
        (MATCH_ID, MATCH_PROVIDER_ID, NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO canonical_players VALUES(501,'Alex Player','Alex','Player',?,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) "
        "VALUES(501,'champion_data','CD_I1',?,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO afl_teams VALUES(10,'CD_T10',73,'Home Team','HT','Home','Home','Home','MEN','{}','{}','{}',?)",
        (NOW.isoformat(),),
    )
    conn.commit()
    yield conn, path
    conn.close()


# --- Endpoint contract -------------------------------------------------------

def test_endpoint_is_marked_verified_and_independent_of_diagnostic_definition():
    from collection.match_interchange_evidence import MATCH_INTERCHANGE_ENDPOINT as diagnostic_endpoint
    assert MATCH_INTERCHANGE_ENDPOINT.verified is True
    assert MATCH_INTERCHANGE_ENDPOINT is not diagnostic_endpoint
    resolved = MATCH_INTERCHANGE_ENDPOINT.url_template.format(match_provider_id=MATCH_PROVIDER_ID)
    assert resolved.endswith(f"/matchInterchange/{MATCH_PROVIDER_ID}")


# --- Parsing ------------------------------------------------------------------

def test_parse_real_concluded_fixture_extracts_entries():
    payload = interchange_fixture("match_interchange_8216_concluded.json")
    parsed = parse_match_interchange(
        payload, match_id=1, match_provider_id="CD_M20260142001", observed_at=_iso(),
        match_status_at_poll="POSTGAME",
    )
    assert len(parsed.home_entries) == 5
    assert len(parsed.away_entries) == 5
    first = parsed.home_entries[0]
    assert first.player_provider_id == "CD_I1031792"
    assert first.team_provider_id == "CD_T10"
    assert first.interchange_count == 8
    assert first.bench_reason == "ROTATION"
    assert first.time_on_ground == 4697
    assert first.time_on_bench == 568
    assert first.power_rating == 5


def test_parse_rejects_non_object_payload():
    with pytest.raises(MatchInterchangeError):
        parse_match_interchange(None, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    with pytest.raises(MatchInterchangeError):
        parse_match_interchange([], match_id=1, match_provider_id="CD_M1", observed_at=_iso())


def test_parse_rejects_payload_for_a_different_match():
    payload = interchange_payload(home=[_entry("CD_I1")], match_id="CD_M_OTHER")
    with pytest.raises(MatchInterchangeError):
        parse_match_interchange(payload, match_id=1, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso())


def test_parse_treats_missing_arrays_as_none_not_empty():
    parsed = parse_match_interchange({"matchId": "CD_M1"}, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    assert parsed.home_entries is None
    assert parsed.away_entries is None


def test_parse_skips_entries_missing_a_player_id():
    payload = interchange_payload(home=[{"teamId": "CD_T10", "interchangeCount": 1}])
    parsed = parse_match_interchange(payload, match_id=1, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso())
    assert parsed.home_entries == []


def test_parse_never_persists_player_name_or_jumper_number():
    payload = interchange_payload(home=[_entry("CD_I1")])
    parsed = parse_match_interchange(payload, match_id=1, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso())
    entry = parsed.home_entries[0]
    assert not hasattr(entry, "player_name")
    assert not hasattr(entry, "jumper_number")


# --- Canonical identity resolution --------------------------------------------

def test_resolve_canonical_player_and_team(db):
    conn, _ = db
    assert resolve_canonical_player(conn, "CD_I1") == 501
    assert resolve_canonical_player(conn, "CD_I_UNKNOWN") is None
    assert resolve_canonical_player(conn, None) is None
    assert resolve_canonical_team(conn, "CD_T10") == 10
    assert resolve_canonical_team(conn, "CD_T_UNKNOWN") is None


def test_resolve_canonical_match_id(db):
    conn, _ = db
    assert resolve_canonical_match_id(conn, MATCH_PROVIDER_ID) == MATCH_ID
    assert resolve_canonical_match_id(conn, "CD_M_UNKNOWN") is None


# --- Persistence: first observation / appearance ------------------------------

def test_first_successful_observation_persists_state_and_appeared_events(db):
    conn, _ = db
    payload = interchange_payload(home=[_entry("CD_I1", count=1)], away=[_entry("CD_I2", team_id="CD_T40", count=1)])
    parsed = parse_match_interchange(payload, match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID,
                                     observed_at=_iso(), match_status_at_poll="LIVE")
    result = persist_match_interchange(conn, parsed)
    conn.commit()
    assert result["outcome"] == OUTCOME_SUCCESS
    assert result["poll_sequence"] == 1
    assert len(result["appeared"]) == 2
    assert result["disappeared"] == []
    assert result["changed"] == []

    rows = current_state_rows(conn, match_id=MATCH_ID)
    assert len(rows) == 2
    home_row = next(r for r in rows if r["side"] == "home")
    assert home_row["player_provider_id"] == "CD_I1"
    assert home_row["canonical_player_id"] == 501
    assert home_row["on_bench"] is True
    assert home_row["interchange_count"] == 1

    events = event_rows(conn, match_id=MATCH_ID)
    assert len(events) == 2
    assert all(event["event_type"] == EVENT_APPEARED for event in events)


def test_home_side_player_appearing_is_isolated_from_away_side(db):
    conn, _ = db
    parsed = parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1")]), match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID,
        observed_at=_iso(),
    )
    result = persist_match_interchange(conn, parsed)
    conn.commit()
    assert len(result["appeared"]) == 1
    assert result["appeared"][0]["side"] == "home"


def test_away_side_player_appearing(db):
    conn, _ = db
    parsed = parse_match_interchange(
        interchange_payload(away=[_entry("CD_I2", team_id="CD_T40")]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    )
    result = persist_match_interchange(conn, parsed)
    conn.commit()
    assert len(result["appeared"]) == 1
    assert result["appeared"][0]["side"] == "away"


# --- Persistence: disappearance ------------------------------------------------

def test_player_disappearing_from_home_interchange(db):
    conn, _ = db
    first = parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1"), _entry("CD_I3")]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    )
    persist_match_interchange(conn, first)
    conn.commit()

    second = parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1")]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(15),
    )
    result = persist_match_interchange(conn, second)
    conn.commit()
    assert len(result["disappeared"]) == 1
    assert result["disappeared"][0]["player_provider_id"] == "CD_I3"

    rows = {row["player_provider_id"]: row for row in current_state_rows(conn, match_id=MATCH_ID)}
    assert rows["CD_I3"]["on_bench"] is False
    # Last known values are preserved, not zeroed, on disappearance.
    assert rows["CD_I3"]["interchange_count"] == 1
    assert rows["CD_I1"]["on_bench"] is True


def test_disappearance_not_inferred_when_side_array_missing_this_poll(db):
    """A transient upstream hiccup (homeInterchange missing/malformed) must
    never be read as every home-side player having left the list."""
    conn, _ = db
    first = parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1")]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    )
    persist_match_interchange(conn, first)
    conn.commit()

    second = parse_match_interchange(
        {"matchId": MATCH_PROVIDER_ID, "awayInterchange": []}, match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(15),
    )
    result = persist_match_interchange(conn, second)
    conn.commit()
    assert result["disappeared"] == []
    row = current_state_rows(conn, match_id=MATCH_ID)[0]
    assert row["on_bench"] is True


def test_disappearance_not_inferred_when_side_array_has_a_malformed_entry(db):
    """A side array that is present but contains one unidentifiable entry
    (e.g. a transiently corrupted player block) must not cause every
    *other* known player on that side to be read as having disappeared --
    the "missing" player could simply be the one behind the malformed
    entry. Regression test for a Codex review finding on PR #206."""
    conn, _ = db
    first = parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1"), _entry("CD_I3")]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    )
    persist_match_interchange(conn, first)
    conn.commit()

    # CD_I1's entry is intact; CD_I3's entry is now malformed (no playerId),
    # so the side is incomplete this poll -- CD_I3 must not be marked
    # disappeared, even though CD_I3 is absent from the successfully parsed
    # entries.
    second = parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1", count=2), {"teamId": "CD_T10", "interchangeCount": 1}]),
        match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(15),
    )
    result = persist_match_interchange(conn, second)
    conn.commit()
    assert result["disappeared"] == []
    # CD_I1's own update is still processed normally.
    assert len(result["changed"]) == 1
    assert result["changed"][0]["player_provider_id"] == "CD_I1"

    rows = {row["player_provider_id"]: row for row in current_state_rows(conn, match_id=MATCH_ID)}
    assert rows["CD_I3"]["on_bench"] is True
    assert rows["CD_I1"]["on_bench"] is True


def test_canonical_identity_re_resolved_when_recording_disappearance(db):
    """Current-state self-healing (module docstring §"Canonical identity
    linking") must also cover the disappearance write path, not only
    appear/update -- otherwise a player whose crosswalk is added only after
    they leave the interchange list would stay unresolved forever. Regression
    test for a Codex review finding on PR #206."""
    conn, _ = db
    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I_LATE")]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    ))
    conn.commit()
    assert current_state_rows(conn, match_id=MATCH_ID)[0]["canonical_player_id"] is None

    # Crosswalk added only after the player has left the list.
    conn.execute(
        "INSERT INTO canonical_players VALUES(888,'Late Crosswalk','Late','Crosswalk',?,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) "
        "VALUES(888,'champion_data','CD_I_LATE',?,?)", (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()

    result = persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[]), match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID,
        observed_at=_iso(15),
    ))
    conn.commit()
    assert len(result["disappeared"]) == 1

    row = current_state_rows(conn, match_id=MATCH_ID)[0]
    assert row["on_bench"] is False
    assert row["canonical_player_id"] == 888

    event = event_rows(conn, match_id=MATCH_ID, event_type=EVENT_DISAPPEARED)[0]
    assert event["canonical_player_id"] == 888


def test_player_reappearing_after_disappearance_is_appeared_again(db):
    conn, _ = db
    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1")]), match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID,
        observed_at=_iso(),
    ))
    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[]), match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID,
        observed_at=_iso(15),
    ))
    result = persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1")]), match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID,
        observed_at=_iso(30),
    ))
    conn.commit()
    assert len(result["appeared"]) == 1
    rows = current_state_rows(conn, match_id=MATCH_ID)
    assert rows[0]["on_bench"] is True


# --- Persistence: field changes -------------------------------------------------

def test_interchange_count_increment_emits_event(db):
    conn, _ = db
    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1", count=3)]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    ))
    result = persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1", count=4)]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(15),
    ))
    conn.commit()
    assert len(result["changed"]) == 1
    assert result["changed"][0]["event_type"] == EVENT_INTERCHANGE_COUNT_CHANGED
    events = event_rows(conn, match_id=MATCH_ID, event_type=EVENT_INTERCHANGE_COUNT_CHANGED)
    assert events[0]["previous_interchange_count"] == 3
    assert events[0]["interchange_count"] == 4


def test_bench_reason_populated_on_first_appearance(db):
    conn, _ = db
    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1", bench_reason="ROTATION")]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    ))
    conn.commit()
    row = current_state_rows(conn, match_id=MATCH_ID)[0]
    assert row["bench_reason"] == "ROTATION"


def test_bench_reason_changing_emits_event(db):
    conn, _ = db
    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1", bench_reason="ROTATION")]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    ))
    result = persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1", bench_reason="INJURY")]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(15),
    ))
    conn.commit()
    assert len(result["changed"]) == 1
    assert result["changed"][0]["event_type"] == EVENT_BENCH_REASON_CHANGED
    events = event_rows(conn, match_id=MATCH_ID, event_type=EVENT_BENCH_REASON_CHANGED)
    assert events[0]["previous_bench_reason"] == "ROTATION"
    assert events[0]["bench_reason"] == "INJURY"
    # bench_reason is persisted exactly as supplied -- no inference performed.


def test_time_on_ground_and_bench_updates_never_generate_event_noise(db):
    conn, _ = db
    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1", tog=100, tob=10)]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    ))
    result = persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1", tog=115, tob=25)]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(15),
    ))
    conn.commit()
    assert result["appeared"] == [] and result["disappeared"] == [] and result["changed"] == []
    assert event_rows(conn, match_id=MATCH_ID) == [
        event for event in event_rows(conn, match_id=MATCH_ID) if event["event_type"] == EVENT_APPEARED
    ]  # only the original appearance event exists; no timer-driven rows added
    row = current_state_rows(conn, match_id=MATCH_ID)[0]
    # Current-state values are still refreshed silently.
    assert row["time_on_ground"] == 115
    assert row["time_on_bench"] == 25


def test_multiple_player_changes_between_polls(db):
    conn, _ = db
    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1"), _entry("CD_I3")], away=[_entry("CD_I2", team_id="CD_T40")]),
        match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    ))
    result = persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(
            home=[_entry("CD_I1", count=2), _entry("CD_I4")],
            away=[_entry("CD_I2", team_id="CD_T40", bench_reason="INJURY")],
        ),
        match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(15),
    ))
    conn.commit()
    assert len(result["appeared"]) == 1  # CD_I4
    assert len(result["disappeared"]) == 1  # CD_I3
    assert len(result["changed"]) == 2  # CD_I1 count, CD_I2 bench_reason


# --- Idempotency / restart safety ----------------------------------------------

def test_repeated_identical_poll_is_idempotent(db):
    conn, _ = db
    payload = interchange_payload(home=[_entry("CD_I1")])
    for offset in (0, 15, 30):
        result = persist_match_interchange(conn, parse_match_interchange(
            payload, match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(offset),
        ))
        conn.commit()
    assert result["appeared"] == [] and result["disappeared"] == [] and result["changed"] == []
    assert len(current_state_rows(conn, match_id=MATCH_ID)) == 1
    assert len(event_rows(conn, match_id=MATCH_ID)) == 1
    polls = conn.execute(
        "SELECT COUNT(*) FROM match_interchange_polls WHERE match_provider_id=?", (MATCH_PROVIDER_ID,)
    ).fetchone()[0]
    assert polls == 3


def test_replay_across_independent_persist_calls_is_idempotent(db):
    """Simulates a scheduler/container restart: entirely independent persist
    calls (fresh parse each time), diffing only against durable state."""
    conn, _ = db
    payload = interchange_payload(home=[_entry("CD_I1", count=5)])
    persist_match_interchange(conn, parse_match_interchange(
        payload, match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(0),
    ))
    conn.commit()
    # Simulate restart: a fresh call with the exact same payload/counter values.
    result = persist_match_interchange(conn, parse_match_interchange(
        payload, match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(60),
    ))
    conn.commit()
    assert result["appeared"] == [] and result["changed"] == []
    assert len(current_state_rows(conn, match_id=MATCH_ID)) == 1


# --- Unresolved canonical mapping ----------------------------------------------

def test_unresolved_canonical_player_and_team_stay_null(db):
    conn, _ = db
    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I_UNKNOWN", team_id="CD_T_UNKNOWN")]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    ))
    conn.commit()
    row = current_state_rows(conn, match_id=MATCH_ID)[0]
    assert row["player_provider_id"] == "CD_I_UNKNOWN"
    assert row["canonical_player_id"] is None
    assert row["team_provider_id"] == "CD_T_UNKNOWN"
    assert row["canonical_team_id"] is None


def test_current_state_self_heals_canonical_link_once_crosswalk_exists(db):
    """Unlike commentary's immutable event log, current-state re-resolves
    canonical identity on every update -- see module docstring."""
    conn, _ = db
    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I_NEW")]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    ))
    conn.commit()
    assert current_state_rows(conn, match_id=MATCH_ID)[0]["canonical_player_id"] is None

    conn.execute(
        "INSERT INTO canonical_players VALUES(777,'New Player','New','Player',?,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) "
        "VALUES(777,'champion_data','CD_I_NEW',?,?)", (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()

    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I_NEW", count=2)]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(15),
    ))
    conn.commit()
    assert current_state_rows(conn, match_id=MATCH_ID)[0]["canonical_player_id"] == 777


# --- Malformed/partial response isolation --------------------------------------

def test_malformed_single_entry_is_skipped_not_fatal(db):
    conn, _ = db
    payload = interchange_payload(home=[_entry("CD_I1"), "not-an-object", {"teamId": "CD_T10"}])
    result = persist_match_interchange(conn, parse_match_interchange(
        payload, match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    ))
    conn.commit()
    assert len(result["appeared"]) == 1


def test_persist_poll_outcome_records_non_success_attempts(db):
    conn, _ = db
    result = persist_poll_outcome(
        conn, match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
        match_status_at_poll="SCHEDULED", outcome="not_published",
    )
    conn.commit()
    assert result["outcome"] == "not_published"
    row = conn.execute(
        "SELECT outcome FROM match_interchange_polls WHERE match_provider_id=?", (MATCH_PROVIDER_ID,)
    ).fetchone()
    assert row["outcome"] == "not_published"


# --- Candidate window -----------------------------------------------------------

def test_recently_active_match_provider_ids_within_grace_window(db):
    conn, _ = db
    persist_poll_outcome(
        conn, match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(0),
        match_status_at_poll="POSTGAME", outcome="not_published",
    )
    conn.commit()
    result = recently_active_match_provider_ids(conn, now=NOW, grace_seconds=600)
    assert (MATCH_ID, MATCH_PROVIDER_ID) in result


def test_recently_active_match_provider_ids_excludes_outside_grace_window(db):
    conn, _ = db
    persist_poll_outcome(
        conn, match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(-3600),
        match_status_at_poll="POSTGAME", outcome="not_published",
    )
    conn.commit()
    assert recently_active_match_provider_ids(conn, now=NOW, grace_seconds=600) == []


def test_recently_active_match_provider_ids_ignores_concluded_only_polls(db):
    conn, _ = db
    persist_poll_outcome(
        conn, match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(0),
        match_status_at_poll="CONCLUDED", outcome=OUTCOME_SUCCESS,
    )
    conn.commit()
    assert recently_active_match_provider_ids(conn, now=NOW, grace_seconds=600) == []


# --- current_state_rows / event_rows filtering -----------------------------------

def test_current_state_rows_filters(db):
    conn, _ = db
    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[_entry("CD_I1")], away=[_entry("CD_I2", team_id="CD_T40")]),
        match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(),
    ))
    persist_match_interchange(conn, parse_match_interchange(
        interchange_payload(home=[], away=[_entry("CD_I2", team_id="CD_T40")]), match_id=MATCH_ID,
        match_provider_id=MATCH_PROVIDER_ID, observed_at=_iso(15),
    ))
    conn.commit()
    assert len(current_state_rows(conn, match_id=MATCH_ID)) == 2
    assert len(current_state_rows(conn, match_id=MATCH_ID, side="away")) == 1
    assert len(current_state_rows(conn, match_id=MATCH_ID, on_bench_only=True)) == 1
    assert len(current_state_rows(conn, match_id=MATCH_ID, canonical_player_id=501)) == 1
