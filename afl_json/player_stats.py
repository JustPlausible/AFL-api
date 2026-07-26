"""Collect, validate and persist canonical CFS match player statistics."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from .client import AflJsonClient, AflJsonInvalidResponse, AflJsonResourceUnavailable
from .collectors import RawResponseWriter

ENDPOINT_NAME = "match_player_statistics"
SOURCE_ENDPOINT = "/cfs/afl/playerStats/match/{matchProviderId}"

# Observed source names are deliberately centralised. Adding a confirmed BBBFL
# field should require changing this mapping, the record, and persistence only.
CANONICAL_STAT_FIELDS: Mapping[str, str] = {
    "goals": "goals", "behinds": "behinds", "kicks": "kicks",
    "handballs": "handballs", "disposals": "disposals", "marks": "marks",
    "tackles": "tackles", "hitouts": "hitouts",
}


class PlayerStatsStatus(str, Enum):
    UNAVAILABLE = "unavailable"
    EMPTY = "empty"
    LIVE_PARTIAL = "live_partial"
    CONCLUDED = "concluded"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PlayerStatDiagnostic:
    severity: str
    code: str
    message: str
    player_id: str | None = None
    field: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalPlayerStat:
    match_provider_id: str
    champion_data_player_id: str
    side: str
    collected_at: str
    source_endpoint: str
    source_status: str | None
    afl_match_id: int | str | None
    team_provider_id: str | None
    goals: int | Decimal | None
    behinds: int | Decimal | None
    kicks: int | Decimal | None
    handballs: int | Decimal | None
    disposals: int | Decimal | None
    marks: int | Decimal | None
    tackles: int | Decimal | None
    hitouts: int | Decimal | None
    extra_stats: dict[str, Any]
    raw_player: dict[str, Any]

    @property
    def natural_key(self) -> tuple[str, str]:
        return self.match_provider_id, self.champion_data_player_id


@dataclass(frozen=True, slots=True)
class PlayerStatsCollectionResult:
    match_provider_id: str
    status: PlayerStatsStatus
    records: list[CanonicalPlayerStat]
    diagnostics: list[PlayerStatDiagnostic]
    collected_at: str
    source_endpoint: str = SOURCE_ENDPOINT
    source_status: str | None = None
    rejected_records: int = 0


class MatchPlayerStatsCollector:
    def __init__(self, client: AflJsonClient, *, raw_directory: str | Path | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.client = client
        self.raw_writer = RawResponseWriter(raw_directory) if raw_directory is not None else None
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def collect(self, match_provider_id: str, *, afl_match_id: int | str | None = None,
                source_status: str | None = None) -> PlayerStatsCollectionResult:
        if not isinstance(match_provider_id, str) or not match_provider_id.strip():
            raise ValueError("match_provider_id is required")
        match_provider_id = match_provider_id.strip()
        collected_at = self.clock().astimezone(timezone.utc).isoformat()
        try:
            response = self.client.get(
                ENDPOINT_NAME, path_parameters={"match_provider_id": match_provider_id}
            )
        except AflJsonResourceUnavailable:
            return PlayerStatsCollectionResult(match_provider_id, PlayerStatsStatus.UNAVAILABLE,
                                                [], [], collected_at)
        if self.raw_writer:
            self.raw_writer.write(ENDPOINT_NAME, response.data,
                                  scope={"matchProviderId": match_provider_id}, page=1)
        return normalise_player_stats(response.data, match_provider_id,
                                      collected_at=collected_at,
                                      afl_match_id=afl_match_id,
                                      source_status=source_status)


def normalise_player_stats(payload: Any, match_provider_id: str, *, collected_at: str,
                           afl_match_id: int | str | None = None,
                           source_status: str | None = None) -> PlayerStatsCollectionResult:
    if payload is None:
        return PlayerStatsCollectionResult(match_provider_id, PlayerStatsStatus.UNAVAILABLE,
                                            [], [], collected_at, source_status=source_status)
    if not isinstance(payload, dict):
        raise _invalid("Player-stat response is not an object or null")
    array_keys = ("homeTeamPlayerStats", "awayTeamPlayerStats")
    present = [key for key in array_keys if key in payload]
    if not present:
        # Observed unpublished error-like bodies and future metadata-only bodies
        # must not be confused with authentication; retain null/known publication states.
        publication = _first(payload, "status", "matchStatus", "matchPhase")
        if _is_unavailable(publication):
            return PlayerStatsCollectionResult(match_provider_id, PlayerStatsStatus.UNAVAILABLE,
                                                [], [], collected_at,
                                                source_status=_text(publication) or source_status)
        raise _invalid("Player-stat response has no recognised team arrays")
    diagnostics: list[PlayerStatDiagnostic] = []
    if len(present) == 1:
        diagnostics.append(PlayerStatDiagnostic(
            "warning", "missing_team_array",
            f"Only {present[0]} is currently published for match {match_provider_id}",
        ))
    for key in present:
        if payload[key] is not None and not isinstance(payload[key], list):
            raise _invalid(f"{key} is not a list or null")

    explicit_status = (_text(_first(payload, "status", "matchStatus", "matchPhase"))
                       or source_status)
    records: list[CanonicalPlayerStat] = []
    rejected = 0
    seen: dict[str, str] = {}
    for side, key in (("home", array_keys[0]), ("away", array_keys[1])):
        values = payload.get(key)
        if values is None:
            if key in present:
                diagnostics.append(PlayerStatDiagnostic(
                    "warning", "null_team_array", f"{key} is currently null for match {match_provider_id}"
                ))
            continue
        for index, entry in enumerate(values):
            if not isinstance(entry, dict):
                rejected += 1
                diagnostics.append(PlayerStatDiagnostic(
                    "error", "malformed_player", f"{key}[{index}] is not an object"
                ))
                continue
            record, record_diagnostics = _normalise_entry(
                entry, side=side, match_provider_id=match_provider_id,
                collected_at=collected_at, afl_match_id=afl_match_id,
                source_status=explicit_status,
            )
            diagnostics.extend(record_diagnostics)
            if record is None:
                rejected += 1
                continue
            previous_side = seen.get(record.champion_data_player_id)
            if previous_side is not None:
                code = "player_on_both_sides" if previous_side != side else "duplicate_player_id"
                diagnostics.append(PlayerStatDiagnostic(
                    "error", code,
                    f"Player {record.champion_data_player_id} appears more than once in match "
                    f"{match_provider_id} ({previous_side}, {side})",
                    record.champion_data_player_id,
                ))
                rejected += 1
                continue
            seen[record.champion_data_player_id] = side
            records.append(record)

    status = _result_status(explicit_status, records, len(present) == 1)
    return PlayerStatsCollectionResult(match_provider_id, status, records, diagnostics,
                                       collected_at, source_status=explicit_status,
                                       rejected_records=rejected)


def _normalise_entry(entry: Mapping[str, Any], *, side: str, match_provider_id: str,
                     collected_at: str, afl_match_id: int | str | None,
                     source_status: str | None):
    diagnostics: list[PlayerStatDiagnostic] = []
    player_stats = entry.get("playerStats")
    stats = player_stats.get("stats") if isinstance(player_stats, dict) else None
    if stats is None:
        stats = {}
    if not isinstance(stats, dict):
        return None, [PlayerStatDiagnostic("error", "malformed_player",
            f"Player-stat entry has non-object playerStats.stats in match {match_provider_id}")]
    player_context = entry.get("player")
    player_id = _deep_player_id(player_context)
    if player_id is None:
        return None, [PlayerStatDiagnostic("error", "missing_player_id",
            f"Player-stat entry is missing Champion Data player ID in match {match_provider_id}")]
    team_id = _deep_first(player_context, "teamId")
    mapped: dict[str, int | Decimal | None] = {}
    for canonical, source in CANONICAL_STAT_FIELDS.items():
        if source not in stats:
            mapped[canonical] = None
            continue
        try:
            mapped[canonical] = _number(stats[source])
        except ValueError as exc:
            mapped[canonical] = None
            diagnostics.append(PlayerStatDiagnostic(
                "error", "invalid_numeric",
                f"Invalid statistic for match {match_provider_id}, player {player_id}, "
                f"field {source}: {exc}", player_id, source,
            ))
    extra = {key: deepcopy(value) for key, value in stats.items()
             if key not in CANONICAL_STAT_FIELDS.values()}
    return CanonicalPlayerStat(
        match_provider_id=match_provider_id, champion_data_player_id=player_id,
        side=side, collected_at=collected_at, source_endpoint=SOURCE_ENDPOINT,
        source_status=source_status, afl_match_id=afl_match_id,
        team_provider_id=_text(team_id), extra_stats=extra,
        raw_player=deepcopy(dict(entry)), **mapped,
    ), diagnostics


def _number(value: Any) -> int | Decimal:
    if isinstance(value, bool) or value is None or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"expected a number, got {type(value).__name__}")
    if isinstance(value, str) and not value.strip():
        raise ValueError("empty strings are not numbers")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{value!r} is not numeric") from None
    if not number.is_finite():
        raise ValueError(f"{value!r} is not finite")
    return int(number) if number == number.to_integral_value() else number


def upsert_player_stats(conn: sqlite3.Connection, result: PlayerStatsCollectionResult) -> int:
    """Upsert current observations without allowing stale/live data over a final snapshot."""
    if result.status in {PlayerStatsStatus.UNAVAILABLE, PlayerStatsStatus.EMPTY}:
        return 0
    authority = 2 if result.status is PlayerStatsStatus.CONCLUDED else 1
    written = 0
    for record in result.records:
        values = asdict(record)
        numeric = [_sqlite_number(values[name]) for name in CANONICAL_STAT_FIELDS]
        cursor = conn.execute(f"""
            INSERT INTO cfs_player_stats (
                match_provider_id, champion_data_player_id, afl_match_id, team_provider_id,
                side, collected_at, source_endpoint, source_status, snapshot_authority,
                {', '.join(CANONICAL_STAT_FIELDS)}, extra_stats_json, raw_player_json
            ) VALUES ({', '.join('?' for _ in range(19))})
            ON CONFLICT(match_provider_id, champion_data_player_id) DO UPDATE SET
                afl_match_id=excluded.afl_match_id, team_provider_id=excluded.team_provider_id,
                side=excluded.side, collected_at=excluded.collected_at,
                source_endpoint=excluded.source_endpoint, source_status=excluded.source_status,
                snapshot_authority=excluded.snapshot_authority,
                {', '.join(f'{name}=excluded.{name}' for name in CANONICAL_STAT_FIELDS)},
                extra_stats_json=excluded.extra_stats_json, raw_player_json=excluded.raw_player_json
            WHERE excluded.snapshot_authority > cfs_player_stats.snapshot_authority
               OR (excluded.snapshot_authority = cfs_player_stats.snapshot_authority
                   AND excluded.collected_at >= cfs_player_stats.collected_at)
        """, (record.match_provider_id, record.champion_data_player_id,
              None if record.afl_match_id is None else str(record.afl_match_id),
              record.team_provider_id, record.side, record.collected_at,
              record.source_endpoint, record.source_status, authority, *numeric,
              json.dumps(record.extra_stats, separators=(",", ":"), sort_keys=True),
              json.dumps(record.raw_player, separators=(",", ":"), sort_keys=True)))
        written += cursor.rowcount
    return written


def _sqlite_number(value: Any) -> int | str | None:
    return str(value) if isinstance(value, Decimal) else value


def _deep_player_id(value: Any) -> str | None:
    current = value
    # The observed payload wraps identity as player.player.player; tolerate
    # fewer wrappers without assigning meanings to unrelated source fields.
    for _ in range(5):
        if not isinstance(current, dict):
            return None
        player_id = _text(current.get("playerId"))
        if player_id:
            return player_id
        current = current.get("player")
    return None


def _deep_first(value: Any, key: str) -> Any:
    current = value
    for _ in range(5):
        if not isinstance(current, dict):
            return None
        if current.get(key) is not None:
            return current[key]
        current = current.get("player")
    return None


def _first(value: Mapping[str, Any], *keys: str) -> Any:
    return next((value[key] for key in keys if value.get(key) is not None), None)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_unavailable(status: Any) -> bool:
    return _text(status).casefold() in {"unavailable", "unpublished", "not_published"} if _text(status) else False


def _result_status(source_status: str | None, records: list[Any], one_team: bool) -> PlayerStatsStatus:
    folded = source_status.casefold() if source_status else ""
    if folded in {"concluded", "completed", "final"}:
        return PlayerStatsStatus.CONCLUDED
    if not records:
        return PlayerStatsStatus.EMPTY
    if one_team or folded in {"live", "in_progress", "playing", "partial"}:
        return PlayerStatsStatus.LIVE_PARTIAL
    return PlayerStatsStatus.UNKNOWN


def _invalid(message: str) -> AflJsonInvalidResponse:
    return AflJsonInvalidResponse(message, endpoint=ENDPOINT_NAME)
