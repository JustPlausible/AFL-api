"""Offline tests for production CFS match-commentary persistence (Issue #201).

No live AFL/CFS access is required: parsing, fingerprinting, categorisation
and canonical resolution are pure/DB-read functions, and persistence is
exercised against a migrated temporary SQLite database, following the same
pattern as tests/test_match_commentary_evidence.py (the diagnostic
counterpart this module is deliberately independent from).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from afl_json.match_commentary import (
    CATEGORY_QUARTER_END,
    CATEGORY_QUARTER_START,
    CATEGORY_SCORE_EVENT,
    MATCH_COMMENTARY_ENDPOINT,
    OUTCOME_SUCCESS,
    MatchCommentaryError,
    categorise_event,
    event_rows,
    parse_commentary_feed,
    persist_commentary_feed,
    persist_poll_outcome,
    recently_active_match_provider_ids,
    resolve_canonical_match_id,
    resolve_canonical_player,
    resolve_canonical_team,
)
from db.migration_runner import migrate_database

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
COMMENTARY_FIXTURES = Path(__file__).parent / "fixtures" / "afl" / "commentary"

HAWTHORN_MATCH_ID = 9101
HAWTHORN_MATCH_PROVIDER_ID = "CD_M20260142409"
BULLDOGS_MATCH_ID = 9102
BULLDOGS_MATCH_PROVIDER_ID = "CD_M20260142406"


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


def commentary_payload(*, events=None, match_id="CD_M20260142409", last_updated="2026-08-23T12:00:00.000+0000"):
    return {
        "matchId": match_id, "lastUpdated": last_updated,
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
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, scraped_at) VALUES(1,'R24',73,1,?)",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, "
        "start_time_utc, season_id, scraped_at) VALUES(?,?,1,'West Coast','Hawthorn','Optus Stadium','POSTGAME',?,73,?)",
        (HAWTHORN_MATCH_ID, HAWTHORN_MATCH_PROVIDER_ID, NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, "
        "start_time_utc, season_id, scraped_at) VALUES(?,?,1,'Western Bulldogs','Melbourne','Marvel Stadium','LIVE',?,73,?)",
        (BULLDOGS_MATCH_ID, BULLDOGS_MATCH_PROVIDER_ID, NOW.isoformat(), NOW.isoformat()),
    )
    # Canonical crosswalks: one resolvable player/team, one deliberately absent (unresolved).
    conn.execute(
        "INSERT INTO canonical_players VALUES(?,?,?,?,?,?)",
        (501, "Jack Gunston", "Jack", "Gunston", NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) "
        "VALUES(?,?,?,?,?)",
        (501, "champion_data", "CD_I291351", NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO afl_teams VALUES(?,'CD_T80','Hawthorn','HAW','Hawks','Hawthorn','Hawthorn','MEN','{}','{}','{}',?)",
        (80, NOW.isoformat()),
    )
    conn.commit()
    yield conn, path
    conn.close()


# --- Endpoint contract -------------------------------------------------------

def test_match_commentary_endpoint_resolves_to_cfs_commentary_root_not_cfs_afl():
    resolved = MATCH_COMMENTARY_ENDPOINT.url_template.format(match_provider_id=HAWTHORN_MATCH_PROVIDER_ID)
    assert resolved == f"https://api.afl.com.au/cfs/commentaryFeed/{HAWTHORN_MATCH_PROVIDER_ID}"


def test_match_commentary_endpoint_is_marked_verified_and_independent_of_diagnostic_definition():
    from collection.match_commentary_evidence import MATCH_COMMENTARY_ENDPOINT as diagnostic_endpoint
    assert MATCH_COMMENTARY_ENDPOINT.verified is True
    assert MATCH_COMMENTARY_ENDPOINT is not diagnostic_endpoint


# --- Parsing ------------------------------------------------------------------

def test_parse_real_reduced_capture_preserves_every_field_and_source_order():
    payload = commentary_fixture("commentary_CD_M20260142409_reduced.json")
    feed = parse_commentary_feed(
        payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(),
    )
    assert feed.feed_last_updated == "2026-08-23T12:15:40.217+0000"
    assert len(feed.events) == 12
    assert [event.source_index for event in feed.events] == list(range(12))
    first = feed.events[0]
    assert first.period_number == 4 and first.period_seconds == 1821
    assert first.player_provider_id is None and first.team_provider_id is None


def test_parse_rejects_non_object_payload():
    with pytest.raises(MatchCommentaryError):
        parse_commentary_feed([], match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())


def test_parse_treats_missing_commentary_event_as_none_not_empty_list():
    payload = {"matchId": HAWTHORN_MATCH_PROVIDER_ID, "lastUpdated": "x"}
    feed = parse_commentary_feed(
        payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(),
    )
    assert feed.events is None


def test_parse_rejects_payload_for_a_different_match():
    """A misrouted/mis-cached response (or a wrong replay-CLI override) must
    never be silently persisted against the wrong canonical match."""
    payload = commentary_payload(events=[_event()], match_id="CD_M_SOME_OTHER_MATCH")
    with pytest.raises(MatchCommentaryError):
        parse_commentary_feed(
            payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(),
        )


def test_parse_tolerates_missing_top_level_match_id():
    payload = {"lastUpdated": "x", "commentaryEvent": [_event()]}
    feed = parse_commentary_feed(
        payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(),
    )
    assert len(feed.events) == 1


def test_categorise_event_matches_structural_and_narrow_text_rules():
    assert categorise_event(comment="anything", period_seconds=0, score_event=True) == CATEGORY_SCORE_EVENT
    assert categorise_event(comment="Q1 is now underway.", period_seconds=0, score_event=False) == CATEGORY_QUARTER_START
    assert categorise_event(comment="Q1 is now underway.", period_seconds=5, score_event=False) is None
    assert categorise_event(comment="The siren has sounded to end Q4.", period_seconds=1821, score_event=False) == CATEGORY_QUARTER_END
    assert categorise_event(comment="Some narrative.", period_seconds=100, score_event=False) is None


# --- Canonical identity resolution --------------------------------------------

def test_resolve_canonical_player_resolves_known_crosswalk(db):
    conn, _ = db
    assert resolve_canonical_player(conn, "CD_I291351") == 501


def test_resolve_canonical_player_returns_none_for_unknown_id_never_guesses(db):
    conn, _ = db
    assert resolve_canonical_player(conn, "CD_I999999999") is None
    assert resolve_canonical_player(conn, None) is None


def test_resolve_canonical_team_resolves_known_crosswalk(db):
    conn, _ = db
    assert resolve_canonical_team(conn, "CD_T80") == 80


def test_resolve_canonical_team_returns_none_for_unknown_id(db):
    conn, _ = db
    assert resolve_canonical_team(conn, "CD_T999") is None


def test_resolve_canonical_match_id_resolves_by_provider_id(db):
    conn, _ = db
    assert resolve_canonical_match_id(conn, HAWTHORN_MATCH_PROVIDER_ID) == HAWTHORN_MATCH_ID
    assert resolve_canonical_match_id(conn, "CD_M_UNKNOWN") is None


# --- Persistence: dedup / idempotency ------------------------------------------

def test_accumulated_feed_idempotency_second_identical_poll_adds_no_rows(db):
    conn, _ = db
    payload = commentary_fixture("commentary_CD_M20260142409_reduced.json")
    feed1 = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(0))
    result1 = persist_commentary_feed(conn, feed1)
    assert result1["new_event_count"] == 12

    feed2 = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(20))
    result2 = persist_commentary_feed(conn, feed2)
    assert result2["new_event_count"] == 0

    total_rows = conn.execute("SELECT COUNT(*) FROM match_commentary_events WHERE match_id=?", (HAWTHORN_MATCH_ID,)).fetchone()[0]
    assert total_rows == 12
    # last_observed_at bookkeeping is still touched on the repeat poll.
    row = conn.execute(
        "SELECT last_observed_at FROM match_commentary_events WHERE match_provider_id=? AND period_number=4 AND period_seconds=1821 LIMIT 1",
        (HAWTHORN_MATCH_PROVIDER_ID,),
    ).fetchone()
    assert row["last_observed_at"] == _iso(20)


def test_multiple_new_events_between_polls_only_new_fingerprints_inserted(db):
    conn, _ = db
    early = commentary_payload(events=[
        _event(comment="Q1 is now underway.", period_number=1, period_seconds=0),
        _event(comment="GOAL - Hawks (Jack Gunston)", period_number=1, period_seconds=59, player_id="CD_I291351", team_id="CD_T80", score_event=True),
    ])
    feed1 = parse_commentary_feed(early, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(0))
    result1 = persist_commentary_feed(conn, feed1)
    assert result1["new_event_count"] == 2

    later = commentary_payload(events=[
        _event(comment="GOAL - Hawks (Mitch Lewis)", period_number=1, period_seconds=164, player_id="CD_I1000887", team_id="CD_T80", score_event=True),
        _event(comment="BEHIND - Eagles (Elliot Yeo)", period_number=1, period_seconds=238, player_id="CD_I292128", team_id="CD_T150", score_event=True),
        _event(comment="GOAL - Hawks (Jack Gunston)", period_number=1, period_seconds=59, player_id="CD_I291351", team_id="CD_T80", score_event=True),
        _event(comment="Q1 is now underway.", period_number=1, period_seconds=0),
    ])
    feed2 = parse_commentary_feed(later, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(20))
    result2 = persist_commentary_feed(conn, feed2)
    assert result2["new_event_count"] == 2

    total_rows = conn.execute("SELECT COUNT(*) FROM match_commentary_events WHERE match_id=?", (HAWTHORN_MATCH_ID,)).fetchone()[0]
    assert total_rows == 4


def test_multiple_events_at_same_period_second_are_distinct_rows(db):
    conn, _ = db
    payload = commentary_fixture("commentary_CD_M20260142409_reduced.json")
    feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())
    persist_commentary_feed(conn, feed)

    rows = conn.execute(
        "SELECT comment, score_event FROM match_commentary_events WHERE match_provider_id=? AND period_number=1 AND period_seconds=1483 ORDER BY id",
        (HAWTHORN_MATCH_PROVIDER_ID,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["score_event"] == 0  # narrative, not scoreEvent
    assert rows[1]["score_event"] == 1  # the GOAL

    rows_1821 = conn.execute(
        "SELECT comment FROM match_commentary_events WHERE match_provider_id=? AND period_number=4 AND period_seconds=1821",
        (HAWTHORN_MATCH_PROVIDER_ID,),
    ).fetchall()
    assert len(rows_1821) == 2


def test_restart_replay_persisting_same_feed_across_independent_calls_is_idempotent(db):
    """Simulates a scheduler restart: two entirely independent persist calls
    (fresh parse each time) against the same accumulated feed must not
    duplicate rows, mirroring restart-safety already proven for the
    diagnostic module."""
    conn, _ = db
    payload = commentary_fixture("commentary_CD_M20260142409_reduced.json")
    for offset in (0, 30, 60):
        feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(offset))
        persist_commentary_feed(conn, feed)
    total_rows = conn.execute("SELECT COUNT(*) FROM match_commentary_events WHERE match_id=?", (HAWTHORN_MATCH_ID,)).fetchone()[0]
    assert total_rows == 12
    poll_count = conn.execute("SELECT COUNT(*) FROM match_commentary_polls WHERE match_provider_id=?", (HAWTHORN_MATCH_PROVIDER_ID,)).fetchone()[0]
    assert poll_count == 3


def test_legitimate_duplicate_text_at_different_slots_creates_separate_rows(db):
    conn, _ = db
    payload = commentary_payload(events=[
        _event(comment="BEHIND - Hawks (Rushed)", period_number=4, period_seconds=409, team_id="CD_T80", score_event=True),
        _event(comment="BEHIND - Hawks (Rushed)", period_number=2, period_seconds=100, team_id="CD_T80", score_event=True),
    ])
    feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())
    result = persist_commentary_feed(conn, feed)
    assert result["new_event_count"] == 2


# --- Canonical linking at persistence time -------------------------------------

def test_player_linked_event_resolves_canonical_player(db):
    conn, _ = db
    payload = commentary_payload(events=[
        _event(comment="GOAL - Hawks (Jack Gunston)", period_number=1, period_seconds=59, player_id="CD_I291351", team_id="CD_T80", score_event=True),
    ])
    feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())
    persist_commentary_feed(conn, feed)
    row = conn.execute("SELECT canonical_player_id, canonical_team_id FROM match_commentary_events WHERE match_provider_id=?", (HAWTHORN_MATCH_PROVIDER_ID,)).fetchone()
    assert row["canonical_player_id"] == 501
    assert row["canonical_team_id"] == 80


def test_unresolved_provider_ids_stay_null_never_guessed(db):
    conn, _ = db
    payload = commentary_payload(events=[
        _event(comment="GOAL - Someone (Unknown Player)", period_number=1, period_seconds=10, player_id="CD_I_UNKNOWN", team_id="CD_T_UNKNOWN", score_event=True),
    ])
    feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())
    persist_commentary_feed(conn, feed)
    row = conn.execute(
        "SELECT player_provider_id, canonical_player_id, team_provider_id, canonical_team_id FROM match_commentary_events WHERE match_provider_id=?",
        (HAWTHORN_MATCH_PROVIDER_ID,),
    ).fetchone()
    assert row["player_provider_id"] == "CD_I_UNKNOWN"
    assert row["canonical_player_id"] is None
    assert row["team_provider_id"] == "CD_T_UNKNOWN"
    assert row["canonical_team_id"] is None


def test_null_player_and_team_ids_persist_as_null(db):
    conn, _ = db
    payload = commentary_payload(events=[_event(comment="Q1 is now underway.", period_number=1, period_seconds=0)])
    feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())
    persist_commentary_feed(conn, feed)
    row = conn.execute(
        "SELECT player_provider_id, canonical_player_id, team_provider_id, canonical_team_id, category FROM match_commentary_events WHERE match_provider_id=?",
        (HAWTHORN_MATCH_PROVIDER_ID,),
    ).fetchone()
    assert row["player_provider_id"] is None and row["canonical_player_id"] is None
    assert row["team_provider_id"] is None and row["canonical_team_id"] is None
    assert row["category"] == CATEGORY_QUARTER_START


def test_score_event_persisted_exactly_as_supplied(db):
    conn, _ = db
    payload = commentary_payload(events=[
        _event(comment="GOAL - Hawks (Jack Gunston)", period_number=1, period_seconds=59, player_id="CD_I291351", team_id="CD_T80", score_event=True),
        _event(comment="Some narrative.", period_number=1, period_seconds=60, score_event=False),
    ])
    feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())
    persist_commentary_feed(conn, feed)
    rows = {row["comment"]: row["score_event"] for row in conn.execute(
        "SELECT comment, score_event FROM match_commentary_events WHERE match_provider_id=?", (HAWTHORN_MATCH_PROVIDER_ID,),
    )}
    assert rows["GOAL - Hawks (Jack Gunston)"] == 1
    assert rows["Some narrative."] == 0


def test_event_with_missing_comment_persists_with_null_comment(db):
    """comment is nullable end-to-end (persistence + API model, see
    api/routes_v1.py's CommentaryEvent) since the source occasionally omits
    it -- this must never raise on write or on a later unfiltered read."""
    conn, _ = db
    payload = commentary_payload(events=[{"periodNumber": 1, "periodSeconds": 5, "playerId": None, "teamId": None, "scoreEvent": False}])
    feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())
    result = persist_commentary_feed(conn, feed)
    assert result["new_event_count"] == 1
    row = conn.execute("SELECT comment FROM match_commentary_events WHERE match_provider_id=?", (HAWTHORN_MATCH_PROVIDER_ID,)).fetchone()
    assert row["comment"] is None


def test_replay_source_and_collector_version_overrides_mark_provenance(db):
    """scripts/import_commentary_capture.py always passes distinct source/
    collector_version so a replay is never indistinguishable in the
    database from a genuine live production poll."""
    conn, _ = db
    payload = commentary_payload(events=[_event()])
    feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())
    persist_commentary_feed(conn, feed, source="replay:manual test", collector_version="match_commentary_import_v1")

    event_row = conn.execute(
        "SELECT source, collector_version FROM match_commentary_events WHERE match_provider_id=?",
        (HAWTHORN_MATCH_PROVIDER_ID,),
    ).fetchone()
    assert event_row["source"] == "replay:manual test"
    assert event_row["collector_version"] == "match_commentary_import_v1"

    poll_row = conn.execute(
        "SELECT collector_version FROM match_commentary_polls WHERE match_provider_id=?",
        (HAWTHORN_MATCH_PROVIDER_ID,),
    ).fetchone()
    assert poll_row["collector_version"] == "match_commentary_import_v1"


def test_replay_touch_only_poll_is_still_marked_even_with_no_new_events(db):
    """Even when a replay only re-observes already-known fingerprints (no
    new event rows), the poll row it writes must still carry the replay's
    collector_version -- this is what makes a touch-only replay
    distinguishable from a live poll."""
    conn, _ = db
    payload = commentary_payload(events=[_event()])
    feed1 = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(0))
    persist_commentary_feed(conn, feed1)  # genuine "live" poll

    feed2 = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(30))
    result2 = persist_commentary_feed(conn, feed2, source="replay:manual test", collector_version="match_commentary_import_v1")
    assert result2["new_event_count"] == 0

    poll_rows = conn.execute(
        "SELECT poll_sequence, collector_version FROM match_commentary_polls WHERE match_provider_id=? ORDER BY poll_sequence",
        (HAWTHORN_MATCH_PROVIDER_ID,),
    ).fetchall()
    assert poll_rows[0]["collector_version"] != "match_commentary_import_v1"
    assert poll_rows[1]["collector_version"] == "match_commentary_import_v1"


def test_missing_commentary_array_is_not_a_hard_failure(db):
    """A dict payload with a missing/malformed commentaryEvent field parses
    successfully (network + JSON shape are fine); only the array itself is
    unknown for this poll -- mirrors the diagnostic module's same choice."""
    conn, _ = db
    payload = {"matchId": HAWTHORN_MATCH_PROVIDER_ID, "lastUpdated": "x"}  # missing commentaryEvent
    feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())
    result = persist_commentary_feed(conn, feed)
    assert result["outcome"] == OUTCOME_SUCCESS
    assert result["new_event_count"] == 0
    assert result["event_count_in_feed"] is None


def test_persist_poll_outcome_records_non_success_attempts(db):
    conn, _ = db
    result = persist_poll_outcome(
        conn, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID,
        observed_at=_iso(), match_status_at_poll="LIVE", outcome="not_published",
    )
    assert result["outcome"] == "not_published"
    row = conn.execute("SELECT outcome FROM match_commentary_polls WHERE match_provider_id=?", (HAWTHORN_MATCH_PROVIDER_ID,)).fetchone()
    assert row["outcome"] == "not_published"


# --- Same-slot scoring-outcome change preservation (CD_M20260142406 real evidence) ---

def test_same_slot_scoring_outcome_change_preserves_both_events_and_links_them(db):
    """Real Round 24 scoring-outcome change (see
    commentary_CD_M20260142406_score_review.metadata.json): a GOAL is
    followed, at the identical match-clock/player/team/scoreEvent slot, by a
    BEHIND -- confirmed both by the live diagnostic poll sequence (poll1)
    and by the real final concluded-match capture (poll2, a reduced,
    verbatim subset of commentary_CD_M20260142406_full.json), which shows
    only the BEHIND remains upstream. Both must remain persisted regardless;
    the later event must be linked back to the earlier one via
    possible_edit_of_event_id. Deliberately not called a "review" or
    "reversal" -- the feed never states why the outcome changed."""
    conn, _ = db
    poll1 = commentary_fixture("commentary_CD_M20260142406_score_review_poll1.json")
    poll2 = commentary_fixture("commentary_CD_M20260142406_score_review_poll2.json")

    feed1 = parse_commentary_feed(poll1, match_id=BULLDOGS_MATCH_ID, match_provider_id=BULLDOGS_MATCH_PROVIDER_ID, observed_at=_iso(0))
    persist_commentary_feed(conn, feed1)

    feed2 = parse_commentary_feed(poll2, match_id=BULLDOGS_MATCH_ID, match_provider_id=BULLDOGS_MATCH_PROVIDER_ID, observed_at=_iso(75))
    result2 = persist_commentary_feed(conn, feed2)

    assert result2["new_event_count"] == 1
    assert len(result2["possible_edits"]) == 1

    rows = conn.execute(
        "SELECT id, comment, possible_edit_of_event_id FROM match_commentary_events "
        "WHERE match_provider_id=? AND period_number=3 AND period_seconds=839 ORDER BY id",
        (BULLDOGS_MATCH_PROVIDER_ID,),
    ).fetchall()
    assert len(rows) == 2
    goal_row, behind_row = rows[0], rows[1]
    assert goal_row["comment"] == "GOAL - Bulldogs (Cody Weightman)"
    assert goal_row["possible_edit_of_event_id"] is None
    assert behind_row["comment"] == "BEHIND - Bulldogs (Cody Weightman)"
    assert behind_row["possible_edit_of_event_id"] == goal_row["id"]

    # The earlier GOAL row's own fields were never rewritten.
    still_there = conn.execute("SELECT comment FROM match_commentary_events WHERE id=?", (goal_row["id"],)).fetchone()
    assert still_there["comment"] == "GOAL - Bulldogs (Cody Weightman)"


def test_edit_linkage_requires_a_nonnull_player_id(db):
    """Mirrors the diagnostic module's restriction: two unrelated narrative
    events sharing a null-player slot must not be linked as a possible edit."""
    conn, _ = db
    payload1 = commentary_payload(events=[_event(comment="First narrative aside.", period_number=2, period_seconds=500)])
    feed1 = parse_commentary_feed(payload1, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(0))
    persist_commentary_feed(conn, feed1)

    payload2 = commentary_payload(events=[_event(comment="Unrelated second aside.", period_number=2, period_seconds=500)])
    feed2 = parse_commentary_feed(payload2, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso(30))
    result2 = persist_commentary_feed(conn, feed2)

    assert result2["new_event_count"] == 1
    assert result2["possible_edits"] == []


# --- Consumer query surface: afl_json.match_commentary.event_rows -------------

def test_event_rows_default_ordering_is_chronological_oldest_first(db):
    conn, _ = db
    payload = commentary_fixture("commentary_CD_M20260142409_reduced.json")
    feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())
    persist_commentary_feed(conn, feed)

    rows = event_rows(conn, match_id=HAWTHORN_MATCH_ID)
    pairs = [(row["period_number"], row["period_seconds"]) for row in rows]
    assert pairs == sorted(pairs)
    assert pairs[0] == (0, 0)
    assert pairs[-1] == (4, 1821)


def test_event_rows_same_second_tiebreak_uses_source_index_descending(db):
    conn, _ = db
    payload = commentary_fixture("commentary_CD_M20260142409_reduced.json")
    feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())
    persist_commentary_feed(conn, feed)

    rows = event_rows(conn, match_id=HAWTHORN_MATCH_ID, period_number=1)
    same_second = [row for row in rows if row["period_seconds"] == 1483]
    # Source array is newest-first: the narrative comment appears BEFORE the
    # goal in the raw array (lower source_index == more recent), so the
    # documented oldest-first tiebreak (source_index DESC) must place the
    # goal first, then the narrative comment.
    assert [row["comment"] for row in same_second] == [
        "GOAL - Hawks (Jack Gunston)",
        "West Coast have 22 kicks and 42 handballs for the match which is inviting the Hawthorn pressure. "
        "The Hawks have a pressure rating of 198, compared to West Coast's 181.",
    ]


def test_event_rows_filters_period_player_team_and_score_events_only(db):
    conn, _ = db
    payload = commentary_fixture("commentary_CD_M20260142409_reduced.json")
    feed = parse_commentary_feed(payload, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID, observed_at=_iso())
    persist_commentary_feed(conn, feed)

    assert len(event_rows(conn, match_id=HAWTHORN_MATCH_ID, period_number=0)) == 2
    assert all(row["score_event"] for row in event_rows(conn, match_id=HAWTHORN_MATCH_ID, score_events_only=True))
    by_player = event_rows(conn, match_id=HAWTHORN_MATCH_ID, canonical_player_id=501)
    assert len(by_player) == 1 and by_player[0]["comment"] == "GOAL - Hawks (Jack Gunston)"
    # Two Hawthorn (CD_T80)-linked events are present in the reduced fixture:
    # the Jack Gunston goal and the Jack Dalton behind.
    by_team = event_rows(conn, match_id=HAWTHORN_MATCH_ID, canonical_team_id=80)
    assert len(by_team) == 2


# --- Candidate window ----------------------------------------------------------

def test_recently_active_match_provider_ids_within_grace_window(db):
    conn, _ = db
    persist_poll_outcome(
        conn, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID,
        observed_at=_iso(0), match_status_at_poll="POSTGAME", outcome="not_published",
    )
    conn.commit()
    result = recently_active_match_provider_ids(conn, now=NOW.replace(tzinfo=timezone.utc), grace_seconds=600)
    assert (HAWTHORN_MATCH_ID, HAWTHORN_MATCH_PROVIDER_ID) in result


def test_recently_active_match_provider_ids_excludes_outside_grace_window(db):
    conn, _ = db
    persist_poll_outcome(
        conn, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID,
        observed_at=_iso(-3600), match_status_at_poll="POSTGAME", outcome="not_published",
    )
    conn.commit()
    result = recently_active_match_provider_ids(conn, now=NOW.replace(tzinfo=timezone.utc), grace_seconds=600)
    assert result == []


def test_recently_active_match_provider_ids_ignores_concluded_only_polls(db):
    conn, _ = db
    persist_poll_outcome(
        conn, match_id=HAWTHORN_MATCH_ID, match_provider_id=HAWTHORN_MATCH_PROVIDER_ID,
        observed_at=_iso(0), match_status_at_poll="CONCLUDED", outcome="success",
    )
    conn.commit()
    result = recently_active_match_provider_ids(conn, now=NOW.replace(tzinfo=timezone.utc), grace_seconds=600)
    assert result == []
