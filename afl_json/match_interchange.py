"""Production CFS match-interchange ingestion and persistence (Issue #204).

Production promotion of the Issue #193 diagnostic investigation
(``collection/match_interchange_evidence.py``, ``scheduler/match_interchange_capture.py``).
This is a deliberately new, narrowly-scoped production path, **not** a
promotion of the diagnostic collector -- mirroring how ``afl_json/match_period.py``
(Issue #187) and ``afl_json/match_commentary.py`` (Issue #201) sit alongside
their own diagnostic counterparts without touching them. The diagnostic
evidence table/scheduler/profile keep running completely independently
(Issue #193 remains useful for debugging/regression evidence -- see
``docs/diagnostics_framework.md``); nothing here reads from or writes to it.

## Endpoint contract

``GET {CFS root}/matchInterchange/{match_provider_id}``. Response shape
(``tests/fixtures/afl/interchange/match_interchange_8216_concluded.json``,
the only real capture available at promotion time):

```json
{
  "matchId": "CD_M20260142001",
  "homeInterchange": [
    {
      "teamId": "CD_T10",
      "player": {"playerId": "CD_I1031792", "playerName": {...}, "captain": false, "playerJumperNumber": 36},
      "interchangeCount": 8, "benchReason": "ROTATION",
      "timeOnGround": 4697, "timeOnBench": 568, "powerRating": 5
    }
  ],
  "awayInterchange": [...],
  "homeInterchangeCounts": {"totalInterchangeCount": 75.0, ...},
  "awayInterchangeCounts": {"totalInterchangeCount": 73.0, ...}
}
```

The per-entry field shape (``teamId``, ``player.playerId``, ``interchangeCount``,
``benchReason``, ``timeOnGround``, ``timeOnBench``, ``powerRating``) is
confirmed structurally verified. The team-level ``home/awayInterchangeCounts``
totals are diagnostic-scope only (already captured by the still-running
Issue #193 profile) and are deliberately **not** part of this narrower
per-player production contract, which exists to answer one consumer
question: "is this canonical player currently on the interchange bench, and
what does CFS say about it?"

## Array-membership semantics: confirmed by real Round 24 live evidence

Issue #204 asks this module to establish, from captured evidence, whether
membership of ``homeInterchange[]``/``awayInterchange[]`` can safely be
documented as meaning "the player is currently off the ground". **It now
can be, for LIVE play**, on the strength of real Round 24 live diagnostic
observations reviewed after this promotion's initial draft (supplied as the
human-readable output of ``scripts/report_interchange_evidence.py`` against
a deployment's populated ``match_interchange_evidence_observations`` table
-- not available to, or reviewable from, this implementation session on its
own; see the module's earlier revision history / PR #206 discussion for
why the semantic below was initially left conservative).

That report covers **7 real Round 24 matches**
(``CD_M20260142401``, ``CD_M20260142403``, ``CD_M20260142404``,
``CD_M20260142405``, ``CD_M20260142406``, ``CD_M20260142408``,
``CD_M20260142409``), each polled roughly every 15 seconds across its full
~3 hour LIVE window. The diagnostic module's ``appeared``/``disappeared``
transition flags are computed as an exact Champion Data ``playerId`` set
difference between successive polls (``collection.match_interchange_evidence._player_set_transitions``,
``set(current) - set(previous)`` over indexed ``player.playerId`` values) --
so their presence in this real data is not an inference, it is direct proof
that the specific set of players listed in ``homeInterchange``/
``awayInterchange`` actually changes during LIVE play. Across the 7
matches: 442 ``player_appeared_home_interchange`` and 435
``player_disappeared_home_interchange`` events, 435
``player_appeared_away_interchange`` and 428
``player_disappeared_away_interchange`` events -- i.e. membership changed on
a large fraction of the ~700 polls per match. Appearances and
disappearances are near-perfectly paired within the same poll (a same-poll
appear+disappear on one side, not just growth), holding each side's listed
count at a fixed steady state of 5 throughout; the handful of polls that
briefly showed 4 or 6 listed players (e.g. ``CD_M20260142403`` seq 349,
``CD_M20260142404`` seq 196 and seq 479, ``CD_M20260142408`` seq 364,
``CD_M20260142409`` seq 329-332) self-corrected back to 5 on the very next
poll(s) -- consistent with catching an in-progress swap mid-transition at
the 15s polling granularity, never with a genuinely resized or reordered
pool. Membership changes are also tightly time-correlated with each team's
own ``totalInterchangeCount``/quarter count incrementing
(``home_total_interchange_count_changed``/``away_total_interchange_count_changed``
co-occur with, or immediately follow, the great majority of appear/
disappear events) -- i.e. CFS's own aggregate rotation counter moves in
lockstep with array membership changing, which is exactly what "a genuine
interchange/rotation event just happened" would look like from this data,
and is very hard to explain under the alternative "fixed, always-listed
pool" reading this promotion originally could not rule out.

**Update: individual round-trip and POSTGAME behaviour now confirmed too.**
A full per-poll (not aggregate-only) evidence export for ``CD_M20260142409``
-- 693 rows including every non-transition poll and the raw payload on
every transition poll -- was subsequently reviewed (Issue #204 comment,
PR #206). Two specific real fixtures now back this:

* **Individual player round-trip: confirmed.** Champion Data player
  ``CD_I1028561`` ("Tom Gross", home side) appears/disappears from
  ``CD_M20260142409``'s ``homeInterchange[]`` **five separate times** across
  the match (poll_sequence pairs (2,48), (100,149), (230,254), (445,482),
  (556,590) -- appear/disappear respectively); 23 distinct home-side
  Champion Data ids show the same repeated-round-trip pattern across the
  full match. The first round trip is checked in as real, verbatim raw
  ``matchInterchange`` responses:
  ``tests/fixtures/afl/interchange/match_interchange_CD_M20260142409_poll002_appeared.json``,
  ``..._poll048_disappeared.json``, ``..._poll100_reappeared.json`` -- see
  ``tests/test_interchange_round24_real_sequence.py``, which exercises
  ``parse_match_interchange``/``persist_match_interchange`` directly against
  them.
* **POSTGAME behaviour: confirmed to freeze.** The same match's evidence
  export includes 40 ``POSTGAME`` polls (poll_sequence 654-693, spanning the
  diagnostic profile's configured 600s post-live grace window). Every field
  -- not just array membership, but each entry's ``interchangeCount``/
  ``benchReason``/``timeOnGround``/``timeOnBench``/``powerRating``, and the
  team-level counts -- is byte-identical across all 40 polls, with zero
  transition flags recorded throughout. ``matchInterchange`` state freezes
  exactly at the ``LIVE`` -> ``POSTGAME`` transition and does not continue
  updating, at least through this ~10 minute captured window. Checked in as
  ``tests/fixtures/afl/interchange/match_interchange_CD_M20260142409_postgame_poll654.json``
  and ``..._postgame_poll693.json`` (reconstructed from this match's stored
  parsed field values rather than a verbatim raw payload, since the
  diagnostic module only retains the raw HTTP response on polls with a
  recorded transition -- POSTGAME had none; every field value in them is
  still real, see the fixture directory's metadata sidecar), exercised by
  the same test module to show persisting both produces zero new events.

**CONCLUDED remains unverified**: this match's capture ends at
poll_sequence 693, still ``POSTGAME`` -- no ``CONCLUDED`` row was ever
captured for it, consistent with the diagnostic profile's post-live grace
window elapsing before ``matches.status`` advanced further. Whether
``matchInterchange`` stays queryable/frozen, or becomes unavailable, once a
match reaches ``CONCLUDED`` remains an open question for a future capture.

On the strength of the confirmed findings above, this module and the
consumer API expose ``on_bench`` -- a per-player boolean reflecting current
``homeInterchange``/``awayInterchange`` array membership as of the most
recent poll, confirmed for LIVE play and confirmed to freeze (not
necessarily to remain available) through at least 10 minutes of POSTGAME.
See ``docs/investigation/afl-json/ENDPOINT_CATALOG.md`` and
``docs/api_v1_interchange.md`` for the consumer-facing documentation of
these findings and the one residual caveat (CONCLUDED).

## Do not infer bench_reason

Per Issue #204, ``benchReason`` (e.g. ``"ROTATION"``) is persisted and
returned exactly as CFS supplies it. This module never infers injury,
substitution, tactical, or medical meaning from it, from commentary, from
timing, or from any other field.

## Canonical identity linking

* ``match_provider_id`` -> ``matches.match_id`` is resolved by the caller
  (the production scheduler already knows both from its own match-window
  candidate query); ``resolve_canonical_match_id`` is provided for
  standalone/replay callers that only have a provider id.
* ``player.playerId`` -> canonical player: the existing ``player_provider_ids``
  crosswalk (``provider='champion_data'``), exactly as used by
  ``afl_json.match_commentary``/``afl_json.player_stats``. Never inferred
  from ``playerName``/``playerJumperNumber``. Unresolved -> ``NULL``, never
  guessed.
* ``teamId`` -> canonical team: ``afl_teams.provider_id``. Unresolved -> ``NULL``.

Unlike ``match_commentary_events`` (an immutable append-only log that
resolves canonical identity once, at first observation, and never backfills
it), ``match_interchange_state`` is a *current*-state table: canonical
identity is re-resolved on every poll that touches a player's row, so a
crosswalk added after a player's first interchange observation still
self-heals the current-state row on the next poll. ``match_interchange_events``
(the transition history) is immutable like commentary's event log: each row
keeps whatever canonical identity was resolved at the moment that specific
event was detected.

## Persistence shape and idempotency

Three tables (migration ``0021``):

* ``match_interchange_state`` -- one current row per ``(match_provider_id,
  player_provider_id)``, upserted every poll that observes that player.
  This is the table the consumer API's current-state route reads.
* ``match_interchange_events`` -- append-only, *meaningful-only* transition
  history (see ``persist_match_interchange`` for exactly what qualifies).
  Never written for a poll where only ``timeOnGround``/``timeOnBench``/
  ``powerRating`` changed with nothing else -- these tick on almost every
  poll for every on-ground-adjacent player and would otherwise flood the
  history with noise (Issue #204 explicitly prohibits this).
* ``match_interchange_polls`` -- lightweight poll bookkeeping (sequence
  continuity, outcome, feed entry counts), mirroring ``match_commentary_polls``.

Every write diffs the incoming poll against **durable** previously-persisted
state (``match_interchange_state``), never an in-memory previous-poll
object -- so a repeated identical poll, a replay, or a scheduler/container
restart mid-match all produce the same result: no spurious event rows, and
the current-state row simply reflects the latest observed values. This is
the same durable-diff idempotency strategy already proven by the Issue #193
diagnostic module's ``load_previous_observation``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from afl_json.contracts import EndpointDefinition, HttpMethod, SourceSystem

COLLECTOR_VERSION = "match_interchange_production_v1"
SOURCE_LABEL = "cfs_match_interchange"

MATCH_INTERCHANGE_ENDPOINT = EndpointDefinition(
    name="match_interchange",
    source=SourceSystem.CFS,
    method=HttpMethod.GET,
    path_template="/matchInterchange/{match_provider_id}",
    requires_auth=True,
    entity_type="match_interchange",
    collection_paths=(),
    identifier_type=None,
    required_path_parameters=("match_provider_id",),
    verified=True,
    unverified_fields=(
        "matchInterchange behaviour once a match reaches CONCLUDED: whether the endpoint "
        "stays queryable/frozen or becomes unavailable (issue #204; POSTGAME is confirmed to "
        "freeze -- see afl_json.match_interchange module docstring)",
        "the complete set of benchReason enum values beyond the single observed ROTATION",
        "timeOnGround/timeOnBench update cadence and precision beyond what polling can observe",
    ),
)

EVENT_APPEARED = "appeared"
EVENT_DISAPPEARED = "disappeared"
EVENT_INTERCHANGE_COUNT_CHANGED = "interchange_count_changed"
EVENT_BENCH_REASON_CHANGED = "bench_reason_changed"

OUTCOME_SUCCESS = "success"


class MatchInterchangeError(ValueError):
    """A matchInterchange payload could not be parsed for production persistence."""


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _coerce_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class ParsedInterchangeEntry:
    """One parsed homeInterchange/awayInterchange entry, identity-scoped only.

    Deliberately excludes ``playerName``/``playerJumperNumber`` from the
    persisted shape -- Issue #204 requires identity resolution through
    ``playerId`` only, never display name or jumper number.
    """

    side: Literal["home", "away"]
    team_provider_id: str | None
    player_provider_id: str
    interchange_count: int | None
    bench_reason: str | None
    time_on_ground: int | None
    time_on_bench: int | None
    power_rating: int | None


@dataclass(frozen=True, slots=True)
class ParsedMatchInterchange:
    """One point-in-time snapshot of matchInterchange, ready for persistence."""

    observed_at: str
    match_id: int
    match_provider_id: str
    match_status_at_poll: str | None
    # None means the field was missing/malformed this poll -- distinct from
    # an empty list (field present, genuinely no one currently listed).
    # Comparison against durable state must skip a side entirely when None,
    # never treat it as "everyone disappeared" -- see persist_match_interchange().
    home_entries: list[ParsedInterchangeEntry] | None
    away_entries: list[ParsedInterchangeEntry] | None
    # False when at least one entry on that side could not be identified
    # (see _parse_side_entries) -- disappearance inference must be skipped
    # for such a side this poll, even though its identifiable entries are
    # still processed normally. Vacuously True when the corresponding
    # *_entries is None (disappearance inference is already skipped via that).
    home_entries_complete: bool
    away_entries_complete: bool


def _parse_side_entries(raw: Any, *, side: Literal["home", "away"],
                        ) -> tuple[list[ParsedInterchangeEntry] | None, bool]:
    """Parse one side's array; also reports whether every entry was identifiable.

    The second element is ``False`` when at least one entry could not be
    mapped to a player (not an object, or missing a usable ``player.playerId``)
    -- meaning this side's *complete* current membership cannot be trusted
    this poll, even though the entries that *did* parse are still good
    observations. Callers must not infer a previously-tracked player's
    disappearance from a side where this is ``False``: the "missing" player
    could simply be the one whose entry was malformed this poll, not someone
    who has genuinely left the list. See ``persist_match_interchange``.
    """
    if not isinstance(raw, list):
        return None, True
    entries: list[ParsedInterchangeEntry] = []
    fully_identified = True
    for item in raw:
        if not isinstance(item, dict):
            fully_identified = False
            continue
        player = item.get("player")
        player_provider_id = player.get("playerId") if isinstance(player, dict) else None
        if not isinstance(player_provider_id, str) or not player_provider_id:
            fully_identified = False
            continue
        entries.append(ParsedInterchangeEntry(
            side=side,
            team_provider_id=_coerce_str(item.get("teamId")),
            player_provider_id=player_provider_id,
            interchange_count=_coerce_int(item.get("interchangeCount")),
            bench_reason=_coerce_str(item.get("benchReason")),
            time_on_ground=_coerce_int(item.get("timeOnGround")),
            time_on_bench=_coerce_int(item.get("timeOnBench")),
            power_rating=_coerce_int(item.get("powerRating")),
        ))
    return entries, fully_identified


def parse_match_interchange(payload: Any, *, match_id: int, match_provider_id: str, observed_at: str,
                            match_status_at_poll: str | None = None) -> ParsedMatchInterchange:
    """Parse a raw matchInterchange response for production persistence.

    Requires an object payload (raises ``MatchInterchangeError`` otherwise)
    but is deliberately tolerant of a missing/malformed ``homeInterchange``/
    ``awayInterchange`` field -- recorded as ``None``, never coerced to an
    empty list. An individual entry missing a usable ``player.playerId`` is
    skipped rather than aborting the whole parse.

    Also raises ``MatchInterchangeError`` when the payload's own top-level
    ``matchId`` is present and disagrees with the requested
    ``match_provider_id`` -- the same misrouted/mis-cached-response guard
    ``afl_json.match_commentary.parse_commentary_feed`` uses, so a wrong
    response is never silently persisted against the wrong canonical match.
    """
    if not isinstance(payload, dict):
        raise MatchInterchangeError("matchInterchange payload is not an object")
    payload_match_id = payload.get("matchId")
    if isinstance(payload_match_id, str) and payload_match_id != match_provider_id:
        raise MatchInterchangeError(
            f"matchInterchange payload matchId {payload_match_id!r} does not match requested "
            f"match_provider_id {match_provider_id!r}"
        )
    home_entries, home_complete = _parse_side_entries(payload.get("homeInterchange"), side="home")
    away_entries, away_complete = _parse_side_entries(payload.get("awayInterchange"), side="away")
    return ParsedMatchInterchange(
        observed_at=observed_at, match_id=match_id, match_provider_id=match_provider_id,
        match_status_at_poll=match_status_at_poll,
        home_entries=home_entries, away_entries=away_entries,
        home_entries_complete=home_complete, away_entries_complete=away_complete,
    )


def resolve_canonical_player(conn: sqlite3.Connection, player_provider_id: str | None) -> int | None:
    """Resolve a Champion Data player id to a canonical player id, or ``None``.

    Reuses the same ``player_provider_ids`` crosswalk read elsewhere in the
    codebase. Never raises, never guesses.
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

    Reuses the same ``afl_teams.provider_id`` column read elsewhere. Never
    raises, never guesses.
    """
    if team_provider_id is None:
        return None
    row = conn.execute("SELECT afl_id FROM afl_teams WHERE provider_id=?", (team_provider_id,)).fetchone()
    return row[0] if row else None


def resolve_canonical_match_id(conn: sqlite3.Connection, match_provider_id: str) -> int | None:
    """Resolve a Champion Data match id to a canonical ``matches.match_id``, or ``None``.

    Only needed by standalone/replay callers that do not already know
    ``match_id``; the production scheduler already has both from its own
    match-window candidate query.
    """
    row = conn.execute(
        "SELECT match_id FROM matches WHERE match_provider_id=?", (match_provider_id,),
    ).fetchone()
    return row[0] if row else None


def _next_poll_sequence(conn: sqlite3.Connection, match_provider_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(poll_sequence), 0) + 1 FROM match_interchange_polls WHERE match_provider_id=?",
        (match_provider_id,),
    ).fetchone()[0]


def persist_poll_outcome(conn: sqlite3.Connection, *, match_id: int, match_provider_id: str, observed_at: str,
                         match_status_at_poll: str | None, outcome: str) -> dict[str, Any]:
    """Record one non-success poll attempt (e.g. not yet published, transport error).

    Never raises for a bad/unavailable individual match -- see
    ``scheduler.match_interchange_production._capture_one``, which maps CFS
    client exceptions to an ``outcome`` string before calling this.
    """
    next_sequence = _next_poll_sequence(conn, match_provider_id)
    cur = conn.execute(
        """INSERT INTO match_interchange_polls(
               match_id, match_provider_id, poll_sequence, observed_at, match_status_at_poll, outcome,
               home_count_in_feed, away_count_in_feed, collector_version
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            match_id, match_provider_id, next_sequence, observed_at, match_status_at_poll, outcome,
            None, None, COLLECTOR_VERSION,
        ),
    )
    return {
        "id": cur.lastrowid, "poll_sequence": next_sequence, "outcome": outcome,
        "appeared": [], "disappeared": [], "changed": [],
    }


def _load_existing_state(conn: sqlite3.Connection, match_provider_id: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM match_interchange_state WHERE match_provider_id=?", (match_provider_id,),
    ).fetchall()
    return {row["player_provider_id"]: row for row in rows}


def persist_match_interchange(conn: sqlite3.Connection, parsed: ParsedMatchInterchange, *,
                              source: str = SOURCE_LABEL,
                              collector_version: str = COLLECTOR_VERSION) -> dict[str, Any]:
    """Insert one parsed poll: upsert current state, append meaningful transitions.

    Diffs against durable ``match_interchange_state`` (never an in-memory
    previous-poll object), so this is restart-safe and idempotent by
    construction -- see module docstring "Persistence shape and idempotency".

    A side (home/away) whose array was missing/malformed this poll
    (``parsed.home_entries``/``away_entries`` is ``None``) is skipped
    entirely for both state updates and disappearance detection for players
    on that side -- a transient upstream hiccup must never be read as
    "everyone on this side left the interchange list". A side whose array
    was present but contained at least one unidentifiable entry (``parsed.
    home_entries_complete``/``away_entries_complete`` is ``False``) still has
    its identifiable entries processed normally, but is excluded from
    disappearance detection for the same reason -- the player "missing"
    from this poll could simply be the one behind the malformed entry.

    Meaningful transitions only (Issue #204): a player's row being newly
    created or transitioning from off-list to on-list is ``appeared``; a
    previously on-list player missing from this poll's (known) array is
    ``disappeared``; ``interchange_count``/``bench_reason`` changing on an
    already on-list player emits the matching ``*_changed`` event.
    ``time_on_ground``/``time_on_bench``/``power_rating`` are always
    refreshed on the current-state row but never by themselves produce an
    event row.
    """
    next_sequence = _next_poll_sequence(conn, parsed.match_provider_id)
    existing = _load_existing_state(conn, parsed.match_provider_id)

    appeared: list[dict[str, Any]] = []
    disappeared: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []

    seen_this_poll: set[str] = set()

    def _record_event(*, side: str, player_provider_id: str, canonical_player_id: int | None,
                      team_provider_id: str | None, canonical_team_id: int | None, event_type: str,
                      interchange_count: int | None, previous_interchange_count: int | None,
                      bench_reason: str | None, previous_bench_reason: str | None,
                      time_on_ground: int | None, time_on_bench: int | None, power_rating: int | None) -> int:
        cur = conn.execute(
            """INSERT INTO match_interchange_events(
                   match_id, match_provider_id, player_provider_id, canonical_player_id, team_provider_id,
                   canonical_team_id, side, event_type, interchange_count, previous_interchange_count,
                   bench_reason, previous_bench_reason, time_on_ground, time_on_bench, power_rating,
                   observed_at, match_status_at_poll, source, collector_version
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                parsed.match_id, parsed.match_provider_id, player_provider_id, canonical_player_id,
                team_provider_id, canonical_team_id, side, event_type, interchange_count,
                previous_interchange_count, bench_reason, previous_bench_reason, time_on_ground,
                time_on_bench, power_rating, parsed.observed_at, parsed.match_status_at_poll, source,
                collector_version,
            ),
        )
        return cur.lastrowid

    for entries, known_this_poll in (
        (parsed.home_entries, parsed.home_entries is not None),
        (parsed.away_entries, parsed.away_entries is not None),
    ):
        if not known_this_poll:
            continue
        for entry in entries:
            seen_this_poll.add(entry.player_provider_id)
            canonical_player_id = resolve_canonical_player(conn, entry.player_provider_id)
            canonical_team_id = resolve_canonical_team(conn, entry.team_provider_id)
            existing_row = existing.get(entry.player_provider_id)

            if existing_row is None:
                conn.execute(
                    """INSERT INTO match_interchange_state(
                           match_id, match_provider_id, player_provider_id, canonical_player_id, team_provider_id,
                           canonical_team_id, side, on_bench, interchange_count, bench_reason,
                           time_on_ground, time_on_bench, power_rating, first_observed_at, last_observed_at,
                           last_transition_at, match_status_at_last_observation, collector_version, updated_at
                       ) VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        parsed.match_id, parsed.match_provider_id, entry.player_provider_id, canonical_player_id,
                        entry.team_provider_id, canonical_team_id, entry.side, entry.interchange_count,
                        entry.bench_reason, entry.time_on_ground, entry.time_on_bench, entry.power_rating,
                        parsed.observed_at, parsed.observed_at, parsed.observed_at, parsed.match_status_at_poll,
                        collector_version, parsed.observed_at,
                    ),
                )
                event_id = _record_event(
                    side=entry.side, player_provider_id=entry.player_provider_id,
                    canonical_player_id=canonical_player_id, team_provider_id=entry.team_provider_id,
                    canonical_team_id=canonical_team_id, event_type=EVENT_APPEARED,
                    interchange_count=entry.interchange_count, previous_interchange_count=None,
                    bench_reason=entry.bench_reason, previous_bench_reason=None,
                    time_on_ground=entry.time_on_ground, time_on_bench=entry.time_on_bench,
                    power_rating=entry.power_rating,
                )
                appeared.append({"id": event_id, "player_provider_id": entry.player_provider_id, "side": entry.side})
                continue

            was_on_list = bool(existing_row["on_bench"])
            prev_count = existing_row["interchange_count"]
            prev_reason = existing_row["bench_reason"]
            transitioned = not was_on_list
            last_transition_at = parsed.observed_at if transitioned else existing_row["last_transition_at"]

            conn.execute(
                """UPDATE match_interchange_state SET
                       canonical_player_id=?, team_provider_id=?, canonical_team_id=?, side=?,
                       on_bench=1, interchange_count=?, bench_reason=?, time_on_ground=?,
                       time_on_bench=?, power_rating=?, last_observed_at=?, last_transition_at=?,
                       match_status_at_last_observation=?, collector_version=?, updated_at=?
                   WHERE id=?""",
                (
                    canonical_player_id, entry.team_provider_id, canonical_team_id, entry.side,
                    entry.interchange_count, entry.bench_reason, entry.time_on_ground, entry.time_on_bench,
                    entry.power_rating, parsed.observed_at, last_transition_at, parsed.match_status_at_poll,
                    collector_version, parsed.observed_at, existing_row["id"],
                ),
            )

            if transitioned:
                event_id = _record_event(
                    side=entry.side, player_provider_id=entry.player_provider_id,
                    canonical_player_id=canonical_player_id, team_provider_id=entry.team_provider_id,
                    canonical_team_id=canonical_team_id, event_type=EVENT_APPEARED,
                    interchange_count=entry.interchange_count, previous_interchange_count=prev_count,
                    bench_reason=entry.bench_reason, previous_bench_reason=prev_reason,
                    time_on_ground=entry.time_on_ground, time_on_bench=entry.time_on_bench,
                    power_rating=entry.power_rating,
                )
                appeared.append({"id": event_id, "player_provider_id": entry.player_provider_id, "side": entry.side})
                continue

            if prev_count != entry.interchange_count:
                event_id = _record_event(
                    side=entry.side, player_provider_id=entry.player_provider_id,
                    canonical_player_id=canonical_player_id, team_provider_id=entry.team_provider_id,
                    canonical_team_id=canonical_team_id, event_type=EVENT_INTERCHANGE_COUNT_CHANGED,
                    interchange_count=entry.interchange_count, previous_interchange_count=prev_count,
                    bench_reason=entry.bench_reason, previous_bench_reason=prev_reason,
                    time_on_ground=entry.time_on_ground, time_on_bench=entry.time_on_bench,
                    power_rating=entry.power_rating,
                )
                changed.append({"id": event_id, "player_provider_id": entry.player_provider_id, "event_type": EVENT_INTERCHANGE_COUNT_CHANGED})

            if prev_reason != entry.bench_reason:
                event_id = _record_event(
                    side=entry.side, player_provider_id=entry.player_provider_id,
                    canonical_player_id=canonical_player_id, team_provider_id=entry.team_provider_id,
                    canonical_team_id=canonical_team_id, event_type=EVENT_BENCH_REASON_CHANGED,
                    interchange_count=entry.interchange_count, previous_interchange_count=prev_count,
                    bench_reason=entry.bench_reason, previous_bench_reason=prev_reason,
                    time_on_ground=entry.time_on_ground, time_on_bench=entry.time_on_bench,
                    power_rating=entry.power_rating,
                )
                changed.append({"id": event_id, "player_provider_id": entry.player_provider_id, "event_type": EVENT_BENCH_REASON_CHANGED})

    # Disappearance: an existing on-list player, on a side whose array was
    # both known this poll and fully identified (see _parse_side_entries),
    # who was not seen in this poll's (known) entries. A side with even one
    # malformed/unidentifiable entry is excluded here -- the "missing"
    # player could simply be the one behind that malformed entry, not
    # someone who genuinely left the list; treating that as a confirmed
    # disappearance would write a false event and a false state flip.
    known_sides = {
        side for side, known in (
            ("home", parsed.home_entries is not None and parsed.home_entries_complete),
            ("away", parsed.away_entries is not None and parsed.away_entries_complete),
        ) if known
    }
    for player_provider_id, row in existing.items():
        if player_provider_id in seen_this_poll:
            continue
        if not bool(row["on_bench"]):
            continue
        if row["side"] not in known_sides:
            continue
        # Re-resolve canonical identity here too, not just on the appear/
        # update paths above -- otherwise a player who disappears before a
        # crosswalk is added for them would stay unresolved forever, since
        # no further appear/update event would touch this row unless they
        # reappear. This keeps current-state self-healing uniform across
        # every write path (see module docstring "Canonical identity linking").
        canonical_player_id = resolve_canonical_player(conn, player_provider_id)
        canonical_team_id = resolve_canonical_team(conn, row["team_provider_id"])
        conn.execute(
            "UPDATE match_interchange_state SET canonical_player_id=?, canonical_team_id=?, "
            "on_bench=0, last_transition_at=?, match_status_at_last_observation=?, "
            "collector_version=?, updated_at=? WHERE id=?",
            (canonical_player_id, canonical_team_id, parsed.observed_at, parsed.match_status_at_poll,
             collector_version, parsed.observed_at, row["id"]),
        )
        event_id = _record_event(
            side=row["side"], player_provider_id=player_provider_id,
            canonical_player_id=canonical_player_id, team_provider_id=row["team_provider_id"],
            canonical_team_id=canonical_team_id, event_type=EVENT_DISAPPEARED,
            interchange_count=row["interchange_count"], previous_interchange_count=row["interchange_count"],
            bench_reason=row["bench_reason"], previous_bench_reason=row["bench_reason"],
            time_on_ground=row["time_on_ground"], time_on_bench=row["time_on_bench"],
            power_rating=row["power_rating"],
        )
        disappeared.append({"id": event_id, "player_provider_id": player_provider_id, "side": row["side"]})

    home_count = len(parsed.home_entries) if parsed.home_entries is not None else None
    away_count = len(parsed.away_entries) if parsed.away_entries is not None else None
    cur = conn.execute(
        """INSERT INTO match_interchange_polls(
               match_id, match_provider_id, poll_sequence, observed_at, match_status_at_poll, outcome,
               home_count_in_feed, away_count_in_feed, collector_version
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            parsed.match_id, parsed.match_provider_id, next_sequence, parsed.observed_at,
            parsed.match_status_at_poll, OUTCOME_SUCCESS, home_count, away_count, collector_version,
        ),
    )
    return {
        "id": cur.lastrowid, "poll_sequence": next_sequence, "outcome": OUTCOME_SUCCESS,
        "appeared": appeared, "disappeared": disappeared, "changed": changed,
        "home_count_in_feed": home_count, "away_count_in_feed": away_count,
    }


def recently_active_match_provider_ids(conn: sqlite3.Connection, *, now: datetime,
                                       grace_seconds: int) -> list[tuple[int, str]]:
    """Matches whose most recent interchange poll observed the *local*
    ``matches.status`` as LIVE or POSTGAME within ``grace_seconds`` of ``now``.

    Mirrors ``afl_json.match_commentary.recently_active_match_provider_ids``
    (the matchInterchange payload does not carry a live/score status field
    either) and is entirely self-contained to this module's own
    ``match_interchange_polls`` table.
    """
    if grace_seconds <= 0:
        return []
    cutoff = (now - timedelta(seconds=grace_seconds)).isoformat()
    rows = conn.execute(
        """
        SELECT match_id, match_provider_id, MAX(observed_at) AS last_active_at
        FROM match_interchange_polls
        WHERE match_status_at_poll IN ('LIVE', 'POSTGAME')
        GROUP BY match_provider_id
        HAVING last_active_at >= ?
        """,
        (cutoff,),
    ).fetchall()
    return [(row["match_id"], row["match_provider_id"]) for row in rows]


def _state_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["on_bench"] = bool(data["on_bench"])
    return data


def current_state_rows(conn: sqlite3.Connection, *, match_id: int, side: str | None = None,
                       canonical_player_id: int | None = None,
                       on_bench_only: bool = False) -> list[dict[str, Any]]:
    """Current per-player interchange state for one match. Backs the consumer API."""
    clauses = ["match_id=?"]
    params: list[Any] = [match_id]
    if side is not None:
        clauses.append("side=?")
        params.append(side)
    if canonical_player_id is not None:
        clauses.append("canonical_player_id=?")
        params.append(canonical_player_id)
    if on_bench_only:
        clauses.append("on_bench=1")
    sql = (
        "SELECT * FROM match_interchange_state WHERE " + " AND ".join(clauses) +
        " ORDER BY side, player_provider_id"
    )
    rows = conn.execute(sql, params).fetchall()
    return [_state_row_to_dict(row) for row in rows]


def event_rows(conn: sqlite3.Connection, *, match_id: int, canonical_player_id: int | None = None,
              event_type: str | None = None) -> list[dict[str, Any]]:
    """Chronological (oldest-first) production interchange transition history for one match.

    This is the query the consumer API's events route runs.
    """
    clauses = ["match_id=?"]
    params: list[Any] = [match_id]
    if canonical_player_id is not None:
        clauses.append("canonical_player_id=?")
        params.append(canonical_player_id)
    if event_type is not None:
        clauses.append("event_type=?")
        params.append(event_type)
    sql = (
        "SELECT * FROM match_interchange_events WHERE " + " AND ".join(clauses) +
        " ORDER BY observed_at ASC, id ASC"
    )
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]
