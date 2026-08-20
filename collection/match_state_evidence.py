"""Diagnostic-only capture of live CFS ``matchItem`` evidence (Issue #148).

This module exists solely to observe and retain how ``score.matchClock.periods``,
``periodCompleted``, ``periodSeconds``, ``match.status`` and ``score.status``
behave around quarter time, half time, three-quarter time and full time, so a
future decision can be evaluated against real evidence.

Confirmed from live capture on 2026-08-16: ``matchClock`` is nested under
``score``, not a top-level sibling of ``match``/``score`` -- i.e. the real
shape is ``payload["score"]["matchClock"]["periods"]``, not
``payload["matchClock"]["periods"]``.

It is deliberately isolated from the maintained, verified AFL JSON contract
registry in ``afl_json.contracts``: the ``matchItem`` endpoint and its
``matchClock`` semantics are unverified and under active investigation, so
its endpoint definition and all parsing/normalisation live here rather than
in the shared production collector surface. Nothing in this module is
consumed by scheduler decision-making, and no ``QT``/``HT``/``3QT`` style
normalisation happens here.
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from afl_json.contracts import EndpointDefinition, HttpMethod, SourceSystem

COLLECTOR_VERSION = "match_state_evidence_v1"

# Unverified, diagnostic-only endpoint: intentionally not part of afl_json.contracts.ENDPOINTS.
MATCH_ITEM_ENDPOINT = EndpointDefinition(
    name="match_item_diagnostic",
    source=SourceSystem.CFS,
    method=HttpMethod.GET,
    path_template="/matchItem/{match_provider_id}",
    requires_auth=True,
    entity_type="match_item_diagnostic",
    collection_paths=(),
    identifier_type=None,
    required_path_parameters=("match_provider_id",),
    verified=False,
    unverified_fields=(
        "score.matchClock.periods semantics for quarter/half/three-quarter/full time (issue #148)",
    ),
)

TRANSITION_FIRST_OBSERVATION = "first_observation"
TRANSITION_NEW_PERIOD = "new_period_appeared"
TRANSITION_PERIOD_NUMBER_CHANGED = "period_number_changed"
TRANSITION_PERIOD_COMPLETED = "latest_period_completed"
TRANSITION_SECONDS_STALLED = "period_seconds_stalled"
TRANSITION_SECONDS_RESUMED = "period_seconds_resumed"
TRANSITION_MATCH_STATUS_CHANGED = "match_status_changed"
TRANSITION_SCORE_STATUS_CHANGED = "score_status_changed"


class MatchStateEvidenceError(ValueError):
    """A matchItem payload could not be parsed for diagnostic evidence."""


@dataclass(frozen=True, slots=True)
class MatchStateObservation:
    """One point-in-time diagnostic snapshot of live matchItem evidence."""

    observed_at: str
    match_id: int
    match_provider_id: str
    match_status: str | None
    score_status: str | None
    periods: list[dict[str, Any]]
    latest_period_number: int | None
    latest_period_seconds: int | None
    latest_period_completed: bool | None
    raw: dict[str, Any]


def _latest_period(periods: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    numbered = [p for p in periods if isinstance(p, dict) and isinstance(p.get("periodNumber"), int)]
    if numbered:
        return max(numbered, key=lambda p: p["periodNumber"])
    # Only periods that have started are believed to appear (issue #148), so an
    # entry with no usable periodNumber is retained positionally rather than dropped.
    last = periods[-1] if periods else None
    return last if isinstance(last, dict) else None


def parse_match_item(payload: Any, *, match_id: int, match_provider_id: str,
                     observed_at: str) -> MatchStateObservation:
    """Parse a raw matchItem response into a diagnostic observation.

    Deliberately tolerant of missing/malformed optional structure (matchClock
    is not published for every match state) but requires an object payload.
    ``matchClock`` is nested under ``score`` in the real upstream payload
    (``score.matchClock.periods``), confirmed from live capture -- it is not
    a top-level sibling of ``match``/``score``.
    """
    if not isinstance(payload, dict):
        raise MatchStateEvidenceError("matchItem payload is not an object")
    match = payload.get("match")
    match = match if isinstance(match, dict) else {}
    score = payload.get("score")
    score = score if isinstance(score, dict) else {}
    match_clock = score.get("matchClock")
    match_clock = match_clock if isinstance(match_clock, dict) else {}
    periods_raw = match_clock.get("periods")
    periods = deepcopy(periods_raw) if isinstance(periods_raw, list) else []
    latest = _latest_period(periods)
    return MatchStateObservation(
        observed_at=observed_at,
        match_id=match_id,
        match_provider_id=match_provider_id,
        match_status=match.get("status"),
        score_status=score.get("status"),
        periods=periods,
        latest_period_number=latest.get("periodNumber") if latest else None,
        latest_period_seconds=latest.get("periodSeconds") if latest else None,
        latest_period_completed=latest.get("periodCompleted") if latest else None,
        raw=deepcopy(payload),
    )


def detect_transitions(previous: MatchStateObservation | None, previous_flags: Sequence[str],
                       current: MatchStateObservation) -> list[str]:
    """Pure comparison of two observations; never consulted by scheduling decisions."""
    if previous is None:
        return [TRANSITION_FIRST_OBSERVATION]

    flags: list[str] = []
    if len(current.periods) > len(previous.periods):
        flags.append(TRANSITION_NEW_PERIOD)
    same_period = previous.latest_period_number == current.latest_period_number
    if not same_period:
        flags.append(TRANSITION_PERIOD_NUMBER_CHANGED)
    if (same_period and previous.latest_period_completed is not True
            and current.latest_period_completed is True):
        flags.append(TRANSITION_PERIOD_COMPLETED)
    if (same_period and previous.latest_period_seconds is not None
            and current.latest_period_seconds is not None):
        delta = current.latest_period_seconds - previous.latest_period_seconds
        if delta == 0 and current.latest_period_completed is not True:
            flags.append(TRANSITION_SECONDS_STALLED)
        elif delta > 0 and TRANSITION_SECONDS_STALLED in previous_flags:
            flags.append(TRANSITION_SECONDS_RESUMED)
    if previous.match_status != current.match_status:
        flags.append(TRANSITION_MATCH_STATUS_CHANGED)
    if previous.score_status != current.score_status:
        flags.append(TRANSITION_SCORE_STATUS_CHANGED)
    return flags


def load_previous_observation(conn: sqlite3.Connection,
                              match_provider_id: str) -> tuple[MatchStateObservation | None, list[str]]:
    """Load the most recently persisted observation for change detection."""
    row = conn.execute(
        """SELECT match_id, match_provider_id, observed_at, match_status, score_status,
                  periods_json, latest_period_number, latest_period_seconds,
                  latest_period_completed, transition_flags_json
           FROM match_state_evidence_observations
           WHERE match_provider_id=? ORDER BY poll_sequence DESC LIMIT 1""",
        (match_provider_id,),
    ).fetchone()
    if row is None:
        return None, []
    observation = MatchStateObservation(
        observed_at=row["observed_at"],
        match_id=row["match_id"],
        match_provider_id=row["match_provider_id"],
        match_status=row["match_status"],
        score_status=row["score_status"],
        periods=json.loads(row["periods_json"]),
        latest_period_number=row["latest_period_number"],
        latest_period_seconds=row["latest_period_seconds"],
        latest_period_completed=(None if row["latest_period_completed"] is None
                                  else bool(row["latest_period_completed"])),
        raw={},
    )
    return observation, json.loads(row["transition_flags_json"])


def persist_observation(conn: sqlite3.Connection, observation: MatchStateObservation,
                        transitions: Sequence[str]) -> dict[str, Any]:
    """Insert one diagnostic observation; raw payload retained only when notable."""
    is_transition = bool(transitions)
    next_sequence = conn.execute(
        "SELECT COALESCE(MAX(poll_sequence), 0) + 1 FROM match_state_evidence_observations WHERE match_provider_id=?",
        (observation.match_provider_id,),
    ).fetchone()[0]
    cur = conn.execute(
        """INSERT INTO match_state_evidence_observations(
               match_id, match_provider_id, poll_sequence, observed_at, match_status, score_status,
               periods_json, latest_period_number, latest_period_seconds, latest_period_completed,
               is_transition, transition_flags_json, raw_match_item_json, collector_version
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            observation.match_id, observation.match_provider_id, next_sequence, observation.observed_at,
            observation.match_status, observation.score_status,
            json.dumps(observation.periods, sort_keys=True),
            observation.latest_period_number, observation.latest_period_seconds,
            None if observation.latest_period_completed is None else int(bool(observation.latest_period_completed)),
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
    }


def reparse_stored_raw_observations(conn: sqlite3.Connection, *, match_id: int | None = None,
                                    match_provider_id: str | None = None,
                                    dry_run: bool = False) -> list[dict[str, Any]]:
    """Diagnostic-only backfill: re-extract period fields from already-stored
    ``raw_match_item_json`` using the current ``parse_match_item``, for rows
    where the raw payload was retained (first-observation and
    detected-transition rows).

    This never invents data for rows without a retained raw payload -- those
    are left completely unchanged -- and never touches ``poll_sequence``,
    ``observed_at``, ``match_status``, ``score_status``, ``is_transition`` or
    ``transition_flags_json``. It only overwrites ``periods_json``,
    ``latest_period_number``, ``latest_period_seconds`` and
    ``latest_period_completed`` when a corrected re-extraction differs from
    what is currently stored. Idempotent: a second run reports zero changes.
    Returns one summary dict per row considered, with ``changed``/``before``/
    ``after`` keys so a caller can report exactly what was recovered.
    """
    clauses = ["raw_match_item_json IS NOT NULL"]
    params: list[Any] = []
    if match_id is not None:
        clauses.append("match_id=?")
        params.append(match_id)
    if match_provider_id is not None:
        clauses.append("match_provider_id=?")
        params.append(match_provider_id)
    rows = conn.execute(
        f"""SELECT id, match_id, match_provider_id, observed_at, raw_match_item_json,
                   periods_json, latest_period_number, latest_period_seconds, latest_period_completed
            FROM match_state_evidence_observations WHERE {' AND '.join(clauses)} ORDER BY id""",
        params,
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        raw = json.loads(row["raw_match_item_json"])
        reparsed = parse_match_item(
            raw, match_id=row["match_id"], match_provider_id=row["match_provider_id"],
            observed_at=row["observed_at"],
        )
        before = {
            "periods": json.loads(row["periods_json"]),
            "latest_period_number": row["latest_period_number"],
            "latest_period_seconds": row["latest_period_seconds"],
            "latest_period_completed": (None if row["latest_period_completed"] is None
                                        else bool(row["latest_period_completed"])),
        }
        after = {
            "periods": reparsed.periods,
            "latest_period_number": reparsed.latest_period_number,
            "latest_period_seconds": reparsed.latest_period_seconds,
            "latest_period_completed": reparsed.latest_period_completed,
        }
        changed = before != after
        if changed and not dry_run:
            conn.execute(
                """UPDATE match_state_evidence_observations
                   SET periods_json=?, latest_period_number=?, latest_period_seconds=?, latest_period_completed=?
                   WHERE id=?""",
                (
                    json.dumps(reparsed.periods, sort_keys=True),
                    reparsed.latest_period_number, reparsed.latest_period_seconds,
                    None if reparsed.latest_period_completed is None else int(bool(reparsed.latest_period_completed)),
                    row["id"],
                ),
            )
        results.append({
            "id": row["id"], "match_id": row["match_id"], "match_provider_id": row["match_provider_id"],
            "changed": changed, "before": before, "after": after,
        })
    return results


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["periods"] = json.loads(data.pop("periods_json"))
    data["transition_flags"] = json.loads(data.pop("transition_flags_json"))
    data["is_transition"] = bool(data["is_transition"])
    if data["latest_period_completed"] is not None:
        data["latest_period_completed"] = bool(data["latest_period_completed"])
    raw = data.pop("raw_match_item_json")
    data["raw_match_item"] = json.loads(raw) if raw else None
    return data


def recently_live_match_provider_ids(conn: sqlite3.Connection, *, now: datetime,
                                     grace_seconds: int) -> list[tuple[int, str]]:
    """Matches whose *captured evidence* last reported CFS-side LIVE within
    ``grace_seconds`` of ``now``, regardless of the current local
    ``matches.status`` value.

    This exists so a bounded post-LIVE observation window (see
    ``scheduler.match_state_capture``) can keep polling a match for a short
    while after the local status-refresh cadence (independently scheduled,
    every ~5 minutes) moves ``matches.status`` away from ``LIVE`` -- for
    example while the Q4 ``periodCompleted`` transition is still settling.
    It is driven entirely by this diagnostic module's own durable evidence
    table, never by production scheduling state, and self-terminates once no
    further LIVE observation lands within the grace window.
    """
    if grace_seconds <= 0:
        return []
    cutoff = (now - timedelta(seconds=grace_seconds)).isoformat()
    rows = conn.execute(
        """
        SELECT match_id, match_provider_id, MAX(observed_at) AS last_live_at
        FROM match_state_evidence_observations
        WHERE match_status='LIVE' OR score_status='LIVE'
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
    sql = f"SELECT * FROM match_state_evidence_observations {where} ORDER BY match_provider_id, poll_sequence"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(row) for row in rows]
