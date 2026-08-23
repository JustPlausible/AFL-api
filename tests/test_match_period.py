"""Tests for the normalized production match-period state (Issue #187).

Representative values below are taken directly from the Round 24 live
diagnostic capture (``AFL_DIAGNOSTIC_PROFILES=match_clock``, Issue #148/#175)
for CD_M20260142401 and CD_M20260142402 -- real ``score.matchClock.periods``
observations, not synthesised data -- so the regulation-time mapping is
exercised against evidence rather than invented values. Malformed/unexpected
shapes are deliberately synthetic: no such payload was ever observed live,
and the defensive behaviour under Issue #187 is that they degrade safely
rather than being guessed at.
"""
from __future__ import annotations

import inspect

from afl_json.match_period import (
    MatchPeriodState,
    derive_match_period_state,
    extract_match_clock_periods,
    match_period_state_from_payload,
)
from afl_json.match_status import normalise_match_status


def _payload(*, match_status, score_status, periods):
    return {
        "match": {"status": match_status},
        "score": {"status": score_status, "matchClock": {"periods": periods}},
    }


# --- Module isolation (Issue #187 non-goals) --------------------------------

def test_module_has_no_db_or_scheduler_coupling():
    """This is an internal, informational, pure-function module: it must not
    import anything that could let it influence persistence, scheduling, or
    lifecycle authority."""
    import afl_json.match_period as module
    import_lines = [
        line for line in inspect.getsource(module).splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    for forbidden in ("sqlite3", "db.connection", "scheduler", "write_lane"):
        assert not any(forbidden in line for line in import_lines), import_lines


def test_state_vocabulary_has_no_concluded_member():
    """Lifecycle finality (CONCLUDED) is never part of this vocabulary --
    Q4 completing maps only to FULL_TIME, never to a lifecycle concept."""
    assert "CONCLUDED" not in MatchPeriodState.__members__
    assert "LIVE" not in MatchPeriodState.__members__
    assert "POSTGAME" not in MatchPeriodState.__members__


# --- Regulation-time evidence: CD_M20260142401 (Round 24) ------------------
# poll_sequence numbers and periodSeconds values below match the captured
# match_state_evidence transitions for CD_M20260142401 verbatim.

def test_q1_active_seq2_period_start():
    # seq=2 at=10:10:29 latest_period=1 periodSeconds=2 periodCompleted=False
    periods = [{"periodNumber": 1, "periodSeconds": 2, "periodCompleted": False}]
    assert derive_match_period_state(periods) == MatchPeriodState.Q1


def test_q1_active_seq5_mid_quarter():
    # seq=5 at=10:11:13 latest_period=1 periodSeconds=45 periodCompleted=False
    periods = [{"periodNumber": 1, "periodSeconds": 45, "periodCompleted": False}]
    assert derive_match_period_state(periods) == MatchPeriodState.Q1


def test_q1_completed_becomes_quarter_time_seq133():
    # seq=133 at=10:43:44 flags=latest_period_completed latest_period=1
    # periodSeconds=2002 periodCompleted=True. Commentary evidence for the
    # same match independently logs 'The siren has sounded to end Q1.' at
    # the same timestamp.
    periods = [{"periodNumber": 1, "periodSeconds": 2002, "periodCompleted": True}]
    assert derive_match_period_state(periods) == MatchPeriodState.QUARTER_TIME


def test_q2_active_seq159_new_period():
    # seq=159 at=10:50:14 flags=new_period_appeared latest_period=2
    # periodSeconds=4 periodCompleted=False; period 1 remains in the list,
    # completed.
    periods = [
        {"periodNumber": 1, "periodSeconds": 2002, "periodCompleted": True},
        {"periodNumber": 2, "periodSeconds": 4, "periodCompleted": False},
    ]
    assert derive_match_period_state(periods) == MatchPeriodState.Q2


def test_q2_completed_becomes_half_time_seq285():
    # seq=285 at=11:21:44 flags=latest_period_completed latest_period=2
    # periodSeconds=1881 periodCompleted=True.
    periods = [
        {"periodNumber": 1, "periodSeconds": 2002, "periodCompleted": True},
        {"periodNumber": 2, "periodSeconds": 1881, "periodCompleted": True},
    ]
    assert derive_match_period_state(periods) == MatchPeriodState.HALF_TIME


def test_q3_active_seq364_new_period():
    # seq=364 at=11:41:29 flags=new_period_appeared latest_period=3
    # periodSeconds=2 periodCompleted=False.
    periods = [{"periodNumber": 3, "periodSeconds": 2, "periodCompleted": False}]
    assert derive_match_period_state(periods) == MatchPeriodState.Q3


def test_q3_completed_becomes_three_quarter_time_seq506():
    # seq=506 at=12:16:59 flags=latest_period_completed latest_period=3
    # periodSeconds=2112 periodCompleted=True.
    periods = [{"periodNumber": 3, "periodSeconds": 2112, "periodCompleted": True}]
    assert derive_match_period_state(periods) == MatchPeriodState.THREE_QUARTER_TIME


def test_q4_active_seq529_new_period():
    # seq=529 at=12:22:43 flags=new_period_appeared latest_period=4
    # periodSeconds=4 periodCompleted=False.
    periods = [{"periodNumber": 4, "periodSeconds": 4, "periodCompleted": False}]
    assert derive_match_period_state(periods) == MatchPeriodState.Q4


def test_q4_completed_becomes_full_time_seq668():
    # seq=668 at=12:57:29 flags=latest_period_completed,match_status_changed,
    # score_status_changed latest_period=4 periodSeconds=2080 periodCompleted=True,
    # match_status POSTGAME at the same poll.
    periods = [{"periodNumber": 4, "periodSeconds": 2080, "periodCompleted": True}]
    assert derive_match_period_state(periods) == MatchPeriodState.FULL_TIME


# --- Period clock stalling during active play (must not read as a break) --

def test_period_seconds_stall_and_resume_do_not_change_derived_state():
    # seq=5 periodSeconds=45 (stalled at next poll), seq=6 periodSeconds=62
    # (resumed) -- both LIVE, periodCompleted False throughout. periodSeconds
    # is never consulted, so a stalled clock and a resumed one must derive
    # identically.
    stalled = derive_match_period_state(
        [{"periodNumber": 1, "periodSeconds": 45, "periodCompleted": False}]
    )
    also_45 = derive_match_period_state(
        [{"periodNumber": 1, "periodSeconds": 45, "periodCompleted": False}]
    )
    resumed = derive_match_period_state(
        [{"periodNumber": 1, "periodSeconds": 62, "periodCompleted": False}]
    )
    assert stalled == also_45 == resumed == MatchPeriodState.Q1


# --- Lifecycle independence (Issue #187 core constraint) -------------------

def test_lifecycle_remains_live_during_quarter_time_half_time_three_quarter_time():
    for periods, expected_period_state in (
        ([{"periodNumber": 1, "periodSeconds": 2002, "periodCompleted": True}], MatchPeriodState.QUARTER_TIME),
        ([{"periodNumber": 2, "periodSeconds": 1881, "periodCompleted": True}], MatchPeriodState.HALF_TIME),
        ([{"periodNumber": 3, "periodSeconds": 2112, "periodCompleted": True}], MatchPeriodState.THREE_QUARTER_TIME),
    ):
        payload = _payload(match_status="LIVE", score_status="LIVE", periods=periods)
        assert normalise_match_status(payload["match"]["status"]) == "LIVE"
        assert match_period_state_from_payload(payload) == expected_period_state


def test_lifecycle_authority_independent_of_full_time_period_state_seq669_seq671():
    """Real evidence from CD_M20260142402: Q4 completes (seq=669, 12:27:32)
    one full poll cycle *before* match_status advances to POSTGAME (seq=671,
    12:28:02). The normalized period state must reflect FULL_TIME at seq=669
    without the lifecycle status having moved -- proving the two are
    genuinely independent, not just coincidentally aligned."""
    still_live_payload = _payload(
        match_status="LIVE", score_status="LIVE",
        periods=[{"periodNumber": 4, "periodSeconds": 2267, "periodCompleted": True}],
    )
    assert match_period_state_from_payload(still_live_payload) == MatchPeriodState.FULL_TIME
    assert normalise_match_status(still_live_payload["match"]["status"]) == "LIVE"

    postgame_payload = _payload(
        match_status="POSTGAME", score_status="POSTGAME",
        periods=[{"periodNumber": 4, "periodSeconds": 2267, "periodCompleted": True}],
    )
    assert match_period_state_from_payload(postgame_payload) == MatchPeriodState.FULL_TIME
    assert normalise_match_status(postgame_payload["match"]["status"]) == "POSTGAME"


def test_q4_completion_followed_by_postgame_is_never_interpreted_as_concluded():
    """FULL_TIME plus POSTGAME must not itself produce or imply CONCLUDED --
    that is a distinct, later lifecycle transition this module has no
    opinion on (see afl_json.match_status, the sole lifecycle authority)."""
    payload = _payload(
        match_status="POSTGAME", score_status="POSTGAME",
        periods=[{"periodNumber": 4, "periodSeconds": 2080, "periodCompleted": True}],
    )
    period_state = match_period_state_from_payload(payload)
    lifecycle_status = normalise_match_status(payload["match"]["status"])

    assert period_state == MatchPeriodState.FULL_TIME
    assert lifecycle_status == "POSTGAME"
    assert lifecycle_status != "CONCLUDED"
    # The period-state function itself has no pathway to "CONCLUDED" at all.
    assert all(member != "CONCLUDED" for member in MatchPeriodState)


# --- Safe degradation: missing/empty/malformed input ------------------------

def test_missing_match_clock_degrades_to_unknown():
    payload = {"match": {"status": "LIVE"}, "score": {"status": "LIVE"}}
    assert match_period_state_from_payload(payload) == MatchPeriodState.UNKNOWN


def test_missing_score_degrades_to_unknown():
    payload = {"match": {"status": "LIVE"}}
    assert match_period_state_from_payload(payload) == MatchPeriodState.UNKNOWN


def test_non_object_payload_degrades_to_unknown_without_raising():
    for bad_payload in (None, [], "not a dict", 42):
        assert match_period_state_from_payload(bad_payload) == MatchPeriodState.UNKNOWN


def test_empty_periods_list_degrades_to_unknown():
    payload = _payload(match_status="LIVE", score_status="LIVE", periods=[])
    assert match_period_state_from_payload(payload) == MatchPeriodState.UNKNOWN
    assert derive_match_period_state([]) == MatchPeriodState.UNKNOWN


def test_malformed_period_entries_degrade_to_unknown():
    for periods in (
        None,
        "not a list",
        [{"periodNumber": "1", "periodCompleted": False}],  # string periodNumber
        [{"periodCompleted": False}],  # missing periodNumber
        [{"periodNumber": 1}],  # missing periodCompleted
        [{"periodNumber": 1, "periodCompleted": None}],  # non-bool periodCompleted
        ["not a mapping"],
        [None],
    ):
        assert derive_match_period_state(periods) == MatchPeriodState.UNKNOWN


def test_unexpected_period_number_outside_regulation_range_degrades_to_unknown():
    """Extra time / finals-specific structures were never observed live
    (Issue #187 open edge case) -- an out-of-range periodNumber must not be
    guessed at."""
    for period_number in (0, 5, 6, -1):
        periods = [{"periodNumber": period_number, "periodSeconds": 10, "periodCompleted": False}]
        assert derive_match_period_state(periods) == MatchPeriodState.UNKNOWN


def test_bool_period_number_is_not_treated_as_an_int_period_number():
    """bool is an int subclass in Python; True/False must not be mistaken
    for periodNumber 1/0."""
    periods = [{"periodNumber": True, "periodSeconds": 10, "periodCompleted": False}]
    assert derive_match_period_state(periods) == MatchPeriodState.UNKNOWN


def test_latest_well_formed_period_ignored_alongside_malformed_siblings():
    """A malformed sibling entry must not prevent deriving state from an
    otherwise well-formed latest period."""
    periods = [
        {"periodNumber": 1, "periodSeconds": 2002, "periodCompleted": True},
        {"unexpected": "shape"},
        {"periodNumber": 2, "periodSeconds": 4, "periodCompleted": False},
    ]
    assert derive_match_period_state(periods) == MatchPeriodState.Q2


# --- extract_match_clock_periods: defensive extraction ----------------------

def test_extract_match_clock_periods_returns_none_never_raises():
    for bad_payload in (None, [], "x", {}, {"score": None}, {"score": {"matchClock": None}},
                        {"score": {"matchClock": {"periods": "not a list"}}}):
        assert extract_match_clock_periods(bad_payload) is None


def test_extract_match_clock_periods_reads_nested_under_score_not_top_level():
    payload = {
        "match": {"status": "LIVE"},
        "score": {"status": "LIVE", "matchClock": {"periods": [
            {"periodNumber": 1, "periodSeconds": 42, "periodCompleted": False},
        ]}},
        # A stray top-level matchClock (wrong shape) must be ignored.
        "matchClock": {"periods": [{"periodNumber": 9, "periodSeconds": 9, "periodCompleted": True}]},
    }
    periods = extract_match_clock_periods(payload)
    assert periods == [{"periodNumber": 1, "periodSeconds": 42, "periodCompleted": False}]
