"""Production CFS match-commentary ingestion and persistence (Issue #201).

Production follow-up to the Issue #196 diagnostic investigation
(``collection/match_commentary_evidence.py``,
``scheduler/match_commentary_capture.py``). Real Round 24 evidence --
including a live-poll capture sequence for ``CD_M20260142409`` and a
Bruno-captured raw ``.response.json`` snapshot of the same match, and the
combined diagnostic evidence report covering the rest of that round -- has
now confirmed the endpoint's contract closely enough to promote it. See
``docs/investigation/afl-json/ENDPOINT_CATALOG.md`` for the full confirmed
contract and ``docs/architecture/api/commentary_api_design.md`` for the
consumer API design.

This module is a deliberately new, narrowly-scoped production path, **not** a
promotion of the diagnostic collector -- mirroring how ``afl_json/match_period.py``
(Issue #187) sits alongside ``collection/match_state_evidence.py`` without
touching it. The diagnostic evidence tables/scheduler/profile keep running
completely independently (Issue #196 remains useful for debugging/regression
evidence -- see ``docs/diagnostics_framework.md``); nothing here reads from or
writes to them.

## Endpoint contract (confirmed)

``GET {CFS root}/commentaryFeed/{match_provider_id}`` -- one directory above
the ``/cfs/afl`` root most other CFS endpoints live under (Issue #199 tracks
a possible future URL-model refactor; this module uses the same
``base_url_override`` technique the diagnostic endpoint definition already
uses rather than pre-empting that refactor). Response shape:

```json
{
  "matchId": "CD_M20260142409",
  "lastUpdated": "2026-08-23T12:15:40.217+0000",
  "commentaryEvent": [
    {
      "comment": "GOAL - Hawks (Jack Gunston)",
      "periodNumber": 1,
      "periodSeconds": 59,
      "playerId": "CD_I291351",
      "teamId": "CD_T80",
      "scoreEvent": true
    }
  ]
}
```

Confirmed from the Round 24 evidence:

* the six event fields observed under Issue #196 (``comment``,
  ``periodNumber``, ``periodSeconds``, ``playerId``, ``teamId``,
  ``scoreEvent``) are the complete event shape -- no additional structured
  scoring fields (e.g. a points value or a discrete goal/behind/rushed type)
  were present anywhere in the captured concluded-match response, so
  ``score_event`` remains the only structured scoring fact this module
  persists; outcome type stays free text only (see "Do not parse prose"
  below);
  * the feed is still an **accumulated, newest-first** array with **no
  upstream event identifier** -- confirmed again on this real concluded
  response (period/second strictly non-increasing from array index 0);
  * multiple events legitimately share one ``(periodNumber, periodSeconds)``
  pair -- e.g. general statistical commentary and a scoring event both
  timestamped ``period=1, seconds=1483`` in the captured response;
  * ``scoreEvent=true`` events can have a null ``playerId`` with a non-null
  ``teamId`` for a rushed behind (``"BEHIND - Eagles (Rushed)"``), confirming
  the diagnostic evidence's team-only score-event case on real data;
  * pre-match (``periodNumber=0``) commentary -- team ins/outs, tips
  reminders, general preview commentary -- carries null ``playerId``/``teamId``
  and ``scoreEvent=false``;
  * the top-level ``lastUpdated`` value can advance even when no new event
  content appears in a later poll (the Bruno capture's ``lastUpdated`` is
  ~2 minutes newer than the diagnostic capture's final poll for the same
  match, with an identical event count) -- so ``lastUpdated`` alone is
  **not** a reliable "there is something new" signal and must never be used
  as a substitute for fingerprint-based dedup;
  * a genuine **official score-review reversal** is present in the
  combined Round 24 diagnostic evidence (not in ``CD_M20260142409`` itself,
  which shows no such sequence in either supplied file -- see
  ``docs/investigation/afl-json/ENDPOINT_CATALOG.md`` for the exact match):
  ``CD_M20260142406`` recorded ``"GOAL - Bulldogs (Cody Weightman)"`` at
  ``period=3, seconds=839``, then on a later poll a second, distinct event
  ``"BEHIND - Bulldogs (Cody Weightman)"`` at the *same* ``period=3,
  seconds=839`` for the same player/team/``scoreEvent``. The original GOAL
  entry was never removed or rewritten -- both remain in the accumulated
  feed. This is exactly the "possible edit" slot-key pattern the diagnostic
  module already detects, now confirmed against a real review sequence
  rather than only a hypothetical one; this module's ``possible_edit_of_event_id``
  linkage (see below) is built to preserve precisely this timeline.

## Do not parse prose

Per Issue #201, this module never derives player/team identity, or a
goal/behind/points outcome, from the free-text ``comment``. ``playerId``/
``teamId`` are the only structured identity fields, resolved through the
existing canonical crosswalks (see ``resolve_canonical_player``/
``resolve_canonical_team`` below); an unresolved provider id stays ``NULL``,
never guessed. ``categorise_event`` (borrowed conservative logic, kept as an
independent copy -- see below) is report/API convenience only and never
authoritative.

## Event identity / deduplication

The endpoint supplies no event id. This module reuses the diagnostic
module's proven approach on the confirmed real evidence: a SHA-256
**fingerprint** over ``(period_number, period_seconds, player_provider_id,
team_provider_id, score_event, comment)`` is the sole dedup key -- an
already-known fingerprint is never re-inserted, only its ``last_observed_*``
bookkeeping is touched, so the accumulated-feed's steady-state (same ~90-130
events returned on every single poll of a live match) never grows a table
that should stay proportional to actual event count. This is a deliberate,
documented, independent re-implementation (not an import) of
``collection.match_commentary_evidence._fingerprint``/``_slot_key`` --
production intentionally does not depend on the diagnostic module, matching
the ``match_period.py`` precedent.

A narrower **slot key** -- the same tuple without ``comment`` -- links a new
event to the most recently first-observed prior event sharing that slot,
*only* when the new event carries a non-null ``player_provider_id`` (exactly
the diagnostic module's restriction, which the ``CD_M20260142406`` review
case above validates: the reversal is player-attributed). The link is
recorded as ``possible_edit_of_event_id`` and is additive/heuristic only --
it is surfaced to consumers, never used to hide, merge, or delete the
earlier row. This is deliberately named "possible" for the same reason the
diagnostic evidence is: two independent, unrelated events *could*
legitimately collide on the same slot, so this must not be presented to
consumers as a certain fact.

Documented limitations (carried over from the diagnostic investigation,
still true on real evidence):

* two genuinely distinct events that are byte-identical across every
  fingerprinted field would collide and be treated as one event;
* event *removal* is not detected -- the accumulated feed only ever grows in
  every observation to date, so there is no evidence removal occurs, and
  nothing here assumes otherwise;
* sub-second real-world ordering between two events sharing one
  ``(period_number, period_seconds)`` pair is inferred only from original
  array position (see ``source_index`` below), never confirmed by an
  independent clock.

## Canonical identity linking

* ``match_provider_id`` -> ``matches.match_id`` is resolved by the caller
  (the production scheduler already knows both from its own match-window
  candidate query); ``resolve_canonical_match_id`` is provided for
  standalone/replay callers that only have a provider id.
* ``playerId`` -> canonical player: exact reuse of the existing
  ``player_provider_ids`` crosswalk used elsewhere in the codebase (e.g.
  ``afl_json.player_stats.upsert_player_stats``) with ``provider='champion_data'``.
  Unresolved -> ``NULL``, never guessed, never an error.
* ``teamId`` -> canonical team: exact reuse of ``afl_teams.provider_id``
  (the same column ``afl_json.player_persistence`` reads). Unresolved ->
  ``NULL``.

## Raw payload retention

Only each newly discovered event's own (small) raw JSON object is retained,
once, at first observation -- never the full accumulated array, and never on
an already-seen fingerprint. ``match_commentary_polls`` (migration ``0019``)
never stores a raw payload at all; it exists purely for poll-sequence
continuity and live/postgame candidate selection. Full accumulated-feed
evidence, when needed for debugging, remains available from the
still-running diagnostic profile.
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

from afl_json.contracts import CFS_API_BASE, EndpointDefinition, HttpMethod, SourceSystem

COLLECTOR_VERSION = "match_commentary_production_v1"
SOURCE_LABEL = "cfs_commentary_feed"

# See module docstring "Endpoint contract (confirmed)" -- commentaryFeed lives
# one directory above the standard CFS_API_BASE root, exactly like the
# diagnostic endpoint definition in collection.match_commentary_evidence.
# Kept as an independent EndpointDefinition (not imported) so the production
# path never depends on the diagnostic module, per this module's docstring.
_CFS_COMMENTARY_BASE_URL = CFS_API_BASE.removesuffix("/afl")

MATCH_COMMENTARY_ENDPOINT = EndpointDefinition(
    name="match_commentary",
    source=SourceSystem.CFS,
    method=HttpMethod.GET,
    path_template="/commentaryFeed/{match_provider_id}",
    requires_auth=True,
    entity_type="match_commentary",
    collection_paths=(),
    identifier_type=None,
    required_path_parameters=("match_provider_id",),
    base_url_override=_CFS_COMMENTARY_BASE_URL,
    verified=True,
    unverified_fields=(
        "whether commentaryEvent[] entries can ever be removed rather than only added to "
        "(no removal has been observed in any capture to date)",
        "confirmed sub-second real-world ordering for two events sharing one "
        "(periodNumber, periodSeconds) pair beyond original array position",
    ),
)

CATEGORY_QUARTER_START = "quarter_start"
CATEGORY_QUARTER_END = "quarter_end"
CATEGORY_SCORE_EVENT = "score_event"

OUTCOME_SUCCESS = "success"
OUTCOME_MALFORMED_PAYLOAD = "malformed_payload"

# Narrow, explicitly anchored patterns used only to derive a best-effort,
# non-authoritative report/API convenience label. See module docstring
# "Do not parse prose". Independent copy of
# collection.match_commentary_evidence's patterns for the same reason the
# fingerprint/slot-key logic is independently re-implemented above.
_QUARTER_START_RE = re.compile(r"^Q\d+\s+is\s+now\s+underway\.?$", re.IGNORECASE)
_QUARTER_END_RE = re.compile(r"^The\s+siren\s+has\s+sounded\s+to\s+end\s+Q\d+\.?$", re.IGNORECASE)


class MatchCommentaryError(ValueError):
    """A commentaryFeed payload could not be parsed for production persistence."""


def categorise_event(*, comment: str | None, period_seconds: int | None, score_event: bool | None) -> str | None:
    """Best-effort, non-authoritative convenience label. See module docstring."""
    if score_event is True:
        return CATEGORY_SCORE_EVENT
    if not comment:
        return None
    text = comment.strip()
    if _QUARTER_END_RE.match(text):
        return CATEGORY_QUARTER_END
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


def _fingerprint(period_number: int | None, period_seconds: int | None, player_provider_id: str | None,
                 team_provider_id: str | None, score_event: bool | None, comment: str | None) -> str:
    canonical = json.dumps(
        {
            "period_number": period_number, "period_seconds": period_seconds,
            "player_provider_id": player_provider_id, "team_provider_id": team_provider_id,
            "score_event": score_event, "comment": comment,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _slot_key(period_number: int | None, period_seconds: int | None, player_provider_id: str | None,
             team_provider_id: str | None, score_event: bool | None) -> str:
    return json.dumps(
        [period_number, period_seconds, player_provider_id, team_provider_id, score_event], sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class ParsedCommentaryEvent:
    """One parsed commentaryFeed entry, with its dedup fingerprint precomputed."""

    source_index: int
    period_number: int | None
    period_seconds: int | None
    comment: str | None
    player_provider_id: str | None
    team_provider_id: str | None
    score_event: bool | None
    fingerprint: str
    slot_key: str
    category: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ParsedCommentaryFeed:
    """One point-in-time snapshot of the (accumulated) commentaryFeed."""

    observed_at: str
    match_id: int
    match_provider_id: str
    match_status_at_poll: str | None
    feed_last_updated: str | None
    # None means commentaryEvent was missing or not a list this poll --
    # distinct from an empty list (field present, feed genuinely has no
    # events yet, e.g. very early pre-match).
    events: list[ParsedCommentaryEvent] | None


def _parse_one_event(raw: Any, *, source_index: int) -> ParsedCommentaryEvent | None:
    if not isinstance(raw, dict):
        return None
    period_number = _coerce_int(raw.get("periodNumber"))
    period_seconds = _coerce_int(raw.get("periodSeconds"))
    comment = _coerce_str(raw.get("comment"))
    player_provider_id = _coerce_str(raw.get("playerId"))
    team_provider_id = _coerce_str(raw.get("teamId"))
    score_event = _coerce_bool(raw.get("scoreEvent"))
    fingerprint = _fingerprint(
        period_number, period_seconds, player_provider_id, team_provider_id, score_event, comment,
    )
    slot_key = _slot_key(period_number, period_seconds, player_provider_id, team_provider_id, score_event)
    category = categorise_event(comment=comment, period_seconds=period_seconds, score_event=score_event)
    return ParsedCommentaryEvent(
        source_index=source_index, period_number=period_number, period_seconds=period_seconds, comment=comment,
        player_provider_id=player_provider_id, team_provider_id=team_provider_id, score_event=score_event,
        fingerprint=fingerprint, slot_key=slot_key, category=category, raw=deepcopy(raw),
    )


def parse_commentary_feed(payload: Any, *, match_id: int, match_provider_id: str, observed_at: str,
                          match_status_at_poll: str | None = None) -> ParsedCommentaryFeed:
    """Parse a raw commentaryFeed response for production persistence.

    Requires an object payload (raises ``MatchCommentaryError`` otherwise)
    but is deliberately tolerant of a missing/malformed ``commentaryEvent``
    field -- recorded as ``events=None``, never coerced to an empty list. An
    individual event entry that is not an object is skipped rather than
    aborting the whole parse.
    """
    if not isinstance(payload, dict):
        raise MatchCommentaryError("commentaryFeed payload is not an object")
    feed_last_updated = _coerce_str(payload.get("lastUpdated"))
    raw_events = payload.get("commentaryEvent")
    events: list[ParsedCommentaryEvent] | None
    if isinstance(raw_events, list):
        events = []
        for index, raw_event in enumerate(raw_events):
            parsed = _parse_one_event(raw_event, source_index=index)
            if parsed is not None:
                events.append(parsed)
    else:
        events = None
    return ParsedCommentaryFeed(
        observed_at=observed_at, match_id=match_id, match_provider_id=match_provider_id,
        match_status_at_poll=match_status_at_poll, feed_last_updated=feed_last_updated, events=events,
    )


def resolve_canonical_player(conn: sqlite3.Connection, player_provider_id: str | None) -> int | None:
    """Resolve a Champion Data player id to a canonical player id, or ``None``.

    Reuses the same ``player_provider_ids`` crosswalk read elsewhere in the
    codebase (e.g. ``afl_json.player_stats.upsert_player_stats``). Never
    raises, never guesses: an unresolved id is simply ``None``.
    """
    if player_provider_id is None:
        return None
    row = conn.execute(
        "SELECT player_id FROM player_provider_ids WHERE provider='champion_data' AND provider_player_id=?",
        (player_provider_id,),
    ).fetchone()
    return row[0] if row else None


def resolve_canonical_team(conn: sqlite3.Connection, team_provider_id: str | None) -> int | None:
    """Resolve a Champion Data team id to a canonical ``afl_teams.afl_id``, or ``None``.

    Reuses the same ``afl_teams.provider_id`` column read elsewhere (e.g.
    ``afl_json.player_persistence``). Never raises, never guesses.
    """
    if team_provider_id is None:
        return None
    row = conn.execute("SELECT afl_id FROM afl_teams WHERE provider_id=?", (team_provider_id,)).fetchone()
    return row[0] if row else None


def resolve_canonical_match_id(conn: sqlite3.Connection, match_provider_id: str) -> int | None:
    """Resolve a Champion Data match id to a canonical ``matches.match_id``, or ``None``.

    Only needed by standalone/replay callers (e.g. the internal import
    script) that do not already know ``match_id``; the production scheduler
    already has both from its own match-window candidate query.
    """
    row = conn.execute(
        "SELECT match_id FROM matches WHERE match_provider_id=?", (match_provider_id,),
    ).fetchone()
    return row[0] if row else None


def _next_poll_sequence(conn: sqlite3.Connection, match_provider_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(poll_sequence), 0) + 1 FROM match_commentary_polls WHERE match_provider_id=?",
        (match_provider_id,),
    ).fetchone()[0]


def persist_poll_outcome(conn: sqlite3.Connection, *, match_id: int, match_provider_id: str, observed_at: str,
                         match_status_at_poll: str | None, outcome: str) -> dict[str, Any]:
    """Record one non-success poll attempt (e.g. not yet published, transport error).

    Never raises for a bad/unavailable individual match -- see
    ``scheduler.match_commentary_production._capture_one``, which is what
    actually maps CFS client exceptions to an ``outcome`` string before
    calling this.
    """
    next_sequence = _next_poll_sequence(conn, match_provider_id)
    cur = conn.execute(
        """INSERT INTO match_commentary_polls(
               match_id, match_provider_id, poll_sequence, observed_at, match_status_at_poll, outcome,
               event_count_in_feed, new_event_count, feed_last_updated, collector_version
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            match_id, match_provider_id, next_sequence, observed_at, match_status_at_poll, outcome,
            None, 0, None, COLLECTOR_VERSION,
        ),
    )
    return {
        "id": cur.lastrowid, "poll_sequence": next_sequence, "outcome": outcome,
        "new_event_count": 0, "new_events": [], "possible_edits": [], "event_count_in_feed": None,
    }


def _load_existing_events(conn: sqlite3.Connection, match_provider_id: str,
                          ) -> tuple[dict[str, int], dict[str, list[tuple[int, int]]]]:
    rows = conn.execute(
        "SELECT id, event_fingerprint, slot_key FROM match_commentary_events WHERE match_provider_id=?",
        (match_provider_id,),
    ).fetchall()
    fingerprint_to_id: dict[str, int] = {}
    # Candidates keyed by slot; second tuple element is the row id itself,
    # used purely as an insertion-order tiebreaker (higher id == more
    # recently first-observed) since match_commentary_events has no
    # first-observed-poll-sequence column of its own.
    slot_to_candidates: dict[str, list[tuple[int, int]]] = {}
    for row in rows:
        fingerprint_to_id[row["event_fingerprint"]] = row["id"]
        slot_to_candidates.setdefault(row["slot_key"], []).append((row["id"], row["id"]))
    return fingerprint_to_id, slot_to_candidates


def persist_commentary_feed(conn: sqlite3.Connection, feed: ParsedCommentaryFeed) -> dict[str, Any]:
    """Insert one successfully parsed poll, deduplicating events by fingerprint.

    Never overwrites a previously captured event's content: an already-known
    fingerprint only has its ``last_observed_*`` bookkeeping touched. A
    genuinely new fingerprint is always inserted as a new row, even when it
    shares a "slot" (period/second/player/team/scoreEvent) with an existing
    row -- in that player-attributed case it is additionally linked via
    ``possible_edit_of_event_id`` for consumer/report visibility. See module
    docstring for the full fingerprint/slot-key/raw-retention policy and the
    confirmed ``CD_M20260142406`` score-review sequence this is built to
    preserve.

    Canonical player/team ids are resolved once, at first observation, using
    ``resolve_canonical_player``/``resolve_canonical_team``. An event that
    was unresolved when first observed keeps that ``NULL`` link on later
    polls -- this module never silently backfills a canonical link after the
    fact, since a later ``player_provider_ids``/``afl_teams`` write happening
    to appear is not evidence about the *event*'s original resolution
    context. A later crosswalk backfill is a separate, explicitly-scoped
    concern if ever needed.
    """
    next_sequence = _next_poll_sequence(conn, feed.match_provider_id)
    new_events: list[dict[str, Any]] = []
    possible_edits: list[dict[str, Any]] = []
    event_count_in_feed: int | None

    if feed.events is None:
        event_count_in_feed = None
    else:
        event_count_in_feed = len(feed.events)
        fingerprint_to_id, slot_to_candidates = _load_existing_events(conn, feed.match_provider_id)
        for event in feed.events:
            existing_id = fingerprint_to_id.get(event.fingerprint)
            if existing_id is not None:
                conn.execute(
                    "UPDATE match_commentary_events SET last_observed_at=?, last_seen_feed_last_updated=? "
                    "WHERE id=?",
                    (feed.observed_at, feed.feed_last_updated, existing_id),
                )
                continue

            possible_edit_of_id: int | None = None
            if event.player_provider_id is not None:
                candidates = slot_to_candidates.get(event.slot_key)
                if candidates:
                    possible_edit_of_id = max(candidates, key=lambda candidate: candidate[1])[0]

            canonical_player_id = resolve_canonical_player(conn, event.player_provider_id)
            canonical_team_id = resolve_canonical_team(conn, event.team_provider_id)

            cur = conn.execute(
                """INSERT INTO match_commentary_events(
                       match_id, match_provider_id, event_fingerprint, slot_key, period_number, period_seconds,
                       comment, score_event, player_provider_id, canonical_player_id, team_provider_id,
                       canonical_team_id, category, source_index, possible_edit_of_event_id, first_observed_at,
                       last_observed_at, source_feed_last_updated, last_seen_feed_last_updated, source,
                       raw_event_json, collector_version
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    feed.match_id, feed.match_provider_id, event.fingerprint, event.slot_key,
                    event.period_number, event.period_seconds, event.comment,
                    None if event.score_event is None else int(event.score_event),
                    event.player_provider_id, canonical_player_id, event.team_provider_id, canonical_team_id,
                    event.category, event.source_index, possible_edit_of_id, feed.observed_at, feed.observed_at,
                    feed.feed_last_updated, feed.feed_last_updated, SOURCE_LABEL,
                    json.dumps(event.raw, sort_keys=True), COLLECTOR_VERSION,
                ),
            )
            new_event_id = cur.lastrowid
            new_events.append({
                "id": new_event_id, "fingerprint": event.fingerprint, "category": event.category,
                "canonical_player_id": canonical_player_id, "canonical_team_id": canonical_team_id,
                "possible_edit_of_event_id": possible_edit_of_id,
            })
            fingerprint_to_id[event.fingerprint] = new_event_id
            slot_to_candidates.setdefault(event.slot_key, []).append((new_event_id, new_event_id))
            if possible_edit_of_id is not None:
                possible_edits.append({"new_event_id": new_event_id, "possible_edit_of_event_id": possible_edit_of_id})

    cur = conn.execute(
        """INSERT INTO match_commentary_polls(
               match_id, match_provider_id, poll_sequence, observed_at, match_status_at_poll, outcome,
               event_count_in_feed, new_event_count, feed_last_updated, collector_version
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            feed.match_id, feed.match_provider_id, next_sequence, feed.observed_at, feed.match_status_at_poll,
            OUTCOME_SUCCESS, event_count_in_feed, len(new_events), feed.feed_last_updated, COLLECTOR_VERSION,
        ),
    )
    return {
        "id": cur.lastrowid, "poll_sequence": next_sequence, "outcome": OUTCOME_SUCCESS,
        "new_event_count": len(new_events), "new_events": new_events, "possible_edits": possible_edits,
        "event_count_in_feed": event_count_in_feed,
    }


def recently_active_match_provider_ids(conn: sqlite3.Connection, *, now: datetime,
                                       grace_seconds: int) -> list[tuple[int, str]]:
    """Matches whose most recent commentary poll observed the *local*
    ``matches.status`` as LIVE or POSTGAME within ``grace_seconds`` of ``now``.

    Entirely self-contained to this module's own ``match_commentary_polls``
    table -- mirrors the diagnostic module's
    ``recently_live_match_provider_ids`` (the commentaryFeed payload does not
    appear to carry a live/score status field either), extended to also
    cover POSTGAME so a match that has left LIVE but not yet reached a
    stable CONCLUDED continues to be polled long enough to catch a
    late-arriving official score-review correction (see the confirmed
    ``CD_M20260142406`` review sequence in the module docstring).
    """
    if grace_seconds <= 0:
        return []
    cutoff = (now - timedelta(seconds=grace_seconds)).isoformat()
    rows = conn.execute(
        """
        SELECT match_id, match_provider_id, MAX(observed_at) AS last_active_at
        FROM match_commentary_polls
        WHERE match_status_at_poll IN ('LIVE', 'POSTGAME')
        GROUP BY match_provider_id
        HAVING last_active_at >= ?
        """,
        (cutoff,),
    ).fetchall()
    return [(row["match_id"], row["match_provider_id"]) for row in rows]


def _event_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["score_event"] = None if data["score_event"] is None else bool(data["score_event"])
    raw = data.pop("raw_event_json")
    data["raw_event"] = json.loads(raw) if raw else None
    return data


def event_rows(conn: sqlite3.Connection, *, match_id: int, period_number: int | None = None,
               canonical_player_id: int | None = None, canonical_team_id: int | None = None,
               score_events_only: bool = False) -> list[dict[str, Any]]:
    """Chronological (oldest-first) production commentary events for one match.

    This is the query the consumer API (``/api/v1/matches/{match_id}/commentary``)
    runs. Default ordering is ``period_number`` then ``period_seconds``
    ascending -- consumer-friendly chronological order -- even though the
    source feed itself is observed newest-first (see module docstring).
    Within one ``(period_number, period_seconds)`` pair, original source
    array position is used as a documented-best-effort tiebreaker: since the
    source array is newest-first, a *smaller* ``source_index`` is more
    recent, so ties break on ``source_index`` descending (oldest array
    position first), then ``id`` ascending as a final deterministic
    tiebreaker for genuinely simultaneous inserts.
    """
    clauses = ["match_id=?"]
    params: list[Any] = [match_id]
    if period_number is not None:
        clauses.append("period_number=?")
        params.append(period_number)
    if canonical_player_id is not None:
        clauses.append("canonical_player_id=?")
        params.append(canonical_player_id)
    if canonical_team_id is not None:
        clauses.append("canonical_team_id=?")
        params.append(canonical_team_id)
    if score_events_only:
        clauses.append("score_event=1")
    sql = (
        "SELECT * FROM match_commentary_events WHERE " + " AND ".join(clauses) +
        " ORDER BY period_number IS NULL, period_number ASC, period_seconds IS NULL, period_seconds ASC, "
        "source_index DESC, id ASC"
    )
    rows = conn.execute(sql, params).fetchall()
    return [_event_row_to_dict(row) for row in rows]
