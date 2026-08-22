"""Diagnostic-only capture of live CFS ``matchInterchange`` evidence (Issue #193).

This module exists solely to observe and retain how ``homeInterchange[]`` /
``awayInterchange[]`` entries, ``interchangeCount``, ``benchReason``,
``timeOnGround``, ``timeOnBench`` and the team-level ``homeInterchangeCounts``
/ ``awayInterchangeCounts`` totals behave during a live match, so a future
decision about production interchange semantics can be evaluated against real
evidence rather than assumption.

Nothing here infers production semantics -- in particular, an entry appearing
in ``homeInterchange``/``awayInterchange`` is *not* assumed to mean "this
player is currently on the bench" until live evidence supports that reading.
This module only records observed shape and detected changes.

It is deliberately isolated from the maintained, verified AFL JSON contract
registry in ``afl_json.contracts``: the ``matchInterchange`` endpoint and its
semantics are unverified and under active investigation, so its endpoint
definition and all parsing/normalisation live here rather than in the shared
production collector surface. Nothing in this module is consumed by scheduler
decision-making, and player-stat collection is entirely untouched.
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from afl_json.contracts import EndpointDefinition, HttpMethod, SourceSystem

COLLECTOR_VERSION = "match_interchange_evidence_v1"

# Unverified, diagnostic-only endpoint: intentionally not part of afl_json.contracts.ENDPOINTS.
MATCH_INTERCHANGE_ENDPOINT = EndpointDefinition(
    name="match_interchange_diagnostic",
    source=SourceSystem.CFS,
    method=HttpMethod.GET,
    path_template="/matchInterchange/{match_provider_id}",
    requires_auth=True,
    entity_type="match_interchange_diagnostic",
    collection_paths=(),
    identifier_type=None,
    required_path_parameters=("match_provider_id",),
    verified=False,
    unverified_fields=(
        "whether homeInterchange[]/awayInterchange[] entries represent players "
        "currently off the ground, or some other selection (issue #193)",
        "whether interchangeCount increments contemporaneously with real interchange events (issue #193)",
        "benchReason enum values and when/how they change (issue #193)",
        "timeOnGround/timeOnBench update cadence and precision (issue #193)",
    ),
)

TRANSITION_FIRST_OBSERVATION = "first_observation"
TRANSITION_PLAYER_APPEARED_HOME = "player_appeared_home_interchange"
TRANSITION_PLAYER_DISAPPEARED_HOME = "player_disappeared_home_interchange"
TRANSITION_PLAYER_APPEARED_AWAY = "player_appeared_away_interchange"
TRANSITION_PLAYER_DISAPPEARED_AWAY = "player_disappeared_away_interchange"
TRANSITION_PLAYER_INTERCHANGE_COUNT_CHANGED = "player_interchange_count_changed"
TRANSITION_PLAYER_BENCH_REASON_CHANGED = "player_bench_reason_changed"
TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED = "player_time_on_ground_changed"
TRANSITION_PLAYER_TIME_ON_BENCH_CHANGED = "player_time_on_bench_changed"
TRANSITION_HOME_TOTAL_INTERCHANGE_COUNT_CHANGED = "home_total_interchange_count_changed"
TRANSITION_AWAY_TOTAL_INTERCHANGE_COUNT_CHANGED = "away_total_interchange_count_changed"
TRANSITION_HOME_QUARTER_INTERCHANGE_COUNT_CHANGED = "home_quarter_interchange_count_changed"
TRANSITION_AWAY_QUARTER_INTERCHANGE_COUNT_CHANGED = "away_quarter_interchange_count_changed"

# Continuously-ticking per-player counters. Real and worth persisting on every
# change, but noisy enough (they can change on almost every 15s poll for every
# player currently on the ground) that they must never, by themselves, make a
# poll "meaningful" for raw-payload retention or the default operator report --
# see persist_observation() and scripts/report_interchange_evidence.py.
NOISY_TRANSITIONS = frozenset({
    TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED,
    TRANSITION_PLAYER_TIME_ON_BENCH_CHANGED,
})

_QUARTER_COUNT_KEYS = ("interchangeCountQ1", "interchangeCountQ2", "interchangeCountQ3", "interchangeCountQ4")


class MatchInterchangeEvidenceError(ValueError):
    """A matchInterchange payload could not be parsed for diagnostic evidence."""


@dataclass(frozen=True, slots=True)
class MatchInterchangeObservation:
    """One point-in-time diagnostic snapshot of live matchInterchange evidence."""

    observed_at: str
    match_id: int
    match_provider_id: str
    match_status_at_poll: str | None
    # None means the field was missing or malformed in this particular
    # response -- distinct from an empty list/dict, which means the field
    # was present and genuinely empty. This distinction matters: coercing a
    # missing field to [] would make detect_transitions() report every
    # previously-tracked player as having disappeared, and the next normal
    # response would then report them all reappearing -- a false signal that
    # would corrupt the very evidence this profile exists to gather. See
    # detect_transitions(), which skips comparison for a side/field entirely
    # whenever either snapshot has None there.
    home_interchange: list[dict[str, Any]] | None
    away_interchange: list[dict[str, Any]] | None
    home_counts: dict[str, Any] | None
    away_counts: dict[str, Any] | None
    raw: dict[str, Any]


def _entries_by_player_id(entries: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Index interchange entries by Champion Data playerId, never by name.

    Entries missing a usable ``player.playerId`` are excluded from comparison
    (rather than raising) since this is diagnostic-only best-effort evidence.
    """
    result: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        player = entry.get("player")
        player_id = player.get("playerId") if isinstance(player, dict) else None
        if isinstance(player_id, str) and player_id:
            result[player_id] = entry
    return result


def parse_match_interchange(payload: Any, *, match_id: int, match_provider_id: str,
                            observed_at: str, match_status_at_poll: str | None = None,
                            ) -> MatchInterchangeObservation:
    """Parse a raw matchInterchange response into a diagnostic observation.

    Deliberately tolerant of missing/malformed optional structure but
    requires an object payload. A missing or wrongly-typed field is recorded
    as ``None`` (unknown/not observed), never coerced to an empty list/dict
    -- see MatchInterchangeObservation for why that distinction matters.
    """
    if not isinstance(payload, dict):
        raise MatchInterchangeEvidenceError("matchInterchange payload is not an object")
    home = payload.get("homeInterchange")
    away = payload.get("awayInterchange")
    home_counts = payload.get("homeInterchangeCounts")
    away_counts = payload.get("awayInterchangeCounts")
    return MatchInterchangeObservation(
        observed_at=observed_at,
        match_id=match_id,
        match_provider_id=match_provider_id,
        match_status_at_poll=match_status_at_poll,
        home_interchange=deepcopy(home) if isinstance(home, list) else None,
        away_interchange=deepcopy(away) if isinstance(away, list) else None,
        home_counts=deepcopy(home_counts) if isinstance(home_counts, dict) else None,
        away_counts=deepcopy(away_counts) if isinstance(away_counts, dict) else None,
        raw=deepcopy(payload),
    )


def _player_set_transitions(previous: Mapping[str, Any], current: Mapping[str, Any], *,
                            appeared: str, disappeared: str) -> list[str]:
    flags: list[str] = []
    if set(current) - set(previous):
        flags.append(appeared)
    if set(previous) - set(current):
        flags.append(disappeared)
    return flags


def _player_field_transitions(previous: Mapping[str, Any], current: Mapping[str, Any],
                              flags: list[str]) -> None:
    """Compare players present in both snapshots; append flags at most once each."""
    for player_id, cur_entry in current.items():
        prev_entry = previous.get(player_id)
        if prev_entry is None:
            continue
        if (TRANSITION_PLAYER_INTERCHANGE_COUNT_CHANGED not in flags
                and prev_entry.get("interchangeCount") != cur_entry.get("interchangeCount")):
            flags.append(TRANSITION_PLAYER_INTERCHANGE_COUNT_CHANGED)
        if (TRANSITION_PLAYER_BENCH_REASON_CHANGED not in flags
                and prev_entry.get("benchReason") != cur_entry.get("benchReason")):
            flags.append(TRANSITION_PLAYER_BENCH_REASON_CHANGED)
        if (TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED not in flags
                and prev_entry.get("timeOnGround") != cur_entry.get("timeOnGround")):
            flags.append(TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED)
        if (TRANSITION_PLAYER_TIME_ON_BENCH_CHANGED not in flags
                and prev_entry.get("timeOnBench") != cur_entry.get("timeOnBench")):
            flags.append(TRANSITION_PLAYER_TIME_ON_BENCH_CHANGED)


def detect_transitions(previous: MatchInterchangeObservation | None,
                       current: MatchInterchangeObservation) -> list[str]:
    """Pure comparison of two observations; never consulted by scheduling decisions.

    Player comparison always uses Champion Data ``playerId``, never player name.
    """
    if previous is None:
        return [TRANSITION_FIRST_OBSERVATION]

    flags: list[str] = []

    # A side/field is compared only when *both* snapshots actually observed
    # it (neither is None) -- a field missing/malformed in just one poll must
    # never be treated as "everyone disappeared" / "everything changed". See
    # MatchInterchangeObservation and parse_match_interchange().
    if previous.home_interchange is not None and current.home_interchange is not None:
        prev_home = _entries_by_player_id(previous.home_interchange)
        cur_home = _entries_by_player_id(current.home_interchange)
        flags.extend(_player_set_transitions(
            prev_home, cur_home, appeared=TRANSITION_PLAYER_APPEARED_HOME, disappeared=TRANSITION_PLAYER_DISAPPEARED_HOME
        ))
        _player_field_transitions(prev_home, cur_home, flags)

    if previous.away_interchange is not None and current.away_interchange is not None:
        prev_away = _entries_by_player_id(previous.away_interchange)
        cur_away = _entries_by_player_id(current.away_interchange)
        flags.extend(_player_set_transitions(
            prev_away, cur_away, appeared=TRANSITION_PLAYER_APPEARED_AWAY, disappeared=TRANSITION_PLAYER_DISAPPEARED_AWAY
        ))
        _player_field_transitions(prev_away, cur_away, flags)

    if previous.home_counts is not None and current.home_counts is not None:
        if previous.home_counts.get("totalInterchangeCount") != current.home_counts.get("totalInterchangeCount"):
            flags.append(TRANSITION_HOME_TOTAL_INTERCHANGE_COUNT_CHANGED)
        if any(previous.home_counts.get(key) != current.home_counts.get(key) for key in _QUARTER_COUNT_KEYS):
            flags.append(TRANSITION_HOME_QUARTER_INTERCHANGE_COUNT_CHANGED)

    if previous.away_counts is not None and current.away_counts is not None:
        if previous.away_counts.get("totalInterchangeCount") != current.away_counts.get("totalInterchangeCount"):
            flags.append(TRANSITION_AWAY_TOTAL_INTERCHANGE_COUNT_CHANGED)
        if any(previous.away_counts.get(key) != current.away_counts.get(key) for key in _QUARTER_COUNT_KEYS):
            flags.append(TRANSITION_AWAY_QUARTER_INTERCHANGE_COUNT_CHANGED)

    return flags


def load_previous_observation(conn: sqlite3.Connection,
                              match_provider_id: str) -> MatchInterchangeObservation | None:
    """Load the most recently persisted observation for change detection."""
    row = conn.execute(
        """SELECT match_id, match_provider_id, observed_at, match_status_at_poll,
                  home_interchange_json, away_interchange_json, home_counts_json, away_counts_json
           FROM match_interchange_evidence_observations
           WHERE match_provider_id=? ORDER BY poll_sequence DESC LIMIT 1""",
        (match_provider_id,),
    ).fetchone()
    if row is None:
        return None
    return MatchInterchangeObservation(
        observed_at=row["observed_at"],
        match_id=row["match_id"],
        match_provider_id=row["match_provider_id"],
        match_status_at_poll=row["match_status_at_poll"],
        home_interchange=json.loads(row["home_interchange_json"]),
        away_interchange=json.loads(row["away_interchange_json"]),
        home_counts=json.loads(row["home_counts_json"]),
        away_counts=json.loads(row["away_counts_json"]),
        raw={},
    )


def persist_observation(conn: sqlite3.Connection, observation: MatchInterchangeObservation,
                        transitions: Sequence[str]) -> dict[str, Any]:
    """Insert one diagnostic observation.

    Raw payload retention and the ``is_transition`` flag are driven only by
    *meaningful* transitions (everything except the noisy per-player
    timeOnGround/timeOnBench counters) -- see NOISY_TRANSITIONS. The noisy
    flags are still recorded in ``transition_flags_json`` on every poll where
    they occur, just not treated as retention/report-worthy on their own.
    """
    meaningful = [flag for flag in transitions if flag not in NOISY_TRANSITIONS]
    is_transition = bool(meaningful)
    next_sequence = conn.execute(
        "SELECT COALESCE(MAX(poll_sequence), 0) + 1 FROM match_interchange_evidence_observations WHERE match_provider_id=?",
        (observation.match_provider_id,),
    ).fetchone()[0]
    cur = conn.execute(
        """INSERT INTO match_interchange_evidence_observations(
               match_id, match_provider_id, poll_sequence, observed_at, match_status_at_poll,
               home_interchange_json, away_interchange_json, home_counts_json, away_counts_json,
               is_transition, transition_flags_json, raw_match_interchange_json, collector_version
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            observation.match_id, observation.match_provider_id, next_sequence, observation.observed_at,
            observation.match_status_at_poll,
            json.dumps(observation.home_interchange, sort_keys=True),
            json.dumps(observation.away_interchange, sort_keys=True),
            json.dumps(observation.home_counts, sort_keys=True),
            json.dumps(observation.away_counts, sort_keys=True),
            int(is_transition), json.dumps(list(transitions)),
            json.dumps(observation.raw, sort_keys=True) if is_transition else None,
            COLLECTOR_VERSION,
        ),
    )
    return {
        "id": cur.lastrowid,
        "poll_sequence": next_sequence,
        "is_transition": is_transition,
        "transitions": list(transitions),
        "meaningful_transitions": meaningful,
    }


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["home_interchange"] = json.loads(data.pop("home_interchange_json"))
    data["away_interchange"] = json.loads(data.pop("away_interchange_json"))
    data["home_counts"] = json.loads(data.pop("home_counts_json"))
    data["away_counts"] = json.loads(data.pop("away_counts_json"))
    data["transition_flags"] = json.loads(data.pop("transition_flags_json"))
    data["is_transition"] = bool(data["is_transition"])
    raw = data.pop("raw_match_interchange_json")
    data["raw_match_interchange"] = json.loads(raw) if raw else None
    return data


def recently_live_match_provider_ids(conn: sqlite3.Connection, *, now: datetime,
                                     grace_seconds: int) -> list[tuple[int, str]]:
    """Matches whose most recent interchange poll observed the *local*
    ``matches.status`` as LIVE within ``grace_seconds`` of ``now``.

    Unlike match_clock's equivalent function, this cannot key off a
    CFS-reported live/score status, because the matchInterchange payload does
    not appear to carry one (see module docstring: no such field is parsed).
    So this profile snapshots the local ``matches.status`` value at each poll
    (``match_status_at_poll``, read-only -- never written back to ``matches``)
    and uses that snapshot history as the self-terminating signal instead:
    once ``matches.status`` moves away from LIVE, subsequent polls stop
    advancing the "last seen LIVE" timestamp this query reads, so the grace
    window still expires on its own once the local ~5 minute status refresh
    catches up. This is entirely local to this profile's own table -- it does
    not read match_clock's evidence, so the two profiles stay independently
    schedulable.
    """
    if grace_seconds <= 0:
        return []
    cutoff = (now - timedelta(seconds=grace_seconds)).isoformat()
    rows = conn.execute(
        """
        SELECT match_id, match_provider_id, MAX(observed_at) AS last_live_at
        FROM match_interchange_evidence_observations
        WHERE match_status_at_poll='LIVE'
        GROUP BY match_provider_id
        HAVING last_live_at >= ?
        """,
        (cutoff,),
    ).fetchall()
    return [(row["match_id"], row["match_provider_id"]) for row in rows]


def evidence_rows(conn: sqlite3.Connection, *, match_id: int | None = None,
                  match_provider_id: str | None = None,
                  transitions_only: bool = False,
                  limit: int | None = 500) -> list[dict[str, Any]]:
    """Read-only report/inspection query; never used by scheduler decisions."""
    clauses: list[str] = []
    params: list[Any] = []
    if match_id is not None:
        clauses.append("match_id=?")
        params.append(match_id)
    if match_provider_id is not None:
        clauses.append("match_provider_id=?")
        params.append(match_provider_id)
    if transitions_only:
        clauses.append("is_transition=1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM match_interchange_evidence_observations {where} ORDER BY match_provider_id, poll_sequence"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(row) for row in rows]
