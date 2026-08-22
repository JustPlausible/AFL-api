"""Offline tests for diagnostic commentaryFeed evidence capture (Issue #196).

No live AFL/CFS access is required: parsing, fingerprinting, categorisation
and edit-detection are pure functions, and persistence is exercised against a
migrated temporary SQLite database. Reuses a reduced fixture based on the
observed CD_M20260142001 commentaryFeed payload (issue #196's referenced
investigation match), plus small synthetic snapshots for transition/
deduplication tests -- following the same pattern as
tests/test_match_interchange_evidence.py.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collection.match_commentary_evidence import (
    CATEGORY_QUARTER_END,
    CATEGORY_QUARTER_START,
    CATEGORY_SCORE_EVENT,
    MATCH_COMMENTARY_ENDPOINT,
    TRANSITION_COMMENTARY_MISSING_OR_MALFORMED,
    TRANSITION_FIRST_POLL,
    TRANSITION_NEW_EVENTS,
    TRANSITION_OUTCOME_SUCCESS,
    TRANSITION_POSSIBLE_EVENT_EDIT,
    MatchCommentaryEvidenceError,
    categorise_event,
    event_rows,
    parse_match_commentary,
    persist_observation,
    persist_poll_outcome,
    poll_rows,
    recently_live_match_provider_ids,
)
from db.migration_runner import migrate_database

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)
COMMENTARY_FIXTURES = Path(__file__).parent / "fixtures" / "afl" / "commentary"


def commentary_fixture(name: str) -> dict:
    return json.loads((COMMENTARY_FIXTURES / name).read_text())


def _iso(offset_seconds: int = 0) -> str:
    return datetime.fromtimestamp(NOW.timestamp() + offset_seconds, tz=timezone.utc).isoformat()


def _event(*, comment="Some narrative comment.", period_number=1, period_seconds=0,
           player_id=None, team_id=None, score_event=False):
    return {
        "comment": comment, "periodNumber": period_number, "periodSeconds": period_seconds,
        "playerId": player_id, "teamId": team_id, "scoreEvent": score_event,
    }


def commentary_payload(*, events=None, match_id="CD_M20260142402", last_updated="2026-08-22T03:00:00.000+0000"):
    return {
        "matchId": match_id,
        "lastUpdated": last_updated,
        "commentaryEvent": events if events is not None else [],
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

def test_match_commentary_endpoint_is_unverified_and_not_in_shared_registry():
    from afl_json.contracts import ENDPOINTS
    assert MATCH_COMMENTARY_ENDPOINT.verified is False
    assert MATCH_COMMENTARY_ENDPOINT.name not in ENDPOINTS
    assert MATCH_COMMENTARY_ENDPOINT.required_path_parameters == ("match_provider_id",)
    assert MATCH_COMMENTARY_ENDPOINT.requires_auth is True


# --- Parsing ------------------------------------------------------------

def test_parse_match_commentary_extracts_events_from_reduced_fixture():
    payload = commentary_fixture("commentary_feed_reduced.json")
    observation = parse_match_commentary(
        payload, match_id=1, match_provider_id="CD_M20260142001", observed_at=_iso(),
        match_status_at_poll="CONCLUDED",
    )
    assert observation.match_status_at_poll == "CONCLUDED"
    assert observation.feed_last_updated == "2026-07-23T12:25:15.324+0000"
    assert len(observation.events) == 12
    assert observation.raw == payload
    comments = {event.comment for event in observation.events}
    assert "The siren has sounded to end Q4." in comments
    assert "Q1 is now underway." in comments


def test_parse_match_commentary_preserves_missing_commentary_field_as_none():
    observation = parse_match_commentary(
        {"matchId": "CD_M1"}, match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    assert observation.events is None


def test_parse_match_commentary_preserves_malformed_commentary_field_as_none():
    observation = parse_match_commentary(
        {"matchId": "CD_M1", "commentaryEvent": "not-a-list"}, match_id=1, match_provider_id="CD_M1",
        observed_at=_iso(),
    )
    assert observation.events is None


def test_parse_match_commentary_skips_non_object_event_entries():
    observation = parse_match_commentary(
        {"matchId": "CD_M1", "commentaryEvent": [_event(comment="Real event"), "not-an-object", None, 42]},
        match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    assert len(observation.events) == 1
    assert observation.events[0].comment == "Real event"


def test_parse_match_commentary_rejects_non_object_payload():
    with pytest.raises(MatchCommentaryEvidenceError):
        parse_match_commentary(None, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    with pytest.raises(MatchCommentaryEvidenceError):
        parse_match_commentary([], match_id=1, match_provider_id="CD_M1", observed_at=_iso())


def test_parse_match_commentary_empty_event_array_is_not_missing():
    """An empty commentaryEvent=[] (e.g. very early pre-match) is a genuinely
    observed empty feed, distinct from a missing/malformed field."""
    observation = parse_match_commentary(
        commentary_payload(events=[]), match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    assert observation.events == []


# --- Conservative categorisation --------------------------------------------

def test_categorise_event_score_event_is_purely_structural():
    assert categorise_event(comment="anything at all", period_seconds=500, score_event=True) == CATEGORY_SCORE_EVENT
    assert categorise_event(comment=None, period_seconds=None, score_event=True) == CATEGORY_SCORE_EVENT


def test_categorise_event_quarter_start_requires_wording_and_zero_seconds():
    assert categorise_event(comment="Q3 is now underway.", period_seconds=0, score_event=False) == CATEGORY_QUARTER_START
    # Same wording but not at periodSeconds=0 is left uncategorised -- the
    # structural signal is required in addition to wording.
    assert categorise_event(comment="Q3 is now underway.", period_seconds=5, score_event=False) is None


def test_categorise_event_quarter_end_matches_siren_wording():
    assert categorise_event(
        comment="The siren has sounded to end Q2.", period_seconds=1978, score_event=False,
    ) == CATEGORY_QUARTER_END


def test_categorise_event_uncategorised_for_ordinary_narrative_text():
    assert categorise_event(
        comment="Nick Daicos has been exceptional in the contest tonight.", period_seconds=1769, score_event=False,
    ) is None


def test_categorise_event_reworded_quarter_text_stays_uncategorised_not_misparsed():
    """Correctness never depends on exact wording (issue #196): a rewording
    of the siren text is simply left uncategorised, not misclassified."""
    assert categorise_event(
        comment="Siren! End of the second term.", period_seconds=1978, score_event=False,
    ) is None


# --- Fingerprint / deduplication key -----------------------------------------

def test_same_content_produces_same_fingerprint_across_independent_parses():
    payload = commentary_payload(events=[_event(comment="GOAL - Crows (Toby Murray)", period_number=4,
                                                 period_seconds=1943, player_id="CD_I1", team_id="CD_T10",
                                                 score_event=True)])
    first = parse_match_commentary(payload, match_id=1, match_provider_id="CD_M1", observed_at=_iso())
    second = parse_match_commentary(payload, match_id=1, match_provider_id="CD_M1", observed_at=_iso(15))
    assert first.events[0].fingerprint == second.events[0].fingerprint


def test_same_period_second_different_comment_is_a_different_fingerprint():
    """Multiple narrative comments legitimately share a periodSeconds value in
    the real feed (see the reduced fixture); the fingerprint must not collapse
    them into one event."""
    a = _event(comment="First narrative aside.", period_number=4, period_seconds=1950)
    b = _event(comment="Second narrative aside.", period_number=4, period_seconds=1950)
    observation = parse_match_commentary(
        commentary_payload(events=[a, b]), match_id=1, match_provider_id="CD_M1", observed_at=_iso(),
    )
    assert observation.events[0].fingerprint != observation.events[1].fingerprint


# --- Persistence: first observation -----------------------------------------

def test_persist_observation_first_poll_inserts_all_events_and_retains_raw(db):
    conn, _ = db
    payload = commentary_fixture("commentary_feed_reduced.json")
    observation = parse_match_commentary(
        payload, match_id=9001, match_provider_id="CD_M20260142402", observed_at=_iso(),
        match_status_at_poll="LIVE",
    )
    outcome = persist_observation(conn, observation)
    conn.commit()
    assert outcome["outcome"] == "success"
    assert outcome["poll_sequence"] == 1
    assert outcome["new_event_count"] == 12
    assert outcome["is_transition"] is True
    assert TRANSITION_FIRST_POLL in outcome["transitions"]
    assert TRANSITION_NEW_EVENTS in outcome["transitions"]

    event_count = conn.execute("SELECT COUNT(*) FROM commentary_evidence_events").fetchone()[0]
    assert event_count == 12
    poll_row = conn.execute("SELECT raw_commentary_json FROM commentary_evidence_polls WHERE poll_sequence=1").fetchone()
    assert poll_row["raw_commentary_json"] is not None
    # Each individual new event's own raw object is retained too.
    raw_events = conn.execute("SELECT raw_event_json FROM commentary_evidence_events").fetchall()
    assert all(row["raw_event_json"] is not None for row in raw_events)


# --- Accumulated-feed deduplication ------------------------------------------

def test_persist_observation_identical_second_poll_inserts_no_new_events_and_no_raw(db):
    conn, _ = db
    payload = commentary_payload(events=[_event(comment="Q1 is now underway.", period_number=1, period_seconds=0)])
    first = parse_match_commentary(payload, match_id=9001, match_provider_id="CD_M20260142402", observed_at=_iso(),
                                    match_status_at_poll="LIVE")
    persist_observation(conn, first)
    conn.commit()

    second = parse_match_commentary(payload, match_id=9001, match_provider_id="CD_M20260142402",
                                     observed_at=_iso(15), match_status_at_poll="LIVE")
    outcome = persist_observation(conn, second)
    conn.commit()
    assert outcome["poll_sequence"] == 2
    assert outcome["new_event_count"] == 0
    assert outcome["is_transition"] is False
    assert outcome["transitions"] == []

    event_count = conn.execute("SELECT COUNT(*) FROM commentary_evidence_events").fetchone()[0]
    assert event_count == 1  # not duplicated
    poll_row = conn.execute("SELECT raw_commentary_json FROM commentary_evidence_polls WHERE poll_sequence=2").fetchone()
    assert poll_row["raw_commentary_json"] is None


def test_persist_observation_reobserved_event_updates_last_seen_not_content(db):
    conn, _ = db
    payload = commentary_payload(events=[_event(comment="Q1 is now underway.", period_number=1, period_seconds=0)])
    first = parse_match_commentary(payload, match_id=9001, match_provider_id="CD_M20260142402", observed_at=_iso(),
                                    match_status_at_poll="LIVE")
    persist_observation(conn, first)
    conn.commit()

    second = parse_match_commentary(payload, match_id=9001, match_provider_id="CD_M20260142402",
                                     observed_at=_iso(30), match_status_at_poll="LIVE")
    persist_observation(conn, second)
    conn.commit()

    row = conn.execute("SELECT comment, first_observed_at, last_seen_at, last_seen_poll_sequence FROM commentary_evidence_events").fetchone()
    assert row["comment"] == "Q1 is now underway."
    assert row["first_observed_at"] == _iso()
    assert row["last_seen_at"] == _iso(30)
    assert row["last_seen_poll_sequence"] == 2


# --- Multiple new events between polls ---------------------------------------

def test_persist_observation_multiple_new_events_between_polls(db):
    conn, _ = db
    first_payload = commentary_payload(events=[_event(comment="Q1 is now underway.", period_number=1, period_seconds=0)])
    first = parse_match_commentary(first_payload, match_id=9001, match_provider_id="CD_M20260142402",
                                    observed_at=_iso(), match_status_at_poll="LIVE")
    persist_observation(conn, first)
    conn.commit()

    # Accumulated feed now newest-first with three new events on top.
    second_payload = commentary_payload(events=[
        _event(comment="GOAL - Crows (Toby Murray)", period_number=1, period_seconds=200,
               player_id="CD_I1", team_id="CD_T10", score_event=True),
        _event(comment="BEHIND - Magpies (Jack Crisp)", period_number=1, period_seconds=100,
               player_id="CD_I2", team_id="CD_T40", score_event=True),
        _event(comment="Q1 is now underway.", period_number=1, period_seconds=0),
    ])
    second = parse_match_commentary(second_payload, match_id=9001, match_provider_id="CD_M20260142402",
                                     observed_at=_iso(30), match_status_at_poll="LIVE")
    outcome = persist_observation(conn, second)
    conn.commit()
    assert outcome["new_event_count"] == 2
    assert outcome["is_transition"] is True
    total = conn.execute("SELECT COUNT(*) FROM commentary_evidence_events").fetchone()[0]
    assert total == 3


# --- Same period/second, different commentary --------------------------------

def test_persist_observation_keeps_distinct_events_sharing_period_and_second(db):
    conn, _ = db
    payload = commentary_payload(events=[
        _event(comment="First narrative aside.", period_number=4, period_seconds=1950),
        _event(comment="Second narrative aside.", period_number=4, period_seconds=1950),
        _event(comment="The siren has sounded to end Q4.", period_number=4, period_seconds=1950),
    ])
    observation = parse_match_commentary(payload, match_id=9001, match_provider_id="CD_M20260142402",
                                          observed_at=_iso(), match_status_at_poll="LIVE")
    outcome = persist_observation(conn, observation)
    conn.commit()
    assert outcome["new_event_count"] == 3
    comments = {row["comment"] for row in conn.execute("SELECT comment FROM commentary_evidence_events").fetchall()}
    assert comments == {"First narrative aside.", "Second narrative aside.", "The siren has sounded to end Q4."}


# --- Changed/mutated events --------------------------------------------------

def test_persist_observation_flags_possible_edit_for_player_attributed_event(db):
    conn, _ = db
    first_payload = commentary_payload(events=[
        _event(comment="GOAL - Crows (Toby Murray)", period_number=4, period_seconds=1943,
               player_id="CD_I1", team_id="CD_T10", score_event=True),
    ])
    first = parse_match_commentary(first_payload, match_id=9001, match_provider_id="CD_M20260142402",
                                    observed_at=_iso(), match_status_at_poll="LIVE")
    persist_observation(conn, first)
    conn.commit()

    # Same period/second/player/team/scoreEvent slot, but the comment text
    # changed -- a plausible correction/edit, never overwriting the original row.
    second_payload = commentary_payload(events=[
        _event(comment="GOAL - Crows (Toby Murray) - corrected spelling", period_number=4, period_seconds=1943,
               player_id="CD_I1", team_id="CD_T10", score_event=True),
    ])
    second = parse_match_commentary(second_payload, match_id=9001, match_provider_id="CD_M20260142402",
                                     observed_at=_iso(30), match_status_at_poll="LIVE")
    outcome = persist_observation(conn, second)
    conn.commit()

    assert outcome["new_event_count"] == 1
    assert TRANSITION_POSSIBLE_EVENT_EDIT in outcome["transitions"]
    assert len(outcome["possible_edits"]) == 1

    rows = conn.execute("SELECT id, comment, possible_edit_of_event_id FROM commentary_evidence_events ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["comment"] == "GOAL - Crows (Toby Murray)"
    assert rows[0]["possible_edit_of_event_id"] is None
    assert rows[1]["comment"] == "GOAL - Crows (Toby Murray) - corrected spelling"
    assert rows[1]["possible_edit_of_event_id"] == rows[0]["id"]
    # The original row's content is never overwritten.
    assert conn.execute("SELECT COUNT(*) FROM commentary_evidence_events WHERE comment='GOAL - Crows (Toby Murray)'").fetchone()[0] == 1


def test_persist_observation_does_not_link_narrative_events_sharing_a_slot(db):
    """Narrative commentary with no playerId is deliberately excluded from
    edit-linkage -- see module docstring: too many unrelated comments
    legitimately share a period/second/no-player/no-team slot."""
    conn, _ = db
    first_payload = commentary_payload(events=[
        _event(comment="First narrative aside.", period_number=4, period_seconds=1950),
    ])
    first = parse_match_commentary(first_payload, match_id=9001, match_provider_id="CD_M20260142402",
                                    observed_at=_iso(), match_status_at_poll="LIVE")
    persist_observation(conn, first)
    conn.commit()

    second_payload = commentary_payload(events=[
        _event(comment="Unrelated narrative aside.", period_number=4, period_seconds=1950),
    ])
    second = parse_match_commentary(second_payload, match_id=9001, match_provider_id="CD_M20260142402",
                                     observed_at=_iso(30), match_status_at_poll="LIVE")
    outcome = persist_observation(conn, second)
    conn.commit()
    assert TRANSITION_POSSIBLE_EVENT_EDIT not in outcome["transitions"]
    assert outcome["possible_edits"] == []
    row = conn.execute(
        "SELECT possible_edit_of_event_id FROM commentary_evidence_events WHERE comment='Unrelated narrative aside.'"
    ).fetchone()
    assert row["possible_edit_of_event_id"] is None


# --- Malformed/missing commentary arrays --------------------------------------

def test_persist_observation_missing_commentary_field_is_recorded_without_crashing(db):
    conn, _ = db
    observation = parse_match_commentary(
        {"matchId": "CD_M20260142402"}, match_id=9001, match_provider_id="CD_M20260142402",
        observed_at=_iso(), match_status_at_poll="LIVE",
    )
    outcome = persist_observation(conn, observation)
    conn.commit()
    assert outcome["outcome"] == "success"
    assert outcome["event_count_in_feed"] is None
    assert outcome["new_event_count"] == 0
    assert TRANSITION_COMMENTARY_MISSING_OR_MALFORMED in outcome["transitions"]
    assert outcome["is_transition"] is True
    row = conn.execute("SELECT event_count_in_feed, raw_commentary_json FROM commentary_evidence_polls WHERE poll_sequence=1").fetchone()
    assert row["event_count_in_feed"] is None
    assert row["raw_commentary_json"] is not None  # retained to aid debugging a malformed poll


# --- Endpoint outcome persistence (success/failure transitions) --------------

def test_persist_poll_outcome_records_failure_and_flags_transition_on_change(db):
    conn, _ = db
    first = persist_poll_outcome(
        conn, match_id=9001, match_provider_id="CD_M20260142402", observed_at=_iso(),
        match_status_at_poll="SCHEDULED", outcome="not_published",
    )
    conn.commit()
    assert first["poll_sequence"] == 1
    assert first["is_transition"] is True

    # Repeating the same outcome is not itself a fresh transition.
    second = persist_poll_outcome(
        conn, match_id=9001, match_provider_id="CD_M20260142402", observed_at=_iso(15),
        match_status_at_poll="SCHEDULED", outcome="not_published",
    )
    conn.commit()
    assert second["poll_sequence"] == 2
    assert second["is_transition"] is False

    third = persist_poll_outcome(
        conn, match_id=9001, match_provider_id="CD_M20260142402", observed_at=_iso(30),
        match_status_at_poll="LIVE", outcome="http_error",
    )
    conn.commit()
    assert third["is_transition"] is True

    rows = conn.execute("SELECT poll_sequence, outcome, is_transition FROM commentary_evidence_polls ORDER BY poll_sequence").fetchall()
    assert [dict(row) for row in rows] == [
        {"poll_sequence": 1, "outcome": "not_published", "is_transition": 1},
        {"poll_sequence": 2, "outcome": "not_published", "is_transition": 0},
        {"poll_sequence": 3, "outcome": "http_error", "is_transition": 1},
    ]


def test_persist_observation_recovering_from_failure_is_flagged_as_transition(db):
    """A success poll immediately following a non-success one must itself be
    a visible transition -- even when the feed content is otherwise
    unchanged -- so the endpoint's recovery/availability timeline is not
    silently lost. Regression test for a Codex review finding on PR #197."""
    conn, _ = db
    persist_poll_outcome(
        conn, match_id=9001, match_provider_id="CD_M20260142402", observed_at=_iso(),
        match_status_at_poll="SCHEDULED", outcome="not_published",
    )
    conn.commit()

    payload = commentary_payload(events=[])
    observation = parse_match_commentary(payload, match_id=9001, match_provider_id="CD_M20260142402",
                                          observed_at=_iso(15), match_status_at_poll="LIVE")
    outcome = persist_observation(conn, observation)
    conn.commit()

    assert outcome["poll_sequence"] == 2
    assert outcome["outcome"] == "success"
    assert outcome["new_event_count"] == 0
    assert outcome["is_transition"] is True
    assert TRANSITION_OUTCOME_SUCCESS in outcome["transitions"]

    row = conn.execute(
        "SELECT is_transition, raw_commentary_json FROM commentary_evidence_polls WHERE poll_sequence=2"
    ).fetchone()
    assert row["is_transition"] == 1
    assert row["raw_commentary_json"] is not None  # recovery poll retains raw for investigation


def test_persist_observation_back_to_back_success_after_recovery_is_not_a_transition(db):
    """Once recovered, a further unchanged success poll goes back to being
    a non-transition -- the recovery flag is not sticky."""
    conn, _ = db
    persist_poll_outcome(
        conn, match_id=9001, match_provider_id="CD_M20260142402", observed_at=_iso(),
        match_status_at_poll="SCHEDULED", outcome="not_published",
    )
    conn.commit()
    payload = commentary_payload(events=[])
    persist_observation(conn, parse_match_commentary(
        payload, match_id=9001, match_provider_id="CD_M20260142402", observed_at=_iso(15),
        match_status_at_poll="LIVE",
    ))
    conn.commit()
    third = persist_observation(conn, parse_match_commentary(
        payload, match_id=9001, match_provider_id="CD_M20260142402", observed_at=_iso(30),
        match_status_at_poll="LIVE",
    ))
    conn.commit()
    assert third["is_transition"] is False
    assert third["transitions"] == []


def test_poll_sequence_shared_across_success_and_failure_persistence(db):
    """A poll's success/failure outcomes share one continuous poll_sequence
    counter per match, whichever function persisted it."""
    conn, _ = db
    persist_poll_outcome(
        conn, match_id=9001, match_provider_id="CD_M20260142402", observed_at=_iso(),
        match_status_at_poll="SCHEDULED", outcome="not_published",
    )
    conn.commit()
    payload = commentary_payload(events=[_event(comment="Q1 is now underway.", period_number=1, period_seconds=0)])
    observation = parse_match_commentary(payload, match_id=9001, match_provider_id="CD_M20260142402",
                                          observed_at=_iso(15), match_status_at_poll="LIVE")
    outcome = persist_observation(conn, observation)
    conn.commit()
    assert outcome["poll_sequence"] == 2


# --- Persistence: restart safety ---------------------------------------------

def test_poll_sequence_continues_across_independent_persist_calls(db):
    conn, _ = db
    payload = commentary_payload(events=[_event(comment="Q1 is now underway.", period_number=1, period_seconds=0)])
    first = parse_match_commentary(payload, match_id=9001, match_provider_id="CD_M20260142402", observed_at=_iso(),
                                    match_status_at_poll="LIVE")
    outcome1 = persist_observation(conn, first)
    conn.commit()
    assert outcome1["poll_sequence"] == 1

    # "Restart": nothing but the durable DB carries state to this second call.
    second_payload = commentary_payload(events=[
        _event(comment="Q1 is now underway.", period_number=1, period_seconds=0),
        _event(comment="GOAL - Crows (Toby Murray)", period_number=1, period_seconds=500,
               player_id="CD_I1", team_id="CD_T10", score_event=True),
    ])
    second = parse_match_commentary(second_payload, match_id=9001, match_provider_id="CD_M20260142402",
                                     observed_at=_iso(15), match_status_at_poll="LIVE")
    outcome2 = persist_observation(conn, second)
    conn.commit()
    assert outcome2["poll_sequence"] == 2
    assert outcome2["new_event_count"] == 1

    count = conn.execute(
        "SELECT COUNT(*) FROM commentary_evidence_polls WHERE match_provider_id='CD_M20260142402'"
    ).fetchone()[0]
    assert count == 2


# --- recently_live_match_provider_ids (self-contained post-live grace) -------

def test_recently_live_match_provider_ids_uses_local_status_snapshot(db):
    conn, _ = db
    payload = commentary_payload(events=[])
    obs = parse_match_commentary(payload, match_id=9001, match_provider_id="CD_M20260142402",
                                  observed_at=_iso(), match_status_at_poll="LIVE")
    persist_observation(conn, obs)
    conn.commit()

    result = recently_live_match_provider_ids(conn, now=NOW, grace_seconds=600)
    assert result == [(9001, "CD_M20260142402")]

    from datetime import timedelta
    expired = recently_live_match_provider_ids(conn, now=NOW + timedelta(seconds=700), grace_seconds=600)
    assert expired == []


def test_recently_live_match_provider_ids_disabled_by_zero_grace(db):
    conn, _ = db
    assert recently_live_match_provider_ids(conn, now=NOW, grace_seconds=0) == []


# --- poll_rows / event_rows filtering ----------------------------------------

def test_poll_rows_and_event_rows_filter_by_match_and_category(db):
    conn, _ = db
    payload = commentary_payload(events=[
        _event(comment="Q1 is now underway.", period_number=1, period_seconds=0),
        _event(comment="GOAL - Crows (Toby Murray)", period_number=1, period_seconds=500,
               player_id="CD_I1", team_id="CD_T10", score_event=True),
    ])
    observation = parse_match_commentary(payload, match_id=9001, match_provider_id="CD_M20260142402",
                                          observed_at=_iso(), match_status_at_poll="LIVE")
    persist_observation(conn, observation)
    conn.commit()

    polls = poll_rows(conn, match_provider_id="CD_M20260142402")
    assert len(polls) == 1
    events = event_rows(conn, match_provider_id="CD_M20260142402")
    assert len(events) == 2
    score_only = event_rows(conn, match_provider_id="CD_M20260142402", category=CATEGORY_SCORE_EVENT)
    assert len(score_only) == 1
    assert score_only[0]["comment"] == "GOAL - Crows (Toby Murray)"
