"""Normalized internal match-period (quarter/break) state (Issue #187).

Production follow-up to the Issue #148/#187 diagnostic investigation
(``collection/match_state_evidence.py``, ``diagnostics/profiles/match_clock.py``):
Round 24 live evidence confirmed that CFS ``matchItem``'s
``score.matchClock.periods`` -- specifically each period's ``periodNumber``
and ``periodCompleted`` -- reliably distinguishes active quarters from
quarter/half/three-quarter-time breaks for ordinary regulation-time matches.

This module is a deliberately new, narrowly-scoped production path, **not** a
promotion of the diagnostic collector: it does not touch the diagnostic
evidence table, the diagnostic ``match_item_diagnostic`` endpoint definition,
or anything under ``collection.match_state_evidence``/``scheduler.match_state_capture``.
Those remain independent and keep running for ongoing evidence capture.

Design constraints (Issue #187):

* State is derived primarily from ``periodNumber`` + ``periodCompleted`` of
  the latest well-formed period. ``periodSeconds`` is never consulted --
  Round 24 evidence showed it can stall mid-quarter during ordinary play
  (broadcast/timing artefacts), so a stalled clock must never be interpreted
  as a break.
* Only regulation periods 1-4 are mapped. Extra time, suspended/abandoned
  matches and other unexpected period structures were not observed in any
  Round 24 capture, so they deliberately degrade to :data:`MatchPeriodState.UNKNOWN`
  rather than guessing at a mapping.
* Missing, empty, or malformed ``matchClock``/``periods`` data degrades
  safely to ``UNKNOWN``. This module never raises for bad input and never
  touches match lifecycle state.
* This state is informational only. It is not consulted by scheduler
  finality, match-window leases, recovery, or any other lifecycle-authority
  decision -- ``afl_json.match_status``/``matches.status`` remain the sole
  source of truth for ``UPCOMING``/``LIVE``/``POSTGAME``/``CONCLUDED``. In
  particular, ``Q4`` completing (``FULL_TIME``) does not itself imply
  ``CONCLUDED``; live evidence shows Q4 completion coincides with ``POSTGAME``,
  and ``POSTGAME -> CONCLUDED`` is a separate, later transition this module
  has no opinion on.
* CFS-specific concepts (``matchClock``, ``periodNumber``, ``periodCompleted``,
  ``periodSeconds``) stay isolated here; only the normalized
  :class:`MatchPeriodState` value is meant to cross into other layers, and it
  must never reach the public ``/api/v1`` contract.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Sequence


class MatchPeriodState(str, Enum):
    """Normalized regulation-time match-period vocabulary.

    ``UNKNOWN`` covers everything not confidently identified as ordinary
    regulation play or a break between regulation quarters -- including
    extra time and other structures Issue #187 left unverified.
    """

    Q1 = "Q1"
    QUARTER_TIME = "QT"
    Q2 = "Q2"
    HALF_TIME = "HT"
    Q3 = "Q3"
    THREE_QUARTER_TIME = "3QT"
    Q4 = "Q4"
    FULL_TIME = "FT"
    UNKNOWN = "UNKNOWN"


_ACTIVE_BY_PERIOD_NUMBER: Mapping[int, MatchPeriodState] = {
    1: MatchPeriodState.Q1,
    2: MatchPeriodState.Q2,
    3: MatchPeriodState.Q3,
    4: MatchPeriodState.Q4,
}
_BREAK_BY_PERIOD_NUMBER: Mapping[int, MatchPeriodState] = {
    1: MatchPeriodState.QUARTER_TIME,
    2: MatchPeriodState.HALF_TIME,
    3: MatchPeriodState.THREE_QUARTER_TIME,
    4: MatchPeriodState.FULL_TIME,
}


def _latest_well_formed_period(periods: Sequence[Any]) -> Mapping[str, Any] | None:
    """Highest ``periodNumber`` among well-formed entries, ignoring the rest.

    A period is well-formed enough to consider only if it is a mapping with
    an ``int`` (not ``bool``) ``periodNumber``; anything else is silently
    excluded from consideration rather than raising, consistent with
    degrading unexpected shapes to ``UNKNOWN`` rather than guessing.
    """
    numbered = [
        period for period in periods
        if isinstance(period, Mapping)
        and isinstance(period.get("periodNumber"), int)
        and not isinstance(period.get("periodNumber"), bool)
    ]
    if not numbered:
        return None
    return max(numbered, key=lambda period: period["periodNumber"])


def derive_match_period_state(periods: Any) -> MatchPeriodState:
    """Map ``score.matchClock.periods`` to a normalized :class:`MatchPeriodState`.

    Pure function: based solely on the latest well-formed period's
    ``periodNumber`` and ``periodCompleted``. ``periodSeconds`` is never
    read. Anything that is not a clean regulation 1-4 ``periodNumber`` with a
    boolean ``periodCompleted`` -- missing/empty/non-list input, malformed
    entries, or a period number outside 1-4 -- returns ``UNKNOWN``.
    """
    if not isinstance(periods, Sequence) or isinstance(periods, (str, bytes)):
        return MatchPeriodState.UNKNOWN
    latest = _latest_well_formed_period(periods)
    if latest is None:
        return MatchPeriodState.UNKNOWN
    period_number = latest["periodNumber"]
    completed = latest.get("periodCompleted")
    if completed is not True and completed is not False:
        return MatchPeriodState.UNKNOWN
    mapping = _BREAK_BY_PERIOD_NUMBER if completed else _ACTIVE_BY_PERIOD_NUMBER
    return mapping.get(period_number, MatchPeriodState.UNKNOWN)


def extract_match_clock_periods(payload: Any) -> list[Any] | None:
    """Defensively pull ``score.matchClock.periods`` out of a matchItem-shaped
    payload, returning ``None`` (never raising) when the payload, ``score``,
    ``matchClock`` or ``periods`` is missing or not the expected shape.

    Deliberately independent of ``collection.match_state_evidence.parse_match_item``:
    that diagnostic parser stays isolated for evidence capture, and this
    production path does not promote or depend on it (Issue #187).
    """
    if not isinstance(payload, Mapping):
        return None
    score = payload.get("score")
    if not isinstance(score, Mapping):
        return None
    match_clock = score.get("matchClock")
    if not isinstance(match_clock, Mapping):
        return None
    periods = match_clock.get("periods")
    if not isinstance(periods, list):
        return None
    return periods


def match_period_state_from_payload(payload: Any) -> MatchPeriodState:
    """Convenience: extract periods from a raw matchItem payload and derive
    the normalized state in one step, degrading safely throughout."""
    periods = extract_match_clock_periods(payload)
    if periods is None:
        return MatchPeriodState.UNKNOWN
    return derive_match_period_state(periods)
