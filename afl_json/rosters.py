"""Collection and conservative normalisation of verified CFS match rosters."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .client import AflJsonClient, AflJsonInvalidResponse, AflJsonResourceUnavailable
from .collectors import RawResponseWriter

# Production persistence collector version (Issue #219). Bumped only when the
# persisted shape of cfs_match_rosters/cfs_match_roster_selections/
# cfs_match_roster_context changes in a way a consumer should be able to see.
ROSTER_COLLECTOR_VERSION = "match_roster_production_v1"

# The five list-shaped change/context collections the collector already
# normalises distinctly from selected positions -- see
# MatchRosterCollector._team_records. Persistence never invents a sixth.
CONTEXT_TYPES: tuple[str, ...] = ("ins", "outs", "lateChanges", "clubDebuts", "milestones")


class RosterStatus(str, Enum):
    PUBLISHED = "published"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RosterCollectionResult:
    """One immutable roster snapshot; unavailable snapshots never imply deletion."""

    round_provider_id: str
    status: RosterStatus
    selections: list[dict[str, Any]]
    rosters: list[dict[str, Any]]
    publication_state: str | None = None
    provider_timestamp: str | None = None
    provider_version: str | int | None = None


@dataclass(frozen=True, slots=True)
class RosterChanges:
    added: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    changed: list[dict[str, Any]]
    unchanged: list[dict[str, Any]]
    replacement_safe: bool


class MatchRosterCollector:
    """Collect selections through the shared authenticated AFL JSON client."""

    def __init__(self, client: AflJsonClient, *, raw_directory: str | Path | None = None):
        self.client = client
        self.raw_writer = RawResponseWriter(raw_directory) if raw_directory is not None else None

    def collect(self, round_provider_id: str) -> RosterCollectionResult:
        if not isinstance(round_provider_id, str) or not round_provider_id.strip():
            raise ValueError("round_provider_id is required")
        round_provider_id = round_provider_id.strip()
        try:
            response = self.client.get(
                "match_rosters", path_parameters={"round_provider_id": round_provider_id}
            )
        except AflJsonResourceUnavailable:
            return RosterCollectionResult(round_provider_id, RosterStatus.UNAVAILABLE, [], [])
        if self.raw_writer:
            # Preserve the provider value exactly: available responses remain
            # lists and a future/unpublished response remains JSON null.
            self.raw_writer.write(
                "match_rosters", response.data,
                scope={"roundProviderId": round_provider_id}, page=1,
            )
        return _normalise_rosters(response.data, round_provider_id)


def compare_rosters(previous: RosterCollectionResult,
                    current: RosterCollectionResult) -> RosterChanges:
    """Compare snapshots; unavailable and ambiguous empty lists cannot replace data."""
    replacement_safe = current.status is RosterStatus.PUBLISHED
    if not replacement_safe:
        return RosterChanges([], [], [], [], False)
    before = {_selection_key(item): item for item in previous.selections}
    after = {_selection_key(item): item for item in current.selections}
    added = [after[key] for key in sorted(after.keys() - before.keys())]
    removed = [before[key] for key in sorted(before.keys() - after.keys())]
    changed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for key in sorted(before.keys() & after.keys()):
        # Provider array order is retained for diagnostics, but is not a roster
        # change: live positions arrays can reorder between publications.
        if _comparison_value(before[key]) == _comparison_value(after[key]):
            unchanged.append(after[key])
        else:
            changed.append({"before": before[key], "after": after[key]})
    return RosterChanges(added, removed, changed, unchanged, True)


# --- Production persistence (Issue #219) -------------------------------------
#
# A CFS roster is a team *selection*, not proof of match participation.
# Nothing in this section, and nothing derived from these tables, ever
# implies that a persisted selection or context record played in the match --
# see cfs_player_stats for actual participation/statistics.
#
# persist_match_rosters reuses MatchRosterCollector's own normalisation
# (RosterCollectionResult.rosters/selections) unchanged rather than
# re-parsing the raw payload, and reuses compare_rosters' existing
# replacement_safe gate (RosterStatus.PUBLISHED only) so persistence and the
# collector's own change-comparison agree on when a round observation is
# trustworthy. See db/migrations/0024_match_roster_production.py for the
# full schema/safety rationale.


@dataclass(frozen=True, slots=True)
class RosterPersistenceSummary:
    """Outcome of one persist_match_rosters call."""

    rosters_written: int = 0
    selections_written: int = 0
    context_written: int = 0
    unmatched_matches: tuple[str, ...] = ()
    unmatched_teams: tuple[tuple[str, str], ...] = ()


def resolve_canonical_match(conn: sqlite3.Connection, match_provider_id: str) -> int | None:
    """Resolve a Champion Data match id to a canonical ``matches.match_id``, or ``None``.

    Never raises, never guesses -- mirrors
    ``afl_json.match_interchange.resolve_canonical_match_id``.
    """
    row = conn.execute(
        "SELECT match_id FROM matches WHERE match_provider_id=?", (match_provider_id,),
    ).fetchone()
    return row[0] if row else None


def resolve_canonical_team(conn: sqlite3.Connection, team_provider_id: str | None) -> int | None:
    """Resolve a Champion Data team id to a canonical ``afl_teams.afl_id``, or ``None``.

    Reuses the same ``afl_teams.provider_id`` column read elsewhere (e.g.
    ``afl_json.match_interchange.resolve_canonical_team``). Never raises,
    never guesses.
    """
    if team_provider_id is None:
        return None
    row = conn.execute(
        "SELECT afl_id FROM afl_teams WHERE provider_id=?", (team_provider_id,),
    ).fetchone()
    return row[0] if row else None


def resolve_canonical_player(conn: sqlite3.Connection, player_provider_id: str | None) -> int | None:
    """Resolve a Champion Data player id to a canonical player id, or ``None``.

    Reuses the same ``player_provider_ids`` crosswalk (``provider=
    'champion_data'``) read by ``afl_json.player_stats``/``afl_json.match_interchange``/
    ``afl_json.match_commentary``. Never raises, never guesses from name or
    jumper number.
    """
    if player_provider_id is None:
        return None
    row = conn.execute(
        "SELECT player_id FROM player_provider_ids WHERE provider='champion_data' AND provider_player_id=?",
        (player_provider_id,),
    ).fetchone()
    return row[0] if row else None


def persist_match_rosters(conn: sqlite3.Connection, result: RosterCollectionResult, *,
                          observed_at: str,
                          collector_version: str = ROSTER_COLLECTOR_VERSION) -> RosterPersistenceSummary:
    """Upsert current roster selection/context state from one round observation.

    Only a ``RosterStatus.PUBLISHED`` observation is replacement-safe -- an
    ``UNAVAILABLE`` (top-level ``null``) or ``EMPTY`` (top-level ``[]``)
    result is a deliberate, immediate no-op that never touches a previously
    persisted roster (see module docstring and
    ``db/migrations/0024_match_roster_production.py``). A malformed/partial
    response never reaches this function at all: ``MatchRosterCollector.collect``
    raises before producing a ``RosterCollectionResult``.

    For each match/team pair this observation covers: an unresolved canonical
    match (``matches.match_provider_id`` not found) skips that match entirely
    (recorded in ``unmatched_matches``) without affecting any other match in
    the same round observation -- a round covers many matches, and one
    unresolved match must never block persistence for the rest. An unresolved
    canonical team is recorded in ``unmatched_teams`` but the roster/selection/
    context rows are still persisted with ``canonical_team_id=NULL`` -- the
    Champion Data team id is never discarded merely because canonical team
    resolution is not yet possible (mirrors player resolution below).

    Selected positions and each of the five context collections
    (``CONTEXT_TYPES``) are replaced in place per ``(match_provider_id,
    team_provider_id)`` (context additionally scoped per ``context_type``):
    a row present in the new observation is upserted (preserving
    ``first_observed_at``, refreshing everything else including a
    re-resolved ``canonical_player_id`` so a crosswalk added after first
    observation self-heals on the next valid publish); a row no longer
    present is deleted. This is a current-state projection, not an
    append-only history -- see module docstring "Current-state, not
    append-only history". A selection/context record with no Champion Data
    player id at all (never observed in committed evidence, but structurally
    possible) is conservatively skipped rather than persisted under an
    invented identity.
    """
    if result.status is not RosterStatus.PUBLISHED:
        return RosterPersistenceSummary()

    selections_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in result.selections:
        match_provider_id = record.get("match_provider_id")
        team_provider_id = record.get("team_provider_id")
        if not match_provider_id or not team_provider_id:
            continue
        selections_by_key[(match_provider_id, team_provider_id)].append(record)

    rosters_written = 0
    selections_written = 0
    context_written = 0
    unmatched_matches: list[str] = []
    unmatched_teams: list[tuple[str, str]] = []

    for roster in result.rosters:
        match_provider_id = roster["match_provider_id"]
        round_provider_id = roster["round_provider_id"]
        match_id = resolve_canonical_match(conn, match_provider_id)
        if match_id is None:
            unmatched_matches.append(match_provider_id)
            continue
        for team in roster["teams"]:
            team_provider_id = team["team_provider_id"]
            side = team["side"]
            if not team_provider_id:
                unmatched_teams.append((match_provider_id, team_provider_id))
                continue
            canonical_team_id = resolve_canonical_team(conn, team_provider_id)
            if canonical_team_id is None:
                unmatched_teams.append((match_provider_id, team_provider_id))

            conn.execute(
                """INSERT INTO cfs_match_rosters(
                       match_id, match_provider_id, round_provider_id, team_provider_id,
                       canonical_team_id, side, team_status, match_status_at_observation,
                       source_last_updated, first_observed_at, last_observed_at,
                       collector_version, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(match_provider_id, team_provider_id) DO UPDATE SET
                       canonical_team_id=excluded.canonical_team_id,
                       side=excluded.side,
                       team_status=excluded.team_status,
                       match_status_at_observation=excluded.match_status_at_observation,
                       source_last_updated=excluded.source_last_updated,
                       last_observed_at=excluded.last_observed_at,
                       collector_version=excluded.collector_version,
                       updated_at=excluded.updated_at""",
                (
                    match_id, match_provider_id, round_provider_id, team_provider_id,
                    canonical_team_id, side, team["team_status"], roster["match_status"],
                    roster["provider_timestamp"], observed_at, observed_at,
                    collector_version, observed_at,
                ),
            )
            rosters_written += 1

            records = selections_by_key.get((match_provider_id, team_provider_id), [])
            selection_records = [
                item for item in records
                if item.get("record_kind") == "selection" and item.get("champion_data_player_id")
            ]
            selections_written += _replace_selections(
                conn, match_id=match_id, match_provider_id=match_provider_id,
                team_provider_id=team_provider_id, canonical_team_id=canonical_team_id,
                side=side, records=selection_records, observed_at=observed_at,
                collector_version=collector_version,
            )

            context_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in records:
                if item.get("record_kind") != "change" or not item.get("champion_data_player_id"):
                    continue
                collection = item.get("source_collection")
                if collection in CONTEXT_TYPES:
                    context_by_type[collection].append(item)
            for context_type in CONTEXT_TYPES:
                context_written += _replace_context(
                    conn, match_id=match_id, match_provider_id=match_provider_id,
                    team_provider_id=team_provider_id, canonical_team_id=canonical_team_id,
                    side=side, context_type=context_type,
                    records=context_by_type.get(context_type, []), observed_at=observed_at,
                    collector_version=collector_version,
                )

    return RosterPersistenceSummary(
        rosters_written=rosters_written, selections_written=selections_written,
        context_written=context_written, unmatched_matches=tuple(unmatched_matches),
        unmatched_teams=tuple(unmatched_teams),
    )


def _replace_selections(conn: sqlite3.Connection, *, match_id: int, match_provider_id: str,
                        team_provider_id: str, canonical_team_id: int | None, side: str,
                        records: list[dict[str, Any]], observed_at: str,
                        collector_version: str) -> int:
    seen: set[str] = set()
    written = 0
    for record in records:
        player_provider_id = record["champion_data_player_id"]
        seen.add(player_provider_id)
        canonical_player_id = resolve_canonical_player(conn, player_provider_id)
        source_order = record.get("source_order") or {}
        conn.execute(
            """INSERT INTO cfs_match_roster_selections(
                   match_id, match_provider_id, team_provider_id, canonical_team_id, side,
                   player_provider_id, canonical_player_id, position, jumper_number, captain,
                   group_order, player_order, first_observed_at, last_observed_at,
                   collector_version, updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(match_provider_id, team_provider_id, player_provider_id) DO UPDATE SET
                   canonical_team_id=excluded.canonical_team_id,
                   side=excluded.side,
                   canonical_player_id=excluded.canonical_player_id,
                   position=excluded.position,
                   jumper_number=excluded.jumper_number,
                   captain=excluded.captain,
                   group_order=excluded.group_order,
                   player_order=excluded.player_order,
                   last_observed_at=excluded.last_observed_at,
                   collector_version=excluded.collector_version,
                   updated_at=excluded.updated_at""",
            (
                match_id, match_provider_id, team_provider_id, canonical_team_id, side,
                player_provider_id, canonical_player_id, record.get("selection_state"),
                _coerce_int(record.get("jumper_number")), _coerce_bool(record.get("captain")),
                source_order.get("group"), source_order.get("player"), observed_at, observed_at,
                collector_version, observed_at,
            ),
        )
        written += 1
    _delete_stale(
        conn, table="cfs_match_roster_selections", match_provider_id=match_provider_id,
        team_provider_id=team_provider_id, seen=seen,
    )
    return written


def _replace_context(conn: sqlite3.Connection, *, match_id: int, match_provider_id: str,
                     team_provider_id: str, canonical_team_id: int | None, side: str,
                     context_type: str, records: list[dict[str, Any]], observed_at: str,
                     collector_version: str) -> int:
    seen: set[str] = set()
    written = 0
    for record in records:
        player_provider_id = record["champion_data_player_id"]
        seen.add(player_provider_id)
        canonical_player_id = resolve_canonical_player(conn, player_provider_id)
        source_order = record.get("source_order") or {}
        conn.execute(
            """INSERT INTO cfs_match_roster_context(
                   match_id, match_provider_id, team_provider_id, canonical_team_id, side,
                   context_type, player_provider_id, canonical_player_id, reason, player_order,
                   first_observed_at, last_observed_at, collector_version, updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(match_provider_id, team_provider_id, context_type, player_provider_id) DO UPDATE SET
                   canonical_team_id=excluded.canonical_team_id,
                   side=excluded.side,
                   canonical_player_id=excluded.canonical_player_id,
                   reason=excluded.reason,
                   player_order=excluded.player_order,
                   last_observed_at=excluded.last_observed_at,
                   collector_version=excluded.collector_version,
                   updated_at=excluded.updated_at""",
            (
                match_id, match_provider_id, team_provider_id, canonical_team_id, side,
                context_type, player_provider_id, canonical_player_id,
                record.get("reason"), source_order.get("record"), observed_at, observed_at,
                collector_version, observed_at,
            ),
        )
        written += 1
    _delete_stale(
        conn, table="cfs_match_roster_context", match_provider_id=match_provider_id,
        team_provider_id=team_provider_id, seen=seen, context_type=context_type,
    )
    return written


def _delete_stale(conn: sqlite3.Connection, *, table: str, match_provider_id: str,
                  team_provider_id: str, seen: set[str], context_type: str | None = None) -> None:
    clauses = ["match_provider_id=?", "team_provider_id=?"]
    params: list[Any] = [match_provider_id, team_provider_id]
    if context_type is not None:
        clauses.append("context_type=?")
        params.append(context_type)
    if seen:
        placeholders = ",".join("?" for _ in seen)
        clauses.append(f"player_provider_id NOT IN ({placeholders})")
        params.extend(sorted(seen))
    conn.execute(f"DELETE FROM {table} WHERE " + " AND ".join(clauses), params)


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _coerce_bool(value: Any) -> int | None:
    if not isinstance(value, bool):
        return None
    return int(value)


def current_roster_teams(conn: sqlite3.Connection, match_id: int) -> list[sqlite3.Row]:
    """Current per-team roster metadata for one match. Backs the consumer API."""
    return conn.execute(
        "SELECT * FROM cfs_match_rosters WHERE match_id=? ORDER BY side", (match_id,),
    ).fetchall()


def current_roster_selections(conn: sqlite3.Connection, match_id: int) -> list[sqlite3.Row]:
    """Current selected positional players for one match. Backs the consumer API."""
    return conn.execute(
        "SELECT * FROM cfs_match_roster_selections WHERE match_id=? "
        "ORDER BY side, group_order, player_order, player_provider_id",
        (match_id,),
    ).fetchall()


def current_roster_context(conn: sqlite3.Connection, match_id: int) -> list[sqlite3.Row]:
    """Current ins/outs/lateChanges/clubDebuts/milestones records for one match.

    Backs the consumer API. Never returned merged with selections -- see
    module docstring and ``docs/api_v1_rosters.md``.
    """
    return conn.execute(
        "SELECT * FROM cfs_match_roster_context WHERE match_id=? "
        "ORDER BY side, context_type, player_order, player_provider_id",
        (match_id,),
    ).fetchall()


def _normalise_rosters(payload: Any, round_provider_id: str) -> RosterCollectionResult:
    if payload is None:
        return RosterCollectionResult(round_provider_id, RosterStatus.UNAVAILABLE, [], [])
    if not isinstance(payload, list):
        raise AflJsonInvalidResponse(
            "Match-roster response is not a list or null", endpoint="match_rosters"
        )
    if not payload:
        # Live semantics for [] have not been verified. Keep it distinct but
        # non-destructive in compare_rosters until a provider meaning is known.
        return RosterCollectionResult(round_provider_id, RosterStatus.EMPTY, [], [])

    rosters: list[dict[str, Any]] = []
    selections: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for match_order, wrapper in enumerate(payload):
        if not isinstance(wrapper, dict):
            raise _invalid("Match-roster list contains a non-object wrapper")
        match = _required_object(wrapper, "match")
        match_roster = _required_object(wrapper, "matchRoster")
        venue = _optional_object(wrapper, "venue")
        for key in ("homeTeam", "awayTeam"):
            if not isinstance(match_roster.get(key), dict):
                raise _invalid(f"matchRoster.{key} is not an object")

        match_provider_id = _first(match_roster, "matchId") or _first(
            match, "providerId", "matchId"
        )
        afl_match_id = _first(match, "id", "aflMatchId")
        roster = {
            "round_provider_id": round_provider_id,
            "round_number": match_roster.get("roundNumber"),
            "match_provider_id": match_provider_id,
            "afl_match_id": afl_match_id,
            "competition_provider_id": match_roster.get("competitionId"),
            "match_status": match_roster.get("status"),
            "provider_timestamp": match_roster.get("lastUpdated"),
            "source_order": match_order,
            "match": deepcopy(match),
            "teams": [],
            "provider_fields": {
                "venue": deepcopy(venue),
                "weather": deepcopy(match_roster.get("weather")),
                "umpires": deepcopy(match_roster.get("umpires")),
                "operationHeader": deepcopy(match_roster.get("operationHeader")),
                "recentMatches": deepcopy(match_roster.get("recentMatches")),
                "recentMatchScores": deepcopy(wrapper.get("recentMatchScores")),
                # Kept once per wrapper while its relationship to positions is unresolved.
                "teamPlayers": deepcopy(wrapper.get("teamPlayers")),
                **_unknown(wrapper, {
                    "match", "matchRoster", "venue", "recentMatchScores", "teamPlayers"
                }),
                **_unknown(match_roster, {
                    "homeTeam", "awayTeam", "competitionId", "lastUpdated", "matchId",
                    "operationHeader", "recentMatches", "roundNumber", "status", "umpires",
                    "weather",
                }),
            },
        }
        rosters.append(roster)
        for team_order, (side, key) in enumerate((("home", "homeTeam"), ("away", "awayTeam"))):
            team = match_roster[key]
            team_record = _normalise_team(team, side, team_order)
            roster["teams"].append(team_record)
            for record in _team_records(
                team, side=side, team_order=team_order, match_order=match_order,
                round_provider_id=round_provider_id, round_number=match_roster.get("roundNumber"),
                match_provider_id=match_provider_id, afl_match_id=afl_match_id,
            ):
                identity = _selection_key(record)
                if identity not in seen:
                    seen.add(identity)
                    selections.append(record)
    selections.sort(key=_selection_key)
    states = {str(roster["match_status"]).casefold() for roster in rosters}
    unavailable = bool(states & {"unavailable", "unpublished", "not_published"})
    status = RosterStatus.UNAVAILABLE if unavailable else RosterStatus.PUBLISHED
    return RosterCollectionResult(
        round_provider_id, status, selections, rosters,
        next((str(item["match_status"]) for item in rosters if item["match_status"] is not None), None),
        next((item["provider_timestamp"] for item in rosters
              if isinstance(item["provider_timestamp"], str)), None),
    )


def _normalise_team(team: Mapping[str, Any], side: str, source_order: int) -> dict[str, Any]:
    optional_fields = (
        "clubDebuts", "ins", "lateChanges", "milestones", "outs", "positions"
    )
    return {
        "team_provider_id": team.get("teamId"),
        "match_provider_id": team.get("matchId"),
        "team_name": _player_name(team.get("teamName")),
        "team_status": team.get("teamStatus"),
        "side": side,
        "source_order": source_order,
        "provider_fields": {
            # Retain all verified context/change collections at team scope as
            # their non-player fields are not yet exhaustively understood.
            **{key: deepcopy(team[key]) for key in optional_fields
               if team.get(key) is not None},
            **_unknown(team, {
                "clubDebuts", "ins", "lateChanges", "matchId", "milestones", "outs",
                "positions", "teamId", "teamName", "teamStatus",
            }),
        },
    }


def _team_records(team: Mapping[str, Any], **context: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    positions = team.get("positions")
    # These provider fields are optional and only lists have verified record
    # semantics. Null is absent; objects and other shapes remain intact in the
    # team-scoped provider_fields assembled by _normalise_team.
    position_records = positions if isinstance(positions, list) else []
    for group_order, group in enumerate(position_records):
        if not isinstance(group, dict):
            raise _invalid("Team roster positions contains a non-object group")
        group_name = _first(group, "position", "positionName", "name", "type")
        players = group.get("players")
        if players is None and "player" in group:
            players = [group["player"]]
        if not isinstance(players, list):
            raise _invalid("Team roster position has no players list")
        for player_order, player_value in enumerate(players):
            player, record_fields = _unwrap_player(player_value)
            records.append(_normalise_player_record(
                player, record_kind="selection", source_collection="positions",
                selection_state=group_name, reason=None,
                source_order={"match": context["match_order"], "team": context["team_order"],
                              "group": group_order, "player": player_order},
                record_fields={**_unknown(group, {
                    "position", "positionName", "name", "type", "players", "player"
                }), **record_fields},
                team=team, **{key: value for key, value in context.items()
                              if key not in {"match_order", "team_order"}},
            ))
    for collection in ("ins", "outs", "lateChanges", "clubDebuts", "milestones"):
        values = team.get(collection)
        supported_records = values if isinstance(values, list) else []
        for record_order, value in enumerate(supported_records):
            player, record_fields = _unwrap_player(value)
            reason = value.get("reason") if isinstance(value, dict) else None
            records.append(_normalise_player_record(
                player, record_kind="change", source_collection=collection,
                selection_state=None, reason=reason,
                source_order={"match": context["match_order"], "team": context["team_order"],
                              "record": record_order},
                record_fields=record_fields, team=team,
                **{key: item for key, item in context.items()
                   if key not in {"match_order", "team_order"}},
            ))
    return records


def _unwrap_player(value: Any) -> tuple[Mapping[str, Any], dict[str, Any]]:
    if not isinstance(value, dict):
        raise _invalid("Team roster player record is not an object")
    player = value.get("player", value)
    if not isinstance(player, dict):
        raise _invalid("Team roster player is not an object")
    return player, _unknown(value, {"player", "reason"}) if player is not value else {}


def _normalise_player_record(player: Mapping[str, Any], *, team: Mapping[str, Any],
                             record_kind: str, source_collection: str,
                             selection_state: Any, reason: Any,
                             source_order: Mapping[str, int], record_fields: Mapping[str, Any],
                             **context: Any) -> dict[str, Any]:
    return {
        "round_provider_id": context["round_provider_id"],
        "round_number": context["round_number"],
        "match_provider_id": context["match_provider_id"],
        "afl_match_id": context["afl_match_id"],
        "team_provider_id": team.get("teamId"),
        "team_name": _player_name(team.get("teamName")),
        "team_side": context["side"],
        "champion_data_player_id": player.get("playerId"),
        "player_name": _player_name(player.get("playerName")),
        "jumper_number": player.get("playerJumperNumber"),
        "captain": player.get("captain"),
        "record_kind": record_kind,
        "source_collection": source_collection,
        "selection_state": selection_state,
        "reason": reason,
        "source_order": dict(source_order),
        "provider_fields": {
            **_unknown(player, {
                "playerId", "playerName", "captain", "playerJumperNumber"
            }),
            **deepcopy(dict(record_fields)),
        },
    }


def _selection_key(item: Mapping[str, Any]) -> tuple[str, ...]:
    player = item.get("champion_data_player_id")
    if player is None:
        player = item.get("player_name") or json.dumps({
            "jumper_number": item.get("jumper_number"),
            "provider_fields": item.get("provider_fields"),
            "reason": item.get("reason"),
        }, sort_keys=True, separators=(",", ":"), default=str)
    # A position name is mutable state rather than identity, so moves compare as changes.
    collection_identity = (item.get("source_collection")
                           if item.get("record_kind") == "change" else item.get("record_kind"))
    return tuple(str(value) for value in (
        item.get("match_provider_id") or item.get("afl_match_id"),
        item.get("team_provider_id"), player, collection_identity,
    ))


def _comparison_value(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return meaningful state without provider array-order diagnostics."""
    value = deepcopy(dict(item))
    value.pop("source_order", None)
    return value


def _required_object(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise _invalid(f"Match-roster wrapper {key} is not an object")
    return item


def _optional_object(value: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    item = value.get(key)
    if item is not None and not isinstance(item, dict):
        raise _invalid(f"Match-roster wrapper {key} is not an object or null")
    return item


def _player_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        parts = (value.get("givenName"), value.get("surname"))
        text = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
        return text or None
    return None


def _first(values: Mapping[str, Any], *keys: str) -> Any:
    return next((values[key] for key in keys if key in values and values[key] is not None), None)


def _unknown(values: Mapping[str, Any], known: set[str]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in values.items() if key not in known}


def _invalid(message: str) -> AflJsonInvalidResponse:
    return AflJsonInvalidResponse(message, endpoint="match_rosters")
