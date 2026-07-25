"""Collection and conservative normalisation of CFS match rosters.

The nested provider schema is only partly verified.  Known identity and
selection fields are promoted while unknown fields stay on the narrowest
match, team, or player object in ``provider_fields``.
"""

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

        payload = response.data
        if self.raw_writer:
            self.raw_writer.write(
                "match_rosters", payload, scope={"roundProviderId": round_provider_id}, page=1
            )
        return _normalise_rosters(payload, round_provider_id)


def compare_rosters(previous: RosterCollectionResult,
                    current: RosterCollectionResult) -> RosterChanges:
    """Compare deterministic selection snapshots without treating absence as deletion."""
    replacement_safe = current.status in {RosterStatus.PUBLISHED, RosterStatus.EMPTY}
    if not replacement_safe:
        return RosterChanges([], [], [], [], False)
    before = {_selection_key(item): item for item in previous.selections}
    after = {_selection_key(item): item for item in current.selections}
    added = [after[key] for key in sorted(after.keys() - before.keys())]
    removed = [before[key] for key in sorted(before.keys() - after.keys())]
    changed, unchanged = [], []
    for key in sorted(before.keys() & after.keys()):
        (unchanged if before[key] == after[key] else changed).append(
            after[key] if before[key] == after[key] else {"before": before[key], "after": after[key]}
        )
    return RosterChanges(added, removed, changed, unchanged, True)


def _normalise_rosters(payload: Any, round_provider_id: str) -> RosterCollectionResult:
    if not isinstance(payload, dict):
        raise AflJsonInvalidResponse("Match-roster response is not an object", endpoint="match_rosters")
    collection = payload.get("matchRosters", payload.get("matches"))
    if collection is None:
        # Explicit provider publication signals are valid even before a collection exists.
        if _is_unavailable(payload):
            return _result(round_provider_id, RosterStatus.UNAVAILABLE, [], [], payload)
        raise AflJsonInvalidResponse(
            "Match-roster response has no matchRosters or matches collection",
            endpoint="match_rosters",
        )
    if not isinstance(collection, list):
        raise AflJsonInvalidResponse(
            "Match-roster collection is not a list", endpoint="match_rosters"
        )

    selections: list[dict[str, Any]] = []
    rosters: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for match_index, match in enumerate(collection):
        if not isinstance(match, dict):
            raise AflJsonInvalidResponse(
                "Match-roster collection contains a non-object match", endpoint="match_rosters"
            )
        teams = match.get("teams", match.get("teamRosters"))
        if not isinstance(teams, list):
            raise AflJsonInvalidResponse(
                "Match-roster match has no teams collection", endpoint="match_rosters"
            )
        match_fields = _unknown(match, {
            "matchId", "providerId", "aflMatchId", "id", "teams", "teamRosters",
            "published", "publicationState", "updatedAt", "timestamp", "version",
        })
        roster = {
            "round_provider_id": round_provider_id,
            "match_provider_id": _first(match, "matchId", "providerId"),
            "afl_match_id": _first(match, "aflMatchId", "id"),
            "published": match.get("published"),
            "publication_state": match.get("publicationState"),
            "provider_timestamp": _first(match, "updatedAt", "timestamp"),
            "provider_version": match.get("version"),
            "source_order": match_index,
            "provider_fields": match_fields,
            "teams": [],
        }
        rosters.append(roster)
        for team_index, team in enumerate(teams):
            if not isinstance(team, dict):
                raise AflJsonInvalidResponse(
                    "Match-roster teams collection contains a non-object", endpoint="match_rosters"
                )
            groups = _player_groups(team)
            team_fields = _unknown(team, {
                "teamId", "providerId", "aflTeamId", "id", "teamName", "name",
                "teamAbbr", "abbreviation", "side", *groups.keys(),
            })
            roster["teams"].append({
                "team_provider_id": _first(team, "teamId", "providerId"),
                "afl_team_id": _first(team, "aflTeamId", "id"),
                "team_name": _text(_first(team, "teamName", "name")),
                "team_abbreviation": _text(_first(team, "teamAbbr", "abbreviation")),
                "team_side": team.get("side"),
                "source_order": team_index,
                "provider_fields": deepcopy(team_fields),
            })
            for group_name, players in groups.items():
                for player_index, player in enumerate(players):
                    if not isinstance(player, dict):
                        raise AflJsonInvalidResponse(
                            "Match-roster player collection contains a non-object",
                            endpoint="match_rosters",
                        )
                    selection = _normalise_selection(
                        round_provider_id, match, team, player, match_index, team_index,
                        player_index, group_name,
                    )
                    key = _selection_key(selection)
                    if key not in seen:
                        seen.add(key)
                        selections.append(selection)
    selections.sort(key=_selection_key)
    unavailable = _is_unavailable(payload) or (rosters and all(
        item["published"] is False for item in rosters
    ))
    status = (RosterStatus.UNAVAILABLE if unavailable else
              RosterStatus.PUBLISHED if selections else RosterStatus.EMPTY)
    return _result(round_provider_id, status, selections, rosters, payload)


def _result(round_id: str, status: RosterStatus, selections: list[dict[str, Any]],
            rosters: list[dict[str, Any]], payload: Mapping[str, Any]) -> RosterCollectionResult:
    return RosterCollectionResult(
        round_id, status, selections, rosters,
        _text(_first(payload, "publicationState", "status")),
        _text(_first(payload, "updatedAt", "timestamp")),
        _first(payload, "version", "revision"),
    )


def _player_groups(team: Mapping[str, Any]) -> dict[str, list[Any]]:
    groups = {}
    for name in ("players", "namedPlayers", "squad", "interchange", "emergencies"):
        if name in team:
            if not isinstance(team[name], list):
                raise AflJsonInvalidResponse(
                    f"Match-roster {name} collection is not a list", endpoint="match_rosters"
                )
            groups[name] = team[name]
    return groups


def _normalise_selection(round_id: str, match: Mapping[str, Any], team: Mapping[str, Any],
                         player: Mapping[str, Any], match_order: int, team_order: int,
                         player_order: int, group: str) -> dict[str, Any]:
    known = {
        "playerId", "championDataPlayerId", "aflPlayerId", "aflId", "playerName", "name",
        "jumperNumber", "named", "isNamed", "emergency", "isEmergency", "selectionState",
        "position", "side", "group", "updatedAt", "timestamp", "version",
    }
    emergency = _first(player, "emergency", "isEmergency")
    named = _first(player, "named", "isNamed")
    return {
        "round_provider_id": round_id,
        "match_provider_id": _first(match, "matchId", "providerId"),
        "afl_match_id": _first(match, "aflMatchId", "id"),
        "team_provider_id": _first(team, "teamId", "providerId"),
        "afl_team_id": _first(team, "aflTeamId", "id"),
        "champion_data_player_id": _first(player, "playerId", "championDataPlayerId"),
        "afl_player_id": _first(player, "aflPlayerId", "aflId"),
        "player_name": _text(_first(player, "playerName", "name")),
        "team_name": _text(_first(team, "teamName", "name")),
        "team_abbreviation": _text(_first(team, "teamAbbr", "abbreviation")),
        "jumper_number": player.get("jumperNumber"),
        "named": named,
        "emergency": True if group == "emergencies" and emergency is None else emergency,
        "selection_state": _first(player, "selectionState", "position", "side"),
        "selection_group": group,
        "team_side": team.get("side"),
        "source_order": {"match": match_order, "team": team_order, "player": player_order},
        "provider_timestamp": _first(player, "updatedAt", "timestamp"),
        "provider_version": player.get("version"),
        "provider_fields": deepcopy(_unknown(player, known)),
    }


def _selection_key(item: Mapping[str, Any]) -> tuple[str, ...]:
    # IDs are preferred.  Name and source position are deterministic fallbacks
    # for investigative fixtures where provider identifiers are absent.
    player = item.get("champion_data_player_id") or item.get("afl_player_id") or item.get("player_name")
    position = item.get("source_order") if player is None else None
    return tuple(str(value) for value in (
        item.get("match_provider_id") or item.get("afl_match_id"),
        item.get("team_provider_id") or item.get("afl_team_id"), player,
        item.get("selection_group"), json.dumps(position, sort_keys=True),
    ))


def _is_unavailable(payload: Mapping[str, Any]) -> bool:
    if payload.get("published") is False or payload.get("available") is False:
        return True
    state = _text(_first(payload, "publicationState", "status"))
    return state is not None and state.casefold() in {"unavailable", "unpublished", "not_published"}


def _first(values: Mapping[str, Any], *keys: str) -> Any:
    return next((values[key] for key in keys if key in values and values[key] is not None), None)


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _unknown(values: Mapping[str, Any], known: set[str]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in values.items() if key not in known}
