"""Versioned canonical read routes (``/api/v1``).

This module is strictly additive: it introduces the first canonical,
versioned consumer surface over authoritative ``cfs_player_stats`` and never
touches ``api/routes.py``'s unversioned compatibility routes, tables, or
response shapes. See
``docs/architecture/api/player_stats_api_design.md`` for the endpoint
API contract this module implements.
"""

import json
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
from version import __version__

router = APIRouter()


class ApiDiscoveryResponse(BaseModel):
    """Stable, deliberately minimal entry point for consumer discovery."""

    name: str
    version: str
    documentation: str


class Season(BaseModel):
    """Public projection of one persisted canonical AFL season."""

    season_id: int
    year: int
    name: str
    is_current: bool
    current_round_number: int | None


class SeasonsResponse(BaseModel):
    seasons: list[Season]


class ByeTeam(BaseModel):
    """Canonical identity for a team known to have a bye."""

    team_id: int
    name: str | None
    abbreviation: str | None


class Round(BaseModel):
    """Stable public projection of one persisted AFL round."""

    round_id: int
    season_id: int
    round_number: int | None
    name: str | None
    abbreviation: str | None
    start_time: str | None
    end_time: str | None
    byes: list[ByeTeam] | None


class RoundsResponse(BaseModel):
    rounds: list[Round]


def _round_from_row(row, teams: dict[int, tuple[str | None, str | None]]) -> Round:
    """Project reviewed bye identities without leaking the stored provider payload."""
    raw_byes = row["byes_json"]
    byes: list[ByeTeam] | None = None
    if raw_byes is not None:
        try:
            stored = json.loads(raw_byes)
        except (TypeError, json.JSONDecodeError):
            stored = None
        if isinstance(stored, list):
            byes = []
            seen: set[int] = set()
            for value in stored:
                team_id = value.get("id") if isinstance(value, dict) else None
                if isinstance(team_id, int) and not isinstance(team_id, bool) and team_id not in seen:
                    seen.add(team_id)
                    name, abbreviation = teams.get(team_id, (None, None))
                    byes.append(ByeTeam(team_id=team_id, name=name, abbreviation=abbreviation))

    return Round(
        round_id=row["round_id"], season_id=row["season_id"],
        round_number=row["round_number"], name=row["round_label"],
        abbreviation=row["abbreviation"], start_time=row["start_time"],
        end_time=row["end_time"], byes=byes,
    )


@router.get(
    "/api/v1",
    response_model=ApiDiscoveryResponse,
    summary="Discover the AFL-api v1 consumer API",
    description=(
        "Authenticated discovery entry point containing only the public API name, "
        "version, and generated documentation location. Use the seasons resource "
        "as the first step in the consumer navigation hierarchy."
    ),
)
def get_api_discovery(
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> ApiDiscoveryResponse:
    log(f"🧭 {credential.label} requested v1 API discovery", "INFO")
    return ApiDiscoveryResponse(name="AFL-api", version=__version__, documentation="/docs")


@router.get(
    "/api/v1/seasons",
    response_model=SeasonsResponse,
    summary="List canonical AFL seasons",
    description=(
        "Returns persisted AFL seasons in descending year order. season_id is the "
        "numeric AFL season identifier. is_current and current_round_number are read "
        "directly from season-sync persistence and are not independently calculated. "
        "Provider payloads and internal metadata are never exposed."
    ),
)
def get_seasons(
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> SeasonsResponse:
    log(f"📅 {credential.label} requested v1 seasons", "INFO")
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT afl_id, year, name, is_current, current_round_number "
            "FROM afl_seasons ORDER BY year DESC, afl_id DESC"
        ).fetchall()
    finally:
        conn.close()

    return SeasonsResponse(
        seasons=[
            Season(
                season_id=row["afl_id"],
                year=row["year"],
                name=row["name"],
                is_current=bool(row["is_current"]),
                current_round_number=row["current_round_number"],
            )
            for row in rows
        ]
    )


def _team_projection(conn) -> dict[int, tuple[str | None, str | None]]:
    return {
        row["afl_id"]: (row["name"], row["abbreviation"])
        for row in conn.execute("SELECT afl_id, name, abbreviation FROM afl_teams")
    }


@router.get(
    "/api/v1/seasons/{season_id}/rounds",
    response_model=RoundsResponse,
    summary="List canonical rounds for a season",
    description=(
        "Returns rounds using their persisted season relationship, ordered by round_number "
        "ascending and round_id ascending. byes is a typed list of canonical team identities; "
        "an empty list means the source explicitly reported no byes, while null means bye "
        "information was unavailable or could not be safely interpreted."
    ),
)
def get_season_rounds(
    season_id: int,
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> RoundsResponse:
    log(f"📅 {credential.label} requested v1 rounds for season {season_id}", "INFO")
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT round_id, round_label, season_id, round_number, abbreviation, "
            "start_time, end_time, byes_json FROM rounds WHERE season_id = ? "
            "ORDER BY round_number ASC, round_id ASC",
            (season_id,),
        ).fetchall()
        teams = _team_projection(conn)
    finally:
        conn.close()
    return RoundsResponse(rounds=[_round_from_row(row, teams) for row in rows])


@router.get(
    "/api/v1/rounds/{round_id}",
    response_model=Round,
    responses={404: {"model": ApplicationErrorResponse, "description": "Round not found"}},
    summary="Get a canonical round",
    description=(
        "Returns stable persisted round facts and typed canonical bye-team identities. "
        "Unknown round identifiers use the shared structured v1 error response."
    ),
)
def get_round(
    round_id: int,
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
):
    log(f"🔍 {credential.label} requested v1 round {round_id}", "INFO")
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT round_id, round_label, season_id, round_number, abbreviation, "
            "start_time, end_time, byes_json FROM rounds WHERE round_id = ?",
            (round_id,),
        ).fetchone()
        if row is None:
            return application_error(404, "round_not_found", "Round not found.")
        teams = _team_projection(conn)
    finally:
        conn.close()
    return _round_from_row(row, teams)


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
