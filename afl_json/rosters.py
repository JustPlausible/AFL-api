"""Collection and conservative normalisation of verified CFS match rosters."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .client import AflJsonClient, AflJsonInvalidResponse, AflJsonResourceUnavailable
from .collectors import RawResponseWriter


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
        if before[key] == after[key]:
            unchanged.append(after[key])
        else:
            changed.append({"before": before[key], "after": after[key]})
    return RosterChanges(added, removed, changed, unchanged, True)


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
    player = item.get("champion_data_player_id") or item.get("player_name")
    fallback_order = item.get("source_order") if player is None else None
    # A position name is mutable state rather than identity, so moves compare as changes.
    collection_identity = (item.get("source_collection")
                           if item.get("record_kind") == "change" else item.get("record_kind"))
    return tuple(str(value) for value in (
        item.get("match_provider_id") or item.get("afl_match_id"),
        item.get("team_provider_id"), player, collection_identity,
        json.dumps(fallback_order, sort_keys=True),
    ))


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
