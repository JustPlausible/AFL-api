"""Versioned canonical read routes (``/api/v1``).

This module is strictly additive: it introduces the first canonical,
versioned consumer surface over authoritative ``cfs_player_stats`` and never
touches ``api/routes.py``'s unversioned compatibility routes, tables, or
response shapes. See
``docs/architecture/api/player_stats_api_design.md`` for the endpoint
API contract this module implements.
"""

from enum import Enum
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from afl_json.player_stats import CANONICAL_STAT_FIELDS
from afl_json.season_report import authoritative_stats_finality_for_match
from api.errors_v1 import ApplicationErrorResponse, application_error
from api_key_capabilities import ADVANCED_READ
from auth import AuthenticatedCredential, authenticate_api_key
from db.connection import get_db_connection
from utils.log import log

router = APIRouter()


class MatchSide(str, Enum):
    HOME = "home"
    AWAY = "away"


class MatchInfo(BaseModel):
    match_id: int
    match_provider_id: str | None
    round_id: int
    season_id: int | None
    status: str | None


FinalityStatus = Literal["final", "partial", "not_available"]


class Lifecycle(BaseModel):
    finality: FinalityStatus


class ResourceMetadata(BaseModel):
    source_updated_at: str | None


class PlayerAdvancedMetadata(BaseModel):
    snapshot_authority: int
    resolved_match_status: str | None
    collected_at: str


class FinalityEvidence(BaseModel):
    authoritative_rows: int
    authoritative_sides: int
    min_snapshot_authority: int | None
    max_snapshot_authority: int | None


class AdvancedMetadata(BaseModel):
    finality_evidence: FinalityEvidence


class PlayerStatValues(BaseModel):
    goals: int | float | None
    behinds: int | float | None
    kicks: int | float | None
    handballs: int | float | None
    disposals: int | float | None
    marks: int | float | None
    tackles: int | float | None
    hitouts: int | float | None


class PlayerStat(BaseModel):
    champion_data_player_id: str
    canonical_player_id: int | None
    afl_player_id: int | None
    display_name: str | None
    side: Literal["home", "away"]
    team_id: int | None
    stats: PlayerStatValues


class AdvancedPlayerStat(PlayerStat):
    advanced: PlayerAdvancedMetadata


class MatchPlayerStatsResponse(BaseModel):
    match: MatchInfo
    lifecycle: Lifecycle
    metadata: ResourceMetadata
    players: list[PlayerStat]


class AdvancedMatchPlayerStatsResponse(BaseModel):
    match: MatchInfo
    lifecycle: Lifecycle
    metadata: ResourceMetadata
    players: list[AdvancedPlayerStat]
    advanced: AdvancedMetadata


def _finality_status(finality) -> FinalityStatus:
    status: FinalityStatus
    if not finality.has_authoritative_snapshot:
        status = "not_available"
    elif finality.is_partial_authoritative_snapshot:
        status = "partial"
    else:
        status = "final"
    return status


def _finality_evidence(finality) -> FinalityEvidence:
    return FinalityEvidence(
        authoritative_rows=finality.authoritative_rows,
        authoritative_sides=finality.authoritative_sides,
        min_snapshot_authority=finality.min_authority,
        max_snapshot_authority=finality.max_authority,
    )


def _display_name(row) -> str | None:
    if row["display_name"]:
        return row["display_name"]
    parts = [part for part in (row["given_name"], row["family_name"]) if part]
    return " ".join(parts) if parts else None


@router.get(
    "/api/v1/matches/{match_id}/player-stats",
    response_model=MatchPlayerStatsResponse | AdvancedMatchPlayerStatsResponse,
    responses={
        403: {"model": ApplicationErrorResponse, "description": "Advanced capability required"},
        404: {"model": ApplicationErrorResponse, "description": "Match not found"},
    },
    summary="Get canonical player statistics for a match",
    description=(
        "Returns canonical player identity and AFL statistics with final, partial, or "
        "not_available lifecycle semantics. metadata.source_updated_at is the newest "
        "source observation represented by the returned (and therefore filtered) rows, "
        "or null when no rows are returned. Set advanced=true to add selected per-player "
        "provenance and match-level finality evidence; this requires the advanced-read "
        "API-key capability. Application 403 and 404 errors use the documented structured "
        "error response."
    ),
)
def get_match_player_stats(
    match_id: int,
    side: MatchSide | None = Query(None, description="Filter players by side (home or away)"),
    champion_data_player_id: str | None = Query(
        None, description="Filter to a single player by Champion Data player ID"
    ),
    advanced: bool = Query(
        False,
        description="Add selected provenance metadata (requires advanced-read capability)",
    ),
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
):
    log(f"📊 {credential.label} requested v1 player stats for match {match_id}", "INFO")
    if advanced and not credential.has_capability(ADVANCED_READ):
        return application_error(
            403,
            "advanced_access_required",
            "This API key does not permit access to advanced metadata.",
        )
    conn = get_db_connection()
    try:
        match_row = conn.execute(
            "SELECT match_id, match_provider_id, round_id, season_id, status "
            "FROM matches WHERE match_id = ?",
            (match_id,),
        ).fetchone()

        if not match_row:
            log(f"❌ No match found with Match ID: {match_id}", "WARN")
            return application_error(404, "match_not_found", "Match not found.")

        match_provider_id = match_row["match_provider_id"]
        match_payload = MatchInfo(
            match_id=match_row["match_id"],
            match_provider_id=match_provider_id,
            round_id=match_row["round_id"],
            season_id=match_row["season_id"],
            status=match_row["status"],
        )
        finality = authoritative_stats_finality_for_match(conn, match_provider_id)
        lifecycle_payload = Lifecycle(finality=_finality_status(finality))

        if not match_provider_id:
            response_values = dict(
                match=match_payload,
                lifecycle=lifecycle_payload,
                metadata=ResourceMetadata(source_updated_at=None),
                players=[],
            )
            if advanced:
                return AdvancedMatchPlayerStatsResponse(
                    **response_values,
                    advanced=AdvancedMetadata(finality_evidence=_finality_evidence(finality)),
                )
            return MatchPlayerStatsResponse(**response_values)

        filters = ["s.match_provider_id = ?"]
        values: list[object] = [match_provider_id]
        if side is not None:
            filters.append("s.side = ?")
            values.append(side.value)
        if champion_data_player_id is not None:
            filters.append("s.champion_data_player_id = ?")
            values.append(champion_data_player_id)

        stat_columns = ", ".join(f"s.{name}" for name in CANONICAL_STAT_FIELDS)
        query = (
            "SELECT s.champion_data_player_id, s.canonical_player_id, s.side, "
            f"{stat_columns}, "
            "s.snapshot_authority, s.resolved_match_status, s.collected_at, "
            "cp.display_name, cp.given_name, cp.family_name, "
            "afl_pp.provider_player_id AS afl_player_id, "
            "CASE s.side WHEN 'home' THEN m.home_team_id WHEN 'away' THEN m.away_team_id END AS team_id "
            "FROM cfs_player_stats s "
            "JOIN matches m ON m.match_provider_id = s.match_provider_id "
            "LEFT JOIN canonical_players cp ON cp.id = s.canonical_player_id "
            "LEFT JOIN player_provider_ids afl_pp "
            "ON afl_pp.provider = 'afl' AND afl_pp.player_id = s.canonical_player_id "
            f"WHERE {' AND '.join(filters)} "
            "ORDER BY s.side, s.champion_data_player_id"
        )
        rows = conn.execute(query, tuple(values)).fetchall()
    finally:
        conn.close()

    player_values = [
        dict(
            champion_data_player_id=row["champion_data_player_id"],
            canonical_player_id=row["canonical_player_id"],
            afl_player_id=(
                int(row["afl_player_id"]) if row["afl_player_id"] is not None else None
            ),
            display_name=_display_name(row),
            side=row["side"],
            team_id=row["team_id"],
            stats=PlayerStatValues(**{name: row[name] for name in CANONICAL_STAT_FIELDS}),
        )
        for row in rows
    ]
    source_updated_at = max((row["collected_at"] for row in rows), default=None)
    response_values = dict(
        match=match_payload,
        lifecycle=lifecycle_payload,
        metadata=ResourceMetadata(source_updated_at=source_updated_at),
    )
    if advanced:
        players = [
            AdvancedPlayerStat(
                **values,
                advanced=PlayerAdvancedMetadata(
                    snapshot_authority=row["snapshot_authority"],
                    resolved_match_status=row["resolved_match_status"],
                    collected_at=row["collected_at"],
                ),
            )
            for values, row in zip(player_values, rows)
        ]
        return AdvancedMatchPlayerStatsResponse(
            **response_values,
            players=players,
            advanced=AdvancedMetadata(finality_evidence=_finality_evidence(finality)),
        )
    return MatchPlayerStatsResponse(
        **response_values,
        players=[PlayerStat(**values) for values in player_values],
    )
