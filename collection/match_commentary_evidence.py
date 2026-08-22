"""Diagnostic-only capture of live CFS ``commentaryFeed`` evidence (Issue #196).

This module exists solely to observe and retain how the CFS
``commentaryFeed/{matchProviderId}`` endpoint behaves during a live match, so
a future decision about production commentary/score-event/timeline semantics
can be evaluated against real evidence rather than assumption. It runs
alongside (and independently of) the ``match_clock`` (Issue #148) and
``interchange`` (Issue #193) diagnostic profiles, sharing only the generic
diagnostics framework's registration/scheduling lifecycle -- see
``diagnostics/profiles/commentary.py`` and
``scheduler/match_commentary_capture.py``.

Nothing here makes commentary authoritative for match state, match finality,
scheduler behaviour, or any consumer API. In particular, the explicit siren
text quoted in Issue #196 (e.g. ``"The siren has sounded to end Q2."``) is
treated as corroborating evidence only, never as a production state signal.

## Accumulated-feed deduplication

The observed endpoint returns the *entire* commentary history for a match on
every request, newest-first, rather than only events published since the
previous poll. Persisting that whole array on every ~15s poll would both
duplicate the same evidence over and over and make it hard to see what is
actually new. Instead, this module treats ``commentary_evidence_events`` as
an append-only, deduplicated store: an event already captured (by
fingerprint -- see below) is never re-inserted or re-written; only genuinely
new fingerprints are persisted, and existing rows are only ever touched to
record that they were seen again (``last_seen_at``/``last_seen_poll_sequence``),
never to change any previously captured field.

## Fingerprint / deduplication key

The endpoint does not supply any per-event identifier (no ``id``/``eventId``
field has ever been observed). Per Issue #196, this module therefore builds a
conservative composite fingerprint -- a SHA-256 digest of the exact tuple
``(periodNumber, periodSeconds, playerId, teamId, scoreEvent, comment)`` --
as the sole deduplication key. This has a documented limitation: two
genuinely distinct events that happen to share every one of those fields
verbatim (same quarter, same second, same player/team attribution -- or lack
of it -- same scoreEvent flag, and byte-identical comment text) would collide
and be treated as a single event. This is judged an acceptable, conservative
trade-off in the absence of any real identifier; it is far more likely for
two *unrelated* narrative comments to land on the same ``periodSeconds`` with
different text (very common in the observed feed -- see the reduced fixture)
than for two truly distinct events to be verbatim-identical in every field.

## Detecting edited/changed events

Because an edit changes the comment text, an edited event necessarily gets a
*new* fingerprint and is inserted as a new row -- the prior row is never
overwritten (Issue #196 is explicit that this must never happen silently).
To surface likely edits for report review, a *narrower* "slot key" --
``(periodNumber, periodSeconds, playerId, teamId, scoreEvent)`` without the
comment text -- is used to link a newly observed event to a plausible earlier
version of itself, but **only when the event carries a specific ``playerId``**.
Generic narrative commentary (no ``playerId``) is deliberately excluded from
this linkage: the fixture evidence shows many unrelated narrative comments
legitimately sharing the same quarter/second/no-player/no-team slot, so
linking those by slot key alone would produce constant false "edit" signals.
A player-attributed slot (typically a scoring or interchange/injury comment)
is a much more precise anchor, so a new event sharing that slot with a prior
one is treated as a *possible* edit and recorded via
``possible_edit_of_event_id`` -- surfaced, never merged or hidden.

Event *removal* and *reordering* are not proactively detected here (the
accumulated-feed shape makes "removed" ambiguous to infer confidently from
one profile alone); the append-only, timestamped store retains exactly what
is needed for a human to investigate those research questions (#9 in Issue
#196) from captured evidence directly.

## Persistence shape

Two dedicated tables (migration ``0018``), kept separate from
``match_state_evidence_observations`` and
``match_interchange_evidence_observations`` per the diagnostics framework's
one-table(s)-per-profile stance (see ``docs/diagnostics_framework.md``):

* ``commentary_evidence_polls`` -- one row per poll *attempt* (whether the
  request succeeded or failed), preserving ``poll_sequence`` continuity
  across restarts and endpoint availability/outcome transitions for
  reporting. Unlike ``match_clock``/``interchange``, a poll row is written
  even for non-success outcomes (Issue #196 explicitly asks for endpoint
  availability/failure transitions to be inspectable); raw payload retention
  and the ``is_transition`` flag still stay conservative (see
  ``persist_observation``/``persist_poll_outcome``).
* ``commentary_evidence_events`` -- one row per *unique* discovered
  commentary event (deduplicated by fingerprint), carrying the source facts
  Issue #196 asks to preserve: match identity, first/last observation
  timing, feed ``lastUpdated``, ``periodNumber``, ``periodSeconds``, the
  original ``comment``, ``playerId``, ``teamId``, ``scoreEvent``, the
  fingerprint/slot key, conservative ``category``, any detected
  ``possible_edit_of_event_id`` link, and a selectively retained raw event
  payload (see below).

Champion Data ``playerId``/``teamId`` values are stored verbatim as source
facts -- this module never resolves or replaces them with player/team names.

## Raw payload retention

Only the *new* event's own (small) raw JSON object is retained -- never the
full accumulated feed array -- and only once, at first observation. The poll
row additionally retains the full raw feed payload only on the very first
poll for a match, or on a poll where at least one new event was discovered,
or where the ``commentaryEvent`` field was missing/malformed (to aid
debugging); an ordinary "nothing new" poll retains no raw payload at all.

## Conservative categorisation

``categorise_event`` derives an optional, best-effort ``category`` label
(``quarter_start`` / ``quarter_end`` / ``score_event``) purely for report
readability. Per Issue #196, **no part of this module's correctness --
fingerprinting, deduplication, persistence, or restart behaviour -- depends
on this label or on parsing free-text English commentary**: ``scoreEvent``
is read directly from the structured boolean field, and the quarter
start/end labels use narrow, explicitly anchored patterns matched only in
addition to the structural ``periodSeconds`` signal available for quarter
starts. An unrecognised or reworded comment is simply left uncategorised
(``category=None``), never mis-parsed or treated as a failure.

It is deliberately isolated from the maintained, verified AFL JSON contract
registry in ``afl_json.contracts``: ``commentaryFeed`` is unverified and
under active investigation, so its endpoint definition and all parsing
live here rather than in the shared production collector surface.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from afl_json.contracts import EndpointDefinition, HttpMethod, SourceSystem

COLLECTOR_VERSION = "match_commentary_evidence_v1"

# Unverified, diagnostic-only endpoint: intentionally not part of afl_json.contracts.ENDPOINTS.
MATCH_COMMENTARY_ENDPOINT = EndpointDefinition(
    name="match_commentary_diagnostic",
    source=SourceSystem.CFS,
    method=HttpMethod.GET,
    path_template="/commentaryFeed/{match_provider_id}",
    requires_auth=True,
    entity_type="match_commentary_diagnostic",
    collection_paths=(),
    identifier_type=None,
    required_path_parameters=("match_provider_id",),
    verified=False,
    unverified_fields=(
        "whether commentaryEvent[] is a complete, stable accumulated history, or can be "
        "edited/removed/reordered later (issue #196)",
        "whether quarter-start events are consistently emitted at periodSeconds=0 (issue #196)",
        "whether quarter-end siren events are consistently emitted at the final matchItem "
        "periodSeconds value for that period (issue #196)",
        "whether scoreEvent/playerId/teamId are consistently populated for all score events (issue #196)",
        "when commentaryFeed first becomes available relative to the scheduled bounce, and its "
        "behaviour around POSTGAME/CONCLUDED (issue #196)",
    ),
)

CATEGORY_QUARTER_START = "quarter_start"
CATEGORY_QUARTER_END = "quarter_end"
CATEGORY_SCORE_EVENT = "score_event"

TRANSITION_FIRST_POLL = "first_poll"
TRANSITION_NEW_EVENTS = "new_events"
TRANSITION_POSSIBLE_EVENT_EDIT = "possible_event_edit"
TRANSITION_COMMENTARY_MISSING_OR_MALFORMED = "commentary_field_missing_or_malformed"

OUTCOME_SUCCESS = "success"
OUTCOME_MALFORMED_PAYLOAD = "malformed_payload"

# Narrow, explicitly anchored patterns used only to derive a best-effort report
# label -- see module docstring "Conservative categorisation". Never consulted
# for fingerprinting, deduplication, or persistence correctness.
_QUARTER_START_RE = re.compile(r"^Q\d+\s+is\s+now\s+underway\.?$", re.IGNORECASE)
_QUARTER_END_RE = re.compile(r"^The\s+siren\s+has\s+sounded\s+to\s+end\s+Q\d+\.?$", re.IGNORECASE)


class MatchCommentaryEvidenceError(ValueError):
    """A commentaryFeed payload could not be parsed for diagnostic evidence."""


def categorise_event(*, comment: str | None, period_seconds: int | None, score_event: bool | None) -> str | None:
    """Best-effort, non-authoritative report label. See module docstring."""
    if score_event is True:
        return CATEGORY_SCORE_EVENT
    if not comment:
        return None
    text = comment.strip()
    if _QUARTER_END_RE.match(text):
        return CATEGORY_QUARTER_END
    # Quarter-start labelling additionally requires the structural
    # periodSeconds=0 signal, not just wording, to stay conservative.
    if period_seconds == 0 and _QUARTER_START_RE.match(text):
        return CATEGORY_QUARTER_START
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _coerce_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _coerce_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _fingerprint(period_number: int | None, period_seconds: int | None, player_id: str | None,
                 team_id: str | None, score_event: bool | None, comment: str | None) -> str:
    canonical = json.dumps(
        {
            "period_number": period_number, "period_seconds": period_seconds, "player_id": player_id,
            "team_id": team_id, "score_event": score_event, "comment": comment,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _slot_key(period_number: int | None, period_seconds: int | None, player_id: str | None,
             team_id: str | None, score_event: bool | None) -> str:
    return json.dumps([period_number, period_seconds, player_id, team_id, score_event], sort_keys=True)


@dataclass(frozen=True, slots=True)
class CommentaryEvent:
    """One parsed commentaryFeed entry, with its dedup fingerprint precomputed."""

    period_number: int | None
    period_seconds: int | None
    comment: str | None
    player_id: str | None
    team_id: str | None
    score_event: bool | None
    fingerprint: str
    slot_key: str
    category: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MatchCommentaryObservation:
    """One point-in-time diagnostic snapshot of the (accumulated) commentaryFeed."""

    observed_at: str
    match_id: int
    match_provider_id: str
    match_status_at_poll: str | None
    feed_last_updated: str | None
    # None means commentaryEvent was missing or not a list in this response --
    # distinct from an empty list, which means the field was present and the
    # feed genuinely has no events yet (e.g. very early pre-match).
    events: list[CommentaryEvent] | None
    raw: dict[str, Any]


def _parse_one_event(raw: Any) -> CommentaryEvent | None:
    if not isinstance(raw, dict):
        return None
    period_number = _coerce_int(raw.get("periodNumber"))
    period_seconds = _coerce_int(raw.get("periodSeconds"))
    comment = _coerce_str(raw.get("comment"))
    player_id = _coerce_str(raw.get("playerId"))
    team_id = _coerce_str(raw.get("teamId"))
    score_event = _coerce_bool(raw.get("scoreEvent"))
    fingerprint = _fingerprint(period_number, period_seconds, player_id, team_id, score_event, comment)
    slot_key = _slot_key(period_number, period_seconds, player_id, team_id, score_event)
    category = categorise_event(comment=comment, period_seconds=period_seconds, score_event=score_event)
    return CommentaryEvent(
        period_number=period_number, period_seconds=period_seconds, comment=comment,
        player_id=player_id, team_id=team_id, score_event=score_event,
        fingerprint=fingerprint, slot_key=slot_key, category=category, raw=deepcopy(raw),
    )


def parse_match_commentary(payload: Any, *, match_id: int, match_provider_id: str, observed_at: str,
                           match_status_at_poll: str | None = None) -> MatchCommentaryObservation:
    """Parse a raw commentaryFeed response into a diagnostic observation.

    Requires an object payload (raises ``MatchCommentaryEvidenceError``
    otherwise) but is deliberately tolerant of a missing/malformed
    ``commentaryEvent`` field -- recorded as ``events=None`` (unknown/not
    observed this poll), never coerced to an empty list. An individual event
    entry that is not an object is skipped rather than aborting the whole
    parse.
    """
    if not isinstance(payload, dict):
        raise MatchCommentaryEvidenceError("commentaryFeed payload is not an object")
    feed_last_updated = _coerce_str(payload.get("lastUpdated"))
    raw_events = payload.get("commentaryEvent")
    events: list[CommentaryEvent] | None
    if isinstance(raw_events, list):
        events = []
        for raw_event in raw_events:
            parsed = _parse_one_event(raw_event)
            if parsed is not None:
                events.append(parsed)
    else:
        events = None
    return MatchCommentaryObservation(
        observed_at=observed_at, match_id=match_id, match_provider_id=match_provider_id,
        match_status_at_poll=match_status_at_poll, feed_last_updated=feed_last_updated,
        events=events, raw=deepcopy(payload),
    )


def _next_poll_sequence(conn: sqlite3.Connection, match_provider_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(poll_sequence), 0) + 1 FROM commentary_evidence_polls WHERE match_provider_id=?",
        (match_provider_id,),
    ).fetchone()[0]


def persist_poll_outcome(conn: sqlite3.Connection, *, match_id: int, match_provider_id: str, observed_at: str,
                         match_status_at_poll: str | None, outcome: str) -> dict[str, Any]:
    """Record one non-success (or malformed-payload) poll attempt.

    Unlike ``match_clock``/``interchange``, commentary persists a row for
    every outcome, not only successes -- Issue #196 explicitly asks that
    endpoint availability/failure transitions be easy to inspect in the
    report, which is impossible if failures are never recorded. ``is_transition``
    is still true only when the outcome differs from the immediately
    preceding poll for this match, so an extended stretch of the same
    failure (e.g. "not yet published" ahead of a bounce) does not itself
    make every single poll report-worthy.
    """
    next_sequence = _next_poll_sequence(conn, match_provider_id)
    previous = conn.execute(
        "SELECT outcome FROM commentary_evidence_polls WHERE match_provider_id=? ORDER BY poll_sequence DESC LIMIT 1",
        (match_provider_id,),
    ).fetchone()
    is_transition = previous is None or previous[0] != outcome
    flags = [f"outcome_{outcome}"] if is_transition else []
    cur = conn.execute(
        """INSERT INTO commentary_evidence_polls(
               match_id, match_provider_id, poll_sequence, observed_at, match_status_at_poll, outcome,
               feed_last_updated, event_count_in_feed, new_event_count, is_transition, transition_flags_json,
               raw_commentary_json, collector_version
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            match_id, match_provider_id, next_sequence, observed_at, match_status_at_poll, outcome,
            None, None, 0, int(is_transition), json.dumps(flags), None, COLLECTOR_VERSION,
        ),
    )
    return {
        "id": cur.lastrowid, "poll_sequence": next_sequence, "outcome": outcome,
        "is_transition": is_transition, "transitions": flags, "new_event_count": 0,
        "new_events": [], "possible_edits": [], "event_count_in_feed": None,
    }


def _load_existing_events(conn: sqlite3.Connection, match_provider_id: str,
                          ) -> tuple[dict[str, int], dict[str, list[tuple[int, int]]]]:
    rows = conn.execute(
        "SELECT id, event_fingerprint, slot_key, first_observed_poll_sequence "
        "FROM commentary_evidence_events WHERE match_provider_id=?",
        (match_provider_id,),
    ).fetchall()
    fingerprint_to_id: dict[str, int] = {}
    slot_to_candidates: dict[str, list[tuple[int, int]]] = {}
    for row in rows:
        fingerprint_to_id[row["event_fingerprint"]] = row["id"]
        slot_to_candidates.setdefault(row["slot_key"], []).append(
            (row["id"], row["first_observed_poll_sequence"])
        )
    return fingerprint_to_id, slot_to_candidates


def persist_observation(conn: sqlite3.Connection, observation: MatchCommentaryObservation) -> dict[str, Any]:
    """Insert one successfully parsed poll, deduplicating events by fingerprint.

    Never overwrites a previously captured event's content: an already-known
    fingerprint only has its ``last_seen_*`` bookkeeping columns touched. A
    genuinely new fingerprint is always inserted as a new row, even if it
    shares a "slot" (period/second/player/team/scoreEvent) with an existing
    row -- in that player-attributed case it is additionally linked via
    ``possible_edit_of_event_id`` for report visibility. See module docstring
    for the full fingerprint/slot-key/raw-retention policy.
    """
    next_sequence = _next_poll_sequence(conn, observation.match_provider_id)
    flags: list[str] = []
    if next_sequence == 1:
        flags.append(TRANSITION_FIRST_POLL)

    new_events: list[dict[str, Any]] = []
    possible_edits: list[dict[str, Any]] = []
    event_count_in_feed: int | None

    if observation.events is None:
        flags.append(TRANSITION_COMMENTARY_MISSING_OR_MALFORMED)
        event_count_in_feed = None
    else:
        event_count_in_feed = len(observation.events)
        fingerprint_to_id, slot_to_candidates = _load_existing_events(conn, observation.match_provider_id)
        for event in observation.events:
            existing_id = fingerprint_to_id.get(event.fingerprint)
            if existing_id is not None:
                conn.execute(
                    "UPDATE commentary_evidence_events SET last_seen_at=?, last_seen_poll_sequence=?, "
                    "last_seen_feed_last_updated=? WHERE id=?",
                    (observation.observed_at, next_sequence, observation.feed_last_updated, existing_id),
                )
                continue

            possible_edit_of_id: int | None = None
            if event.player_id is not None:
                candidates = slot_to_candidates.get(event.slot_key)
                if candidates:
                    possible_edit_of_id = max(candidates, key=lambda candidate: candidate[1])[0]

            cur = conn.execute(
                """INSERT INTO commentary_evidence_events(
                       match_id, match_provider_id, event_fingerprint, slot_key, period_number, period_seconds,
                       comment, player_id, team_id, score_event, category, first_observed_at,
                       first_observed_poll_sequence, last_seen_at, last_seen_poll_sequence,
                       first_seen_feed_last_updated, last_seen_feed_last_updated, possible_edit_of_event_id,
                       raw_event_json, collector_version
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    observation.match_id, observation.match_provider_id, event.fingerprint, event.slot_key,
                    event.period_number, event.period_seconds, event.comment, event.player_id, event.team_id,
                    None if event.score_event is None else int(event.score_event), event.category,
                    observation.observed_at, next_sequence, observation.observed_at, next_sequence,
                    observation.feed_last_updated, observation.feed_last_updated, possible_edit_of_id,
                    json.dumps(event.raw, sort_keys=True), COLLECTOR_VERSION,
                ),
            )
            new_event_id = cur.lastrowid
            new_events.append({
                "id": new_event_id, "fingerprint": event.fingerprint, "category": event.category,
                "possible_edit_of_event_id": possible_edit_of_id,
            })
            fingerprint_to_id[event.fingerprint] = new_event_id
            slot_to_candidates.setdefault(event.slot_key, []).append((new_event_id, next_sequence))
            if possible_edit_of_id is not None:
                possible_edits.append({"new_event_id": new_event_id, "possible_edit_of_event_id": possible_edit_of_id})

    if new_events:
        flags.append(TRANSITION_NEW_EVENTS)
    if possible_edits:
        flags.append(TRANSITION_POSSIBLE_EVENT_EDIT)

    is_transition = bool(flags)
    # Raw feed retention: only the first poll, a poll with at least one new
    # event, or a malformed/missing-array poll retains the full raw payload
    # -- never an ordinary "nothing new" poll. Each new event's own raw
    # object is separately retained above, once, regardless of this flag.
    retain_raw = next_sequence == 1 or bool(new_events) or observation.events is None
    cur = conn.execute(
        """INSERT INTO commentary_evidence_polls(
               match_id, match_provider_id, poll_sequence, observed_at, match_status_at_poll, outcome,
               feed_last_updated, event_count_in_feed, new_event_count, is_transition, transition_flags_json,
               raw_commentary_json, collector_version
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            observation.match_id, observation.match_provider_id, next_sequence, observation.observed_at,
            observation.match_status_at_poll, OUTCOME_SUCCESS, observation.feed_last_updated,
            event_count_in_feed, len(new_events), int(is_transition), json.dumps(flags),
            json.dumps(observation.raw, sort_keys=True) if retain_raw else None, COLLECTOR_VERSION,
        ),
    )
    return {
        "id": cur.lastrowid, "poll_sequence": next_sequence, "outcome": OUTCOME_SUCCESS,
        "is_transition": is_transition, "transitions": flags, "new_event_count": len(new_events),
        "new_events": new_events, "possible_edits": possible_edits, "event_count_in_feed": event_count_in_feed,
    }


def recently_live_match_provider_ids(conn: sqlite3.Connection, *, now: datetime,
                                     grace_seconds: int) -> list[tuple[int, str]]:
    """Matches whose most recent commentary poll observed the *local*
    ``matches.status`` as LIVE within ``grace_seconds`` of ``now``.

    Mirrors ``collection.match_interchange_evidence.recently_live_match_provider_ids``
    (the commentaryFeed payload does not appear to carry a live/score status
    field either) and is entirely self-contained to this profile's own table.
    """
    if grace_seconds <= 0:
        return []
    cutoff = (now - timedelta(seconds=grace_seconds)).isoformat()
    rows = conn.execute(
        """
        SELECT match_id, match_provider_id, MAX(observed_at) AS last_live_at
        FROM commentary_evidence_polls
        WHERE match_status_at_poll='LIVE'
        GROUP BY match_provider_id
        HAVING last_live_at >= ?
        """,
        (cutoff,),
    ).fetchall()
    return [(row["match_id"], row["match_provider_id"]) for row in rows]


def _poll_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["transition_flags"] = json.loads(data.pop("transition_flags_json"))
    data["is_transition"] = bool(data["is_transition"])
    raw = data.pop("raw_commentary_json")
    data["raw_commentary"] = json.loads(raw) if raw else None
    return data


def _event_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["score_event"] = None if data["score_event"] is None else bool(data["score_event"])
    raw = data.pop("raw_event_json")
    data["raw_event"] = json.loads(raw) if raw else None
    return data


def poll_rows(conn: sqlite3.Connection, *, match_id: int | None = None, match_provider_id: str | None = None,
             transitions_only: bool = False, limit: int | None = 500) -> list[dict[str, Any]]:
    """Read-only report/inspection query over poll attempts; never used by scheduler decisions."""
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
    sql = f"SELECT * FROM commentary_evidence_polls {where} ORDER BY match_provider_id, poll_sequence"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_poll_row_to_dict(row) for row in rows]


def event_rows(conn: sqlite3.Connection, *, match_id: int | None = None, match_provider_id: str | None = None,
              category: str | None = None, limit: int | None = 500) -> list[dict[str, Any]]:
    """Read-only report/inspection query over deduplicated events; never used by scheduler decisions."""
    clauses: list[str] = []
    params: list[Any] = []
    if match_id is not None:
        clauses.append("match_id=?")
        params.append(match_id)
    if match_provider_id is not None:
        clauses.append("match_provider_id=?")
        params.append(match_provider_id)
    if category is not None:
        clauses.append("category=?")
        params.append(category)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM commentary_evidence_events {where} ORDER BY match_provider_id, first_observed_poll_sequence"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_event_row_to_dict(row) for row in rows]
