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
from .match_status import MatchLifecycle, later_match_status, normalise_match_status

ENDPOINT_NAME = "match_player_statistics"
SOURCE_ENDPOINT = "/cfs/afl/playerStats/match/{matchProviderId}"

# Observed source names are deliberately centralised. Adding a confirmed
# canonical AFL field should require changing this mapping, the record, and
# persistence only.
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
    endpoint_source_status: str | None
    resolved_match_status: str | None
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
    endpoint_source_status: str | None = None
    resolved_match_status: str | None = None
    rejected_records: int = 0

    @property
    def source_status(self) -> str | None:
        """Compatibility alias; this value always came from the endpoint."""
        return self.endpoint_source_status


class MatchPlayerStatsCollector:
    def __init__(self, client: AflJsonClient, *, raw_directory: str | Path | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.client = client
        self.raw_writer = RawResponseWriter(raw_directory) if raw_directory is not None else None
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def collect(self, match_provider_id: str, *, afl_match_id: int | str | None = None,
                canonical_match_status: str | None = None,
                source_status: str | None = None) -> PlayerStatsCollectionResult:
        """Collect stats, using canonical match metadata only when the endpoint is silent.

        ``source_status`` is retained as a compatibility spelling for callers
        that previously supplied match metadata under that ambiguous name.
        """
        if not isinstance(match_provider_id, str) or not match_provider_id.strip():
            raise ValueError("match_provider_id is required")
        match_provider_id = match_provider_id.strip()
        if canonical_match_status is not None and source_status is not None:
            raise ValueError("supply canonical_match_status or source_status, not both")
        resolved_match_status = canonical_match_status or source_status
        collected_at = self.clock().astimezone(timezone.utc).isoformat()
        try:
            response = self.client.get(
                ENDPOINT_NAME, path_parameters={"match_provider_id": match_provider_id}
            )
        except AflJsonResourceUnavailable:
            return PlayerStatsCollectionResult(match_provider_id, PlayerStatsStatus.UNAVAILABLE,
                                                [], [], collected_at,
                                                resolved_match_status=resolved_match_status)
        if self.raw_writer:
            self.raw_writer.write(ENDPOINT_NAME, response.data,
                                  scope={"matchProviderId": match_provider_id}, page=1)
        return normalise_player_stats(response.data, match_provider_id,
                                      collected_at=collected_at,
                                      afl_match_id=afl_match_id,
                                      canonical_match_status=resolved_match_status)


def normalise_player_stats(payload: Any, match_provider_id: str, *, collected_at: str,
                           afl_match_id: int | str | None = None,
                           canonical_match_status: str | None = None,
                           source_status: str | None = None) -> PlayerStatsCollectionResult:
    if canonical_match_status is not None and source_status is not None:
        raise ValueError("supply canonical_match_status or source_status, not both")
    resolved_match_status = _text(canonical_match_status or source_status)
    if payload is None:
        return PlayerStatsCollectionResult(match_provider_id, PlayerStatsStatus.UNAVAILABLE,
                                            [], [], collected_at,
                                            resolved_match_status=resolved_match_status)
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
                                                endpoint_source_status=_text(publication),
                                                resolved_match_status=resolved_match_status)
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

    endpoint_source_status = _text(_first(payload, "status", "matchStatus", "matchPhase"))
    endpoint_lifecycle = normalise_match_status(endpoint_source_status)
    canonical_lifecycle = normalise_match_status(resolved_match_status)
    if endpoint_source_status and endpoint_lifecycle is None:
        diagnostics.append(PlayerStatDiagnostic(
            "warning", "unrecognised_endpoint_status",
            f"Player-stat endpoint status {endpoint_source_status!r} is not recognised",
        ))
    if (endpoint_lifecycle and canonical_lifecycle
            and MatchLifecycle[endpoint_lifecycle] < MatchLifecycle[canonical_lifecycle]):
        diagnostics.append(PlayerStatDiagnostic(
            "warning", "endpoint_status_regression",
            f"Ignored endpoint status {endpoint_source_status!r}; reconciled match status "
            f"{resolved_match_status!r} is later for match {match_provider_id}",
        ))
    effective_status = later_match_status(canonical_lifecycle, endpoint_lifecycle)
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
                endpoint_source_status=endpoint_source_status,
                resolved_match_status=effective_status,
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

    status = _result_status(effective_status, records, len(present) == 1)
    return PlayerStatsCollectionResult(match_provider_id, status, records, diagnostics,
                                       collected_at,
                                       endpoint_source_status=endpoint_source_status,
                                       resolved_match_status=effective_status,
                                       rejected_records=rejected)


def _normalise_entry(entry: Mapping[str, Any], *, side: str, match_provider_id: str,
                     collected_at: str, afl_match_id: int | str | None,
                     endpoint_source_status: str | None,
                     resolved_match_status: str | None):
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
        endpoint_source_status=endpoint_source_status,
        resolved_match_status=resolved_match_status, afl_match_id=afl_match_id,
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
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    stat_columns = {row[1] for row in conn.execute("PRAGMA table_info(cfs_player_stats)")}
    supports_canonical_link = ("player_provider_ids" in tables
                               and "canonical_player_id" in stat_columns)
    written = 0
    for record in result.records:
        values = asdict(record)
        numeric = [_sqlite_number(values[name]) for name in CANONICAL_STAT_FIELDS]
        mapped_player = (conn.execute(
            "SELECT player_id FROM player_provider_ids "
            "WHERE provider='champion_data' AND provider_player_id=?",
            (record.champion_data_player_id,),
        ).fetchone() if supports_canonical_link else None)
        canonical_player_id = mapped_player[0] if mapped_player else None
        # Collection time alone does not make a snapshot new information. A
        # repeated response should therefore be a zero-write idempotent run,
        # while a newer same-authority response with any changed source or
        # canonical value remains eligible for update.
        changed_columns = (
            "afl_match_id", "team_provider_id", "side", "source_endpoint",
            "endpoint_source_status", "resolved_match_status",
            *CANONICAL_STAT_FIELDS, "extra_stats_json", "raw_player_json",
        )
        if supports_canonical_link:
            changed_columns = (*changed_columns, "canonical_player_id")
        meaningful_change = " OR ".join(
            f"excluded.{name} IS NOT cfs_player_stats.{name}" for name in changed_columns
        )
        canonical_column = "canonical_player_id," if supports_canonical_link else ""
        canonical_assignment = ("canonical_player_id=excluded.canonical_player_id,"
                                if supports_canonical_link else "")
        parameters = [record.match_provider_id, record.champion_data_player_id,
                      None if record.afl_match_id is None else str(record.afl_match_id),
                      record.team_provider_id]
        if supports_canonical_link:
            parameters.append(canonical_player_id)
        parameters.extend((record.side, record.collected_at, record.source_endpoint,
                           record.endpoint_source_status, record.resolved_match_status,
                           authority, *numeric,
                           json.dumps(record.extra_stats, separators=(",", ":"), sort_keys=True),
                           json.dumps(record.raw_player, separators=(",", ":"), sort_keys=True)))
        cursor = conn.execute(f"""
            INSERT INTO cfs_player_stats (
                match_provider_id, champion_data_player_id, afl_match_id, team_provider_id,
                {canonical_column}
                side, collected_at, source_endpoint, endpoint_source_status,
                resolved_match_status, snapshot_authority,
                {', '.join(CANONICAL_STAT_FIELDS)}, extra_stats_json, raw_player_json
            ) VALUES ({', '.join('?' for _ in parameters)})
            ON CONFLICT(match_provider_id, champion_data_player_id) DO UPDATE SET
                afl_match_id=excluded.afl_match_id, team_provider_id=excluded.team_provider_id,
                {canonical_assignment}
                side=excluded.side, collected_at=excluded.collected_at,
                source_endpoint=excluded.source_endpoint,
                endpoint_source_status=excluded.endpoint_source_status,
                resolved_match_status=excluded.resolved_match_status,
                snapshot_authority=excluded.snapshot_authority,
                {', '.join(f'{name}=excluded.{name}' for name in CANONICAL_STAT_FIELDS)},
                extra_stats_json=excluded.extra_stats_json, raw_player_json=excluded.raw_player_json
            WHERE excluded.snapshot_authority > cfs_player_stats.snapshot_authority
               OR (excluded.snapshot_authority = cfs_player_stats.snapshot_authority
                   AND excluded.collected_at >= cfs_player_stats.collected_at
                   AND ({meaningful_change}))
        """, parameters)
        written += cursor.rowcount
    return written


def resolve_canonical_match_status(conn: sqlite3.Connection, *,
                                   match_provider_id: str | None = None,
                                   afl_match_id: int | str | None = None) -> str | None:
    """Resolve canonical match status by either stable match identifier."""
    if not match_provider_id and afl_match_id is None:
        raise ValueError("match_provider_id or afl_match_id is required")
    clauses: list[str] = []
    parameters: list[Any] = []
    if match_provider_id:
        clauses.append("match_provider_id = ?")
        parameters.append(match_provider_id)
    if afl_match_id is not None:
        clauses.append("match_id = ?")
        parameters.append(afl_match_id)
    try:
        row = conn.execute(
            f"SELECT status FROM matches WHERE {' OR '.join(clauses)} "
            "ORDER BY CASE WHEN match_provider_id = ? THEN 0 ELSE 1 END LIMIT 1",
            (*parameters, match_provider_id),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return None
        raise
    return _text(row[0]) if row else None


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
    if one_team or folded in {"live", "in_progress", "playing", "partial", "postgame"}:
        return PlayerStatsStatus.LIVE_PARTIAL
    return PlayerStatsStatus.UNKNOWN


def _status_class(value: str) -> PlayerStatsStatus:
    """Classify status text without using record presence as evidence."""
    return _result_status(value, [object()], False)


def _invalid(message: str) -> AflJsonInvalidResponse:
    return AflJsonInvalidResponse(message, endpoint=ENDPOINT_NAME)
