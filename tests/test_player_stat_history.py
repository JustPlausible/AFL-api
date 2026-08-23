"""Tests for append-only CFS player-stat history and period checkpoints (Issue #195).

These exercise ``afl_json.player_stats.upsert_player_stats`` directly (the
single write path every collector/scheduler/CLI entry point already calls --
see ``docs/architecture/player_stats_storage_contract.md``), so there is no
second player-stat poller or scheduler path involved anywhere in this file.

The Cody Weightman GOAL -> BEHIND scoring-outcome reversal
(``commentaryFeed`` evidence, see ``tests/fixtures/afl/commentary/``) is used
here only as the *shape* of a synthetic goal/behind reversal fixture -- no
matching CFS ``playerStats`` before/after snapshot for that real match
(``CD_M20260142406``) exists anywhere in this repository's fixtures or
diagnostic captures, so this test cannot and does not claim to reproduce the
real event. It is a labelled-synthetic regression test for the general
goal/behind reversal contract only.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from afl_json.match_period import MatchPeriodState
from afl_json.player_stats import (
    PlayerStatsStatus, normalise_player_stats, upsert_player_stats,
)


def _schema(conn: sqlite3.Connection) -> None:
    from db.migration_runner import discover_migrations
    wanted = {"0006", "0009", "0020"}
    for migration in discover_migrations():
        if migration.identifier in wanted:
            migration.module.migrate(conn)


def _payload(players, *, status=None):
    home, away = [], []
    for player_id, side, stats in players:
        entry = {"player": {"playerId": player_id}, "playerStats": {"stats": stats}}
        (home if side == "home" else away).append(entry)
    payload = {"homeTeamPlayerStats": home, "awayTeamPlayerStats": away}
    if status is not None:
        payload["status"] = status
    return payload


def _result(players, *, collected_at, status=None, canonical_match_status=None):
    return normalise_player_stats(
        _payload(players, status=status), "CD_M1",
        collected_at=collected_at, canonical_match_status=canonical_match_status,
    )


def _history(conn, player_id="CD_I1"):
    return conn.execute(
        "SELECT stat_field, previous_value, new_value, delta, match_period_state, observed_at "
        "FROM cfs_player_stat_history WHERE champion_data_player_id=? ORDER BY id",
        (player_id,),
    ).fetchall()


def _checkpoints(conn, player_id="CD_I1"):
    return conn.execute(
        "SELECT checkpoint_marker, goals, behinds, kicks, handballs, disposals, marks, tackles, hitouts "
        "FROM cfs_player_stat_checkpoints WHERE champion_data_player_id=? ORDER BY id",
        (player_id,),
    ).fetchall()


def _db():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    return conn


# --- 1. Baseline -------------------------------------------------------

def test_first_observation_is_baseline_not_fabricated_deltas():
    conn = _db()
    result = _result([("CD_I1", "home", {"goals": 1, "tackles": 4})], collected_at="2026-01-01T00:00:00+00:00")
    assert upsert_player_stats(conn, result) == 1
    assert _history(conn) == []
    checkpoints = _checkpoints(conn)
    assert [row[0] for row in checkpoints] == ["BASELINE"]
    assert checkpoints[0][1:3] == (1, None)  # goals=1, behinds=None


# --- 2. Identical repeat -------------------------------------------------

def test_identical_repeated_snapshot_creates_no_history():
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at="2026-01-01T00:00:00+00:00"))
    written = upsert_player_stats(
        conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at="2026-01-01T00:05:00+00:00")
    )
    assert written == 0
    assert _history(conn) == []
    assert len(_checkpoints(conn)) == 1


# --- 3 & 4. Positive and negative deltas --------------------------------

def test_single_positive_change_tackles_4_to_5():
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at="2026-01-01T00:00:00+00:00"))
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 5})], collected_at="2026-01-01T00:05:00+00:00"))
    rows = _history(conn)
    assert len(rows) == 1
    assert rows[0][:4] == ("tackles", 4, 5, 1)


def test_negative_change_tackles_6_to_5_is_preserved():
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 6})], collected_at="2026-01-01T00:00:00+00:00"))
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 5})], collected_at="2026-01-01T00:05:00+00:00"))
    rows = _history(conn)
    assert len(rows) == 1
    assert rows[0][:4] == ("tackles", 6, 5, -1)


# --- 5. Multiple fields in one poll --------------------------------------

def test_multiple_fields_changing_in_one_poll_are_all_retained():
    conn = _db()
    upsert_player_stats(conn, _result(
        [("CD_I1", "home", {"tackles": 4, "kicks": 10, "marks": 2})], collected_at="2026-01-01T00:00:00+00:00"
    ))
    upsert_player_stats(conn, _result(
        [("CD_I1", "home", {"tackles": 5, "kicks": 12, "marks": 2})], collected_at="2026-01-01T00:05:00+00:00"
    ))
    rows = {row[0]: (row[1], row[2], row[3]) for row in _history(conn)}
    assert rows == {"tackles": (4, 5, 1), "kicks": (10, 12, 2)}
    assert "marks" not in rows


# --- 6. Multiple players in one poll --------------------------------------

def test_multiple_players_changing_in_one_poll():
    conn = _db()
    upsert_player_stats(conn, _result([
        ("CD_I1", "home", {"tackles": 4}), ("CD_I2", "away", {"tackles": 1}),
    ], collected_at="2026-01-01T00:00:00+00:00"))
    upsert_player_stats(conn, _result([
        ("CD_I1", "home", {"tackles": 5}), ("CD_I2", "away", {"tackles": 3}),
    ], collected_at="2026-01-01T00:05:00+00:00"))
    assert [row[:4] for row in _history(conn, "CD_I1")] == [("tackles", 4, 5, 1)]
    assert [row[:4] for row in _history(conn, "CD_I2")] == [("tackles", 1, 3, 2)]


# --- 7. Goal/behind scoring-outcome reversal (labelled synthetic) --------

def test_goal_behind_scoring_outcome_reversal_is_recorded_as_observed_synthetic_fixture():
    """SYNTHETIC fixture: shape mirrors the real Cody Weightman GOAL -> BEHIND
    commentary reversal for match CD_M20260142406, but no real CFS playerStats
    before/after snapshot for that event exists in this repository -- see
    module docstring. This only proves the general reversal contract: a
    goal/behind swap is stored as two independent factual field transitions,
    never as a 'correction' or 'score review'."""
    conn = _db()
    upsert_player_stats(conn, _result(
        [("CD_I1", "home", {"goals": 1, "behinds": 0})], collected_at="2026-01-01T00:00:00+00:00"
    ))
    upsert_player_stats(conn, _result(
        [("CD_I1", "home", {"goals": 0, "behinds": 1})], collected_at="2026-01-01T00:05:00+00:00"
    ))
    rows = {row[0]: (row[1], row[2], row[3]) for row in _history(conn)}
    assert rows == {"goals": (1, 0, -1), "behinds": (0, 1, 1)}


# --- 8. Meaningful null/value transitions --------------------------------

def test_field_absent_then_present_is_a_meaningful_null_transition():
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 1})], collected_at="2026-01-01T00:00:00+00:00"))
    # hitouts was never reported for CD_I1's baseline poll (absent, not invalid).
    upsert_player_stats(conn, _result(
        [("CD_I1", "home", {"tackles": 1, "hitouts": 3})], collected_at="2026-01-01T00:05:00+00:00"
    ))
    rows = {row[0]: (row[1], row[2], row[3]) for row in _history(conn)}
    assert rows == {"hitouts": (None, 3, None)}


def test_field_present_then_absent_is_a_meaningful_null_transition():
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"hitouts": 3})], collected_at="2026-01-01T00:00:00+00:00"))
    upsert_player_stats(conn, _result([("CD_I1", "home", {"hitouts": 3, "tackles": 1})],
                                       collected_at="2026-01-01T00:05:00+00:00"))
    # Confirm hitouts staying steady while tackles newly appears does not
    # touch hitouts, then genuinely drop hitouts from the source next poll.
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 1})], collected_at="2026-01-01T00:10:00+00:00"))
    rows = _history(conn)
    fields = [row[0] for row in rows]
    assert fields.count("hitouts") == 1
    hitouts_row = next(row for row in rows if row[0] == "hitouts")
    assert hitouts_row[1:3] == (3, None)


# --- 9. Invalid/partial source data does not fabricate deltas -----------

def test_invalid_numeric_field_does_not_create_a_false_delta():
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"goals": 2})], collected_at="2026-01-01T00:00:00+00:00"))
    result = _result([("CD_I1", "home", {"goals": "not-a-number"})], collected_at="2026-01-01T00:05:00+00:00")
    assert any(d.code == "invalid_numeric" for d in result.diagnostics)
    upsert_player_stats(conn, result)
    # cfs_player_stats itself still reflects existing (unchanged) semantics:
    # the invalid field is stored as NULL there, but history must not treat
    # that as an observed goals: 2 -> None removal.
    assert conn.execute(
        "SELECT goals FROM cfs_player_stats WHERE champion_data_player_id='CD_I1'"
    ).fetchone() == (None,)
    assert _history(conn) == []


def test_recovery_after_an_invalid_poll_diffs_against_last_known_good_value():
    """PR #203 review finding: an invalid_numeric poll in between two valid
    polls must not make the later valid poll compare against the malformed
    poll's incidental cfs_player_stats NULL. goals: 2 -> invalid -> 3 must
    record a single 2 -> 3 (+1) transition, not a fabricated None -> 3."""
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"goals": 2})], collected_at="2026-01-01T00:00:00+00:00"))
    invalid = _result([("CD_I1", "home", {"goals": "not-a-number"})], collected_at="2026-01-01T00:05:00+00:00")
    upsert_player_stats(conn, invalid)
    assert conn.execute(
        "SELECT goals FROM cfs_player_stats WHERE champion_data_player_id='CD_I1'"
    ).fetchone() == (None,)
    upsert_player_stats(conn, _result([("CD_I1", "home", {"goals": 3})], collected_at="2026-01-01T00:10:00+00:00"))
    rows = _history(conn)
    assert len(rows) == 1
    assert rows[0][:4] == ("goals", 2, 3, 1)


# --- 10. Canonical player linkage ----------------------------------------

def test_canonical_player_linkage_is_retained_when_available():
    conn = _db()
    now = "2026-01-01T00:00:00+00:00"
    conn.execute("INSERT INTO canonical_players (id, created_at, updated_at) VALUES (42, ?, ?)", (now, now))
    conn.execute(
        "INSERT INTO player_provider_ids (player_id, provider, provider_player_id, created_at, updated_at) "
        "VALUES (42, 'champion_data', 'CD_I1', ?, ?)", (now, now),
    )
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at=now))
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 5})], collected_at="2026-01-01T00:05:00+00:00"))
    history_canonical = conn.execute(
        "SELECT canonical_player_id FROM cfs_player_stat_history WHERE champion_data_player_id='CD_I1'"
    ).fetchone()
    checkpoint_canonical = conn.execute(
        "SELECT canonical_player_id FROM cfs_player_stat_checkpoints WHERE champion_data_player_id='CD_I1' "
        "AND checkpoint_marker='BASELINE'"
    ).fetchone()
    assert history_canonical == (42,)
    assert checkpoint_canonical == (42,)


# --- 11. Chronological ordering -------------------------------------------

def test_history_query_orders_chronologically_by_match_and_player():
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 1})], collected_at="2026-01-01T00:00:00+00:00"))
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 2})], collected_at="2026-01-01T00:05:00+00:00"))
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 3})], collected_at="2026-01-01T00:10:00+00:00"))
    rows = conn.execute(
        "SELECT previous_value, new_value FROM cfs_player_stat_history "
        "WHERE match_provider_id='CD_M1' AND champion_data_player_id='CD_I1' ORDER BY observed_at"
    ).fetchall()
    assert rows == [(1, 2), (2, 3)]


# --- 12 & 13. Checkpoint dedup + progression ------------------------------

def test_repeated_poll_at_a_break_does_not_duplicate_checkpoints():
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at="2026-01-01T00:00:00+00:00"),
                        match_period_state=MatchPeriodState.Q1)
    for minute in (20, 21, 22):
        upsert_player_stats(
            conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at=f"2026-01-01T00:{minute}:00+00:00"),
            match_period_state=MatchPeriodState.QUARTER_TIME,
        )
    markers = [row[0] for row in _checkpoints(conn)]
    assert markers == ["BASELINE", "QT"]


def test_checkpoint_progression_through_all_regulation_markers():
    conn = _db()
    sequence = [
        ("2026-01-01T00:00:00+00:00", MatchPeriodState.Q1, 1),
        ("2026-01-01T00:20:00+00:00", MatchPeriodState.QUARTER_TIME, 2),
        ("2026-01-01T00:30:00+00:00", MatchPeriodState.Q2, 3),
        ("2026-01-01T00:50:00+00:00", MatchPeriodState.HALF_TIME, 4),
        ("2026-01-01T01:10:00+00:00", MatchPeriodState.Q3, 5),
        ("2026-01-01T01:30:00+00:00", MatchPeriodState.THREE_QUARTER_TIME, 6),
        ("2026-01-01T01:50:00+00:00", MatchPeriodState.Q4, 7),
        ("2026-01-01T02:10:00+00:00", MatchPeriodState.FULL_TIME, 8),
    ]
    for collected_at, period_state, tackles in sequence:
        upsert_player_stats(
            conn, _result([("CD_I1", "home", {"tackles": tackles})], collected_at=collected_at),
            match_period_state=period_state,
        )
    markers = [row[0] for row in _checkpoints(conn)]
    assert markers == ["BASELINE", "QT", "HT", "3QT", "FT"]


# --- 14 & 15. FT vs CONCLUDED distinction --------------------------------

def test_ft_does_not_automatically_imply_concluded():
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at="2026-01-01T00:00:00+00:00"))
    upsert_player_stats(
        conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at="2026-01-01T02:00:00+00:00",
                      canonical_match_status="POSTGAME"),
        match_period_state=MatchPeriodState.FULL_TIME,
    )
    markers = [row[0] for row in _checkpoints(conn)]
    assert markers == ["BASELINE", "FT"]
    assert "CONCLUDED" not in markers


def test_later_concluded_snapshot_records_additional_history_and_checkpoint_after_ft():
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at="2026-01-01T00:00:00+00:00"))
    upsert_player_stats(
        conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at="2026-01-01T02:00:00+00:00",
                      canonical_match_status="POSTGAME"),
        match_period_state=MatchPeriodState.FULL_TIME,
    )
    final = _result([("CD_I1", "home", {"tackles": 5})], collected_at="2026-01-01T02:10:00+00:00",
                    canonical_match_status="CONCLUDED")
    assert final.status is PlayerStatsStatus.CONCLUDED
    upsert_player_stats(conn, final)
    markers = {row[0]: row for row in _checkpoints(conn)}
    assert set(markers) == {"BASELINE", "FT", "CONCLUDED"}
    assert markers["FT"][7] == 4  # tackles column at FT
    assert markers["CONCLUDED"][7] == 5  # tackles column at CONCLUDED, differs from FT
    history_fields = [row[0] for row in _history(conn)]
    assert history_fields.count("tackles") == 1  # the postgame->concluded change


# --- 16. Stale/lower-authority data is fully inert ------------------------

def test_stale_lower_authority_data_creates_no_history_or_checkpoint_and_cannot_overwrite():
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at="2026-01-01T00:00:00+00:00"))
    final = _result([("CD_I1", "home", {"tackles": 10})], collected_at="2026-01-01T02:00:00+00:00",
                    canonical_match_status="CONCLUDED")
    upsert_player_stats(conn, final)
    stale = _result([("CD_I1", "home", {"tackles": 99})], collected_at="2026-01-01T02:30:00+00:00",
                    canonical_match_status="LIVE")
    written = upsert_player_stats(conn, stale, match_period_state=MatchPeriodState.Q4)
    assert written == 0
    assert conn.execute(
        "SELECT tackles FROM cfs_player_stats WHERE champion_data_player_id='CD_I1'"
    ).fetchone() == (10,)
    assert "tackles" not in {row[0] for row in _history(conn) if row[2] == 99}
    assert all(row[2] != 99 for row in _history(conn))
    assert all(row[7] != 99 for row in _checkpoints(conn))  # no bogus Q4 checkpoint either


# --- 17. Transaction/error semantics ---------------------------------------

class _FailingHistoryConnProxy:
    """Duck-typed sqlite3.Connection proxy: upsert_player_stats only ever
    calls .execute() on its ``conn`` argument, so this is enough to simulate
    a mid-transaction history-write failure without needing to patch the
    (immutable, C-implemented) sqlite3.Connection type itself."""

    def __init__(self, real: sqlite3.Connection):
        self._real = real

    def execute(self, sql, *args, **kwargs):
        if "INSERT INTO cfs_player_stat_history" in sql:
            raise sqlite3.OperationalError("simulated history-write failure")
        return self._real.execute(sql, *args, **kwargs)


def test_history_write_failure_rolls_back_the_authoritative_update_too():
    """Mirrors the scheduler write-lane's own contract (commit on success,
    rollback on any exception, single shared connection/transaction) --
    see scheduler/write_lane.py's docstring and _execute(). A failure while
    writing history must not leave cfs_player_stats updated without the
    corresponding history row, or vice versa: the caller (write_lane in
    production) rolls back the whole transaction on any exception."""
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at="2026-01-01T00:00:00+00:00"))
    conn.commit()

    proxy = _FailingHistoryConnProxy(conn)
    with pytest.raises(sqlite3.OperationalError):
        upsert_player_stats(
            proxy, _result([("CD_I1", "home", {"tackles": 5})], collected_at="2026-01-01T00:05:00+00:00")
        )
    conn.rollback()
    assert conn.execute(
        "SELECT tackles FROM cfs_player_stats WHERE champion_data_player_id='CD_I1'"
    ).fetchone() == (4,)
    assert _history(conn) == []


# --- Scheduler period-state wiring (no second poller/scheduler) -----------

def test_scheduler_period_state_provider_hook_flows_into_history_without_extra_network_calls():
    """Confirms the Issue #195 integration point end-to-end: a caller-supplied
    MatchPeriodState reaches cfs_player_stat_history/checkpoints purely
    through the existing single upsert_player_stats() write path -- no new
    endpoint, poller, or scheduler is introduced."""
    conn = _db()
    upsert_player_stats(conn, _result([("CD_I1", "home", {"tackles": 4})], collected_at="2026-01-01T00:00:00+00:00"))
    upsert_player_stats(
        conn, _result([("CD_I1", "home", {"tackles": 5})], collected_at="2026-01-01T00:05:00+00:00"),
        match_period_state=MatchPeriodState.HALF_TIME,
    )
    row = conn.execute(
        "SELECT match_period_state FROM cfs_player_stat_history WHERE champion_data_player_id='CD_I1'"
    ).fetchone()
    assert row == ("HT",)
    assert [r[0] for r in _checkpoints(conn)] == ["BASELINE", "HT"]
