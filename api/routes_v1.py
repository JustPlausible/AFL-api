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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from afl_json.player_stats import CANONICAL_STAT_FIELDS
from afl_json.season_report import authoritative_stats_finality_for_match
from api.errors_v1 import ApplicationErrorResponse, application_error
from api_key_capabilities import ADVANCED_READ
from auth import AuthenticatedCredential, authenticate_api_key
from db.connection import get_db_connection
from utils.log import log
from version import __version__

router = APIRouter()

# SQLite integers are signed 64-bit; a value outside this range raises
# OverflowError when bound as a query parameter, so query filters against an
# INTEGER column must be rejected with a standard 422 before reaching the DB.
_SQLITE_INTEGER_MIN = -(2**63)
_SQLITE_INTEGER_MAX = 2**63 - 1


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


class MatchTeam(BaseModel):
    """Minimal canonical team identity for one side of a match."""

    team_id: int = Field(description="Canonical AFL team identifier.")
    name: str | None = Field(
        description="Canonical persisted team name, or null when unavailable."
    )


class Match(BaseModel):
    """Stable public projection of one persisted canonical AFL match."""

    match_id: int = Field(description="Canonical match identifier used by player-stats.")
    round_id: int
    season_id: int | None
    status: str | None = Field(description="Persisted match lifecycle status.")
    start_time_utc: str | None = Field(
        description="Persisted scheduled start in UTC, or null when unknown."
    )
    home_team: MatchTeam | None = Field(
        description="Canonical home-team identity, or null when it cannot be resolved."
    )
    away_team: MatchTeam | None = Field(
        description="Canonical away-team identity, or null when it cannot be resolved."
    )
    score_home: int | None = Field(description="Persisted home score, or null when unavailable.")
    score_away: int | None = Field(description="Persisted away score, or null when unavailable.")


class MatchesResponse(BaseModel):
    matches: list[Match]


def _match_from_row(row) -> Match:
    """Project only reviewed canonical fields from a match/team join."""
    return Match(
        match_id=row["match_id"],
        round_id=row["round_id"],
        season_id=row["season_id"],
        status=row["status"],
        start_time_utc=row["start_time_utc"],
        home_team=(
            MatchTeam(team_id=row["canonical_home_team_id"], name=row["home_team_name"])
            if row["canonical_home_team_id"] is not None
            else None
        ),
        away_team=(
            MatchTeam(team_id=row["canonical_away_team_id"], name=row["away_team_name"])
            if row["canonical_away_team_id"] is not None
            else None
        ),
        score_home=row["score_home"],
        score_away=row["score_away"],
    )


_MATCH_SELECT = (
    "SELECT m.match_id, m.round_id, m.season_id, m.status, m.start_time_utc, "
    "m.score_home, m.score_away, ht.afl_id AS canonical_home_team_id, "
    "ht.name AS home_team_name, at.afl_id AS canonical_away_team_id, "
    "at.name AS away_team_name FROM matches m "
    "LEFT JOIN afl_teams ht ON ht.afl_id = m.home_team_id "
    "LEFT JOIN afl_teams at ON at.afl_id = m.away_team_id "
)


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
            if not stored:
                byes = []
            else:
                resolved: list[ByeTeam] = []
                seen: set[int] = set()
                complete = True
                for value in stored:
                    team_id = value.get("id") if isinstance(value, dict) else None
                    if not isinstance(team_id, int) or isinstance(team_id, bool):
                        complete = False
                        continue
                    if team_id not in seen:
                        seen.add(team_id)
                        name, abbreviation = teams.get(team_id, (None, None))
                        resolved.append(
                            ByeTeam(team_id=team_id, name=name, abbreviation=abbreviation)
                        )
                # Never present a partial projection as a complete source bye list.
                if complete:
                    byes = resolved

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
    responses={404: {"model": ApplicationErrorResponse, "description": "Season not found"}},
    summary="List canonical rounds for a season",
    description=(
        "Returns rounds using their persisted season relationship, with numbered rounds "
        "ordered by round_number and round_id ascending and unknown numbers last. byes is a "
        "typed list of canonical team identities; "
        "an empty list means the source explicitly reported no byes, while null means bye "
        "information was unavailable or could not be safely interpreted."
    ),
)
def get_season_rounds(
    season_id: int,
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> RoundsResponse | JSONResponse:
    log(f"📅 {credential.label} requested v1 rounds for season {season_id}", "INFO")
    conn = get_db_connection()
    try:
        season = conn.execute(
            "SELECT afl_id FROM afl_seasons WHERE afl_id = ?", (season_id,)
        ).fetchone()
        if season is None:
            return application_error(404, "season_not_found", "Season not found.")
        rows = conn.execute(
            "SELECT round_id, round_label, season_id, round_number, abbreviation, "
            "start_time, end_time, byes_json FROM rounds WHERE season_id = ? "
            "ORDER BY round_number IS NULL ASC, round_number ASC, round_id ASC",
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
) -> Round | JSONResponse:
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


@router.get(
    "/api/v1/rounds/{round_id}/matches",
    response_model=MatchesResponse,
    responses={404: {"model": ApplicationErrorResponse, "description": "Round not found"}},
    summary="List canonical matches for a round",
    description=(
        "Returns persisted matches belonging to an existing canonical round. Known UTC "
        "start times sort first in ascending order, followed by unknown times; match_id "
        "is the stable tie-breaker. Team identities resolve only through canonical "
        "afl_teams persistence. A valid round without matches returns an empty collection."
    ),
)
def get_round_matches(
    round_id: int,
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> MatchesResponse | JSONResponse:
    log(f"📦 {credential.label} requested v1 matches for round {round_id}", "INFO")
    conn = get_db_connection()
    try:
        round_row = conn.execute(
            "SELECT round_id FROM rounds WHERE round_id = ?", (round_id,)
        ).fetchone()
        if round_row is None:
            return application_error(404, "round_not_found", "Round not found.")
        rows = conn.execute(
            _MATCH_SELECT
            + "WHERE m.round_id = ? "
            "ORDER BY m.start_time_utc IS NULL ASC, m.start_time_utc ASC, m.match_id ASC",
            (round_id,),
        ).fetchall()
    finally:
        conn.close()
    return MatchesResponse(matches=[_match_from_row(row) for row in rows])


@router.get(
    "/api/v1/matches/{match_id}",
    response_model=Match,
    responses={404: {"model": ApplicationErrorResponse, "description": "Match not found"}},
    summary="Get a canonical match",
    description=(
        "Returns the stable canonical projection of one persisted match. match_id is the "
        "same identifier accepted by the match player-stats resource. Raw provider and "
        "collector payloads are not part of this contract."
    ),
)
def get_match(
    match_id: int,
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> Match | JSONResponse:
    log(f"🔍 {credential.label} requested v1 match {match_id}", "INFO")
    conn = get_db_connection()
    try:
        row = conn.execute(_MATCH_SELECT + "WHERE m.match_id = ?", (match_id,)).fetchone()
        if row is None:
            return application_error(404, "match_not_found", "Match not found.")
    finally:
        conn.close()
    return _match_from_row(row)


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
        "or null when no rows are returned. canonical_player_id and champion_data_player_id "
        "each filter directly against the value persisted on the stat row -- neither is "
        "resolved from the other. Supplying both is conjunctive (AND): rows must match both "
        "persisted values, so identifiers naming different players deterministically return "
        "an empty players list rather than an error, consistent with any other filter that "
        "matches no rows. A row with an unresolved (null) canonical_player_id never matches "
        "the canonical filter but remains reachable via champion_data_player_id. Set "
        "advanced=true to add selected per-player provenance and match-level finality "
        "evidence; this requires the advanced-read API-key capability. Application 403 and "
        "404 errors use the documented structured error response."
    ),
)
def get_match_player_stats(
    match_id: int,
    side: MatchSide | None = Query(None, description="Filter players by side (home or away)"),
    canonical_player_id: int | None = Query(
        None,
        ge=_SQLITE_INTEGER_MIN,
        le=_SQLITE_INTEGER_MAX,
        description=(
            "Filter to a single player by canonical AFL-api player ID "
            "(cfs_player_stats.canonical_player_id). Matches only rows with that "
            "persisted canonical identity; a row with an unresolved (null) canonical "
            "identity never matches, and is not inferred."
        ),
    ),
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
        if canonical_player_id is not None:
            filters.append("s.canonical_player_id = ?")
            values.append(canonical_player_id)
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


class CommentaryPlayer(BaseModel):
    """Minimal player identity attached to one commentary event."""

    id: int = Field(description="Canonical AFL-api player identifier.")
    name: str | None = Field(description="Canonical display name, or null when not yet resolved.")
    provider_id: str | None = Field(description="Source Champion Data player identifier.")


class CommentaryTeam(BaseModel):
    """Minimal team identity attached to one commentary event."""

    id: int = Field(description="Canonical AFL-api team identifier.")
    name: str | None = Field(description="Canonical persisted team name, or null when unavailable.")
    provider_id: str | None = Field(description="Source Champion Data team identifier.")


class CommentaryEvent(BaseModel):
    """One normalized production commentary event."""

    id: int = Field(description="Stable AFL-api-generated event identifier. Not a Champion Data id.")
    match_id: int
    period_number: int | None = Field(description="0 for pre-match commentary, 1-4 for regulation quarters.")
    period_seconds: int | None = Field(description="Elapsed seconds within the period.")
    comment: str | None = Field(
        description=(
            "Original commentary text exactly as supplied by the source feed, or null when the "
            "source event omitted or malformed the comment field."
        )
    )
    score_event: bool | None = Field(
        description="Source scoreEvent flag exactly as supplied. Never inferred from comment text."
    )
    player: CommentaryPlayer | None = Field(
        description="Linked player identity, or null when the source event has no playerId or it is unresolved."
    )
    team: CommentaryTeam | None = Field(
        description="Linked team identity, or null when the source event has no teamId or it is unresolved."
    )
    observed_at: str = Field(description="UTC time AFL-api first observed this event in the source feed.")
    possible_edit_of_event_id: int | None = Field(
        description=(
            "Heuristic, non-authoritative link to an earlier event this one likely republishes or "
            "revises (e.g. a same-slot scoring-outcome change), based on a shared match-clock/player/"
            "team/score_event combination. Both events remain in the response; this link never causes "
            "the earlier event to be hidden, merged, or removed. Null when no such link was detected."
        )
    )


class MatchCommentaryResponse(BaseModel):
    match: MatchInfo
    events: list[CommentaryEvent]


def _commentary_name_lookups(
    conn, rows: list[dict],
) -> tuple[dict[int, str | None], dict[int, str | None]]:
    player_ids = {row["canonical_player_id"] for row in rows if row["canonical_player_id"] is not None}
    team_ids = {row["canonical_team_id"] for row in rows if row["canonical_team_id"] is not None}
    player_names: dict[int, str | None] = {}
    if player_ids:
        placeholders = ",".join("?" for _ in player_ids)
        for prow in conn.execute(
            f"SELECT id, display_name, given_name, family_name FROM canonical_players WHERE id IN ({placeholders})",
            tuple(player_ids),
        ):
            player_names[prow["id"]] = _display_name(prow)
    team_names: dict[int, str | None] = {}
    if team_ids:
        placeholders = ",".join("?" for _ in team_ids)
        for trow in conn.execute(
            f"SELECT afl_id, name FROM afl_teams WHERE afl_id IN ({placeholders})", tuple(team_ids),
        ):
            team_names[trow["afl_id"]] = trow["name"]
    return player_names, team_names


def _commentary_event_from_row(row: dict, player_names: dict, team_names: dict) -> CommentaryEvent:
    player = None
    if row["canonical_player_id"] is not None:
        player = CommentaryPlayer(
            id=row["canonical_player_id"], name=player_names.get(row["canonical_player_id"]),
            provider_id=row["player_provider_id"],
        )
    team = None
    if row["canonical_team_id"] is not None:
        team = CommentaryTeam(
            id=row["canonical_team_id"], name=team_names.get(row["canonical_team_id"]),
            provider_id=row["team_provider_id"],
        )
    return CommentaryEvent(
        id=row["id"], match_id=row["match_id"], period_number=row["period_number"],
        period_seconds=row["period_seconds"], comment=row["comment"], score_event=row["score_event"],
        player=player, team=team, observed_at=row["first_observed_at"],
        possible_edit_of_event_id=row["possible_edit_of_event_id"],
    )


@router.get(
    "/api/v1/matches/{match_id}/commentary",
    response_model=MatchCommentaryResponse,
    responses={404: {"model": ApplicationErrorResponse, "description": "Match not found"}},
    summary="Get production commentary events for a match",
    description=(
        "Returns normalized CFS commentaryFeed events for one canonical match, backed by production "
        "persistence (Issue #201) rather than the separate commentary diagnostic evidence tables. "
        "Ordering is chronological (period_number then period_seconds ascending) for consumer "
        "usability, even though the upstream feed itself is observed newest-first. player and team "
        "are null when the source event carries no playerId/teamId, or when that provider id has no "
        "known canonical crosswalk yet -- never guessed from the comment text. score_event and comment "
        "are persisted exactly as supplied by the source; no goal/behind/points outcome is parsed from "
        "prose. Commentary is not authoritative for match finality, lifecycle, or player statistics. "
        "A valid match with no commentary yet returns an empty events collection."
    ),
)
def get_match_commentary(
    match_id: int,
    period: int | None = Query(None, description="Filter to one period_number (0 for pre-match)."),
    player_id: int | None = Query(None, description="Filter to one canonical player identifier."),
    team_id: int | None = Query(None, description="Filter to one canonical team identifier."),
    score_events_only: bool = Query(False, description="Return only events with score_event=true."),
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> MatchCommentaryResponse | JSONResponse:
    log(f"💬 {credential.label} requested v1 commentary for match {match_id}", "INFO")
    from afl_json.match_commentary import event_rows

    conn = get_db_connection()
    try:
        match_row = conn.execute(
            "SELECT match_id, match_provider_id, round_id, season_id, status FROM matches WHERE match_id = ?",
            (match_id,),
        ).fetchone()
        if match_row is None:
            return application_error(404, "match_not_found", "Match not found.")

        rows = event_rows(
            conn, match_id=match_id, period_number=period, canonical_player_id=player_id,
            canonical_team_id=team_id, score_events_only=score_events_only,
        )
        player_names, team_names = _commentary_name_lookups(conn, rows)
    finally:
        conn.close()

    return MatchCommentaryResponse(
        match=MatchInfo(
            match_id=match_row["match_id"], match_provider_id=match_row["match_provider_id"],
            round_id=match_row["round_id"], season_id=match_row["season_id"], status=match_row["status"],
        ),
        events=[_commentary_event_from_row(row, player_names, team_names) for row in rows],
    )


class PlayerIdentifiers(BaseModel):
    """Known provider-ID crosswalks for one canonical player."""

    afl_player_id: int | None = Field(
        description="Numeric AFL player identifier, or null when no crosswalk exists."
    )
    champion_data_player_id: str | None = Field(
        description="Opaque Champion Data player identifier, or null when no crosswalk exists."
    )


class CanonicalPlayer(BaseModel):
    """Stable public projection of one persisted canonical player."""

    canonical_player_id: int = Field(
        description="Stable internal canonical player identifier; the primary consumer identity."
    )
    display_name: str | None = Field(
        description="Canonical display name, or null when not yet resolved."
    )
    current_team: MatchTeam | None = Field(
        description=(
            "Canonical team identity for the player's current-season membership, or null "
            "when no current season exists, the player has no membership row for it, or "
            "that membership's team is unresolved."
        )
    )
    identifiers: PlayerIdentifiers = Field(
        description="Known provider-ID crosswalks. Unresolved mappings are null, never guessed."
    )


class PlayerResponse(BaseModel):
    player: CanonicalPlayer


def _identifiers(conn, canonical_player_id: int) -> PlayerIdentifiers:
    """Resolve known provider-ID crosswalks for one canonical player.

    Unresolved mappings stay ``null`` rather than being guessed or inferred
    from the other identifier.
    """
    providers = {
        provider_row["provider"]: provider_row["provider_player_id"]
        for provider_row in conn.execute(
            "SELECT provider, provider_player_id FROM player_provider_ids WHERE player_id = ?",
            (canonical_player_id,),
        ).fetchall()
    }
    afl_player_id = providers.get("afl")
    return PlayerIdentifiers(
        afl_player_id=int(afl_player_id) if afl_player_id is not None else None,
        champion_data_player_id=providers.get("champion_data"),
    )


def _current_team(conn, canonical_player_id: int) -> MatchTeam | None:
    """Resolve a player's team for the current season, if cleanly resolvable.

    Joins the player's own membership rows to current seasons directly,
    rather than picking one global "current" season up front, so a player
    is not missed merely because a different season also has
    ``is_current = 1`` (e.g. under a second configured competition).
    """
    membership_row = conn.execute(
        "SELECT csp.team_id, t.name FROM competition_season_players csp "
        "JOIN afl_seasons s ON s.afl_id = csp.competition_season_id AND s.is_current = 1 "
        "LEFT JOIN afl_teams t ON t.afl_id = csp.team_id "
        "WHERE csp.player_id = ? "
        "ORDER BY s.year DESC, s.afl_id DESC LIMIT 1",
        (canonical_player_id,),
    ).fetchone()
    if membership_row is None or membership_row["team_id"] is None:
        return None
    return MatchTeam(team_id=membership_row["team_id"], name=membership_row["name"])


@router.get(
    "/api/v1/players/{canonical_player_id}",
    response_model=PlayerResponse,
    responses={404: {"model": ApplicationErrorResponse, "description": "Player not found"}},
    summary="Get a canonical player",
    description=(
        "Returns the stable canonical projection of one persisted player, resolved from "
        "canonical player and provider-identity persistence rather than the legacy players "
        "table. canonical_player_id is the primary consumer identity; identifiers exposes "
        "known AFL and Champion Data crosswalks, which are null rather than guessed when "
        "unresolved. current_team reflects the player's competition-season membership for "
        "the current season only, and is null when that cannot be resolved cleanly."
    ),
)
def get_player(
    canonical_player_id: int,
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> PlayerResponse | JSONResponse:
    log(f"🔍 {credential.label} requested v1 player {canonical_player_id}", "INFO")
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id, display_name, given_name, family_name FROM canonical_players WHERE id = ?",
            (canonical_player_id,),
        ).fetchone()
        if row is None:
            return application_error(404, "player_not_found", "Player not found.")

        identifiers = _identifiers(conn, canonical_player_id)
        current_team = _current_team(conn, canonical_player_id)
    finally:
        conn.close()

    return PlayerResponse(
        player=CanonicalPlayer(
            canonical_player_id=row["id"],
            display_name=_display_name(row),
            current_team=current_team,
            identifiers=identifiers,
        )
    )


class PlayerSeasonMembership(BaseModel):
    """One persisted competition-season membership for a canonical player."""

    season_id: int = Field(description="Canonical AFL season identifier, as returned by GET /api/v1/seasons.")
    year: int = Field(description="Season year, for display and chronological sorting.")
    name: str = Field(description="Persisted season name.")
    team: MatchTeam | None = Field(
        description=(
            "Canonical team identity the player was associated with for this season, or null "
            "when the membership row has no resolved team. This reflects exactly the team "
            "persisted for this season and is never carried over from an adjacent season."
        )
    )


class PlayerSeasonsResponse(BaseModel):
    canonical_player_id: int = Field(description="The canonical player these memberships belong to.")
    seasons: list[PlayerSeasonMembership] = Field(
        description=(
            "Every persisted competition-season membership for this player, most recent season "
            "first (year then season_id, both descending). A player with no known membership in "
            "any season returns an empty list, not an error."
        )
    )


def _season_memberships(conn, canonical_player_id: int) -> list[PlayerSeasonMembership]:
    """Resolve every persisted season membership for a player, oldest history preserved.

    Each row is scoped to its own ``competition_season_players`` season, so an
    earlier season's team is never overwritten or inferred from a later one --
    e.g. a player moving from Team A (2025, 2026) to Team B (2027) keeps 2025
    and 2026 reporting Team A.
    """
    rows = conn.execute(
        "SELECT csp.competition_season_id AS season_id, s.year, s.name, "
        "csp.team_id, t.name AS team_name "
        "FROM competition_season_players csp "
        "JOIN afl_seasons s ON s.afl_id = csp.competition_season_id "
        "LEFT JOIN afl_teams t ON t.afl_id = csp.team_id "
        "WHERE csp.player_id = ? "
        "ORDER BY s.year DESC, csp.competition_season_id DESC",
        (canonical_player_id,),
    ).fetchall()
    return [
        PlayerSeasonMembership(
            season_id=row["season_id"],
            year=row["year"],
            name=row["name"],
            team=(
                MatchTeam(team_id=row["team_id"], name=row["team_name"])
                if row["team_id"] is not None
                else None
            ),
        )
        for row in rows
    ]


@router.get(
    "/api/v1/players/{canonical_player_id}/seasons",
    response_model=PlayerSeasonsResponse,
    responses={404: {"model": ApplicationErrorResponse, "description": "Player not found"}},
    summary="List a canonical player's season/team memberships",
    description=(
        "Returns every persisted competition-season membership for one canonical player, most "
        "recent season first. Each entry reports the team associated with the player for that "
        "specific season only -- a later club change never rewrites an earlier season's team, "
        "and a season with no resolved team returns team: null. Use season_id with GET "
        "/api/v1/seasons/{season_id}/rounds to navigate to that season's matches, and the "
        "player's champion_data_player_id (from GET /api/v1/players/{canonical_player_id}) to "
        "filter GET /api/v1/matches/{match_id}/player-stats for that season's statistics. A "
        "player with no known season membership returns 200 with an empty seasons list."
    ),
)
def get_player_seasons(
    canonical_player_id: int,
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> PlayerSeasonsResponse | JSONResponse:
    log(f"📅 {credential.label} requested v1 season memberships for player {canonical_player_id}", "INFO")
    conn = get_db_connection()
    try:
        player_row = conn.execute(
            "SELECT id FROM canonical_players WHERE id = ?", (canonical_player_id,)
        ).fetchone()
        if player_row is None:
            return application_error(404, "player_not_found", "Player not found.")
        seasons = _season_memberships(conn, canonical_player_id)
    finally:
        conn.close()

    return PlayerSeasonsResponse(canonical_player_id=canonical_player_id, seasons=seasons)


class PlayersResponse(BaseModel):
    players: list[CanonicalPlayer]


MAX_PLAYER_SEARCH_RESULTS = 100


@router.get(
    "/api/v1/players",
    response_model=PlayersResponse,
    responses={
        422: {
            "model": ApplicationErrorResponse,
            "description": "Missing or blank search parameter",
        }
    },
    summary="Search canonical players by name",
    description=(
        "Resolves canonical player identities by human-readable name, so a consumer "
        "can discover a canonical_player_id without already knowing it. Matching is "
        "case-insensitive and partial against each player's resolved display name "
        "(canonical_players.display_name, falling back to given_name/family_name — "
        "the same fallback used by the single-player resource), with no fuzzy, "
        "phonetic, or provider-ID inference. Results are ordered by display name "
        "then canonical_player_id for determinism, and capped at "
        f"{MAX_PLAYER_SEARCH_RESULTS} rows. The `search` query parameter is required "
        "and must be non-blank; an unfiltered player collection is out of scope for "
        "this resource, so a missing or blank value returns a structured 422 rather "
        "than every canonical player. A valid search with no matches returns 200 "
        "with an empty players collection."
    ),
)
def search_players(
    search: str = Query(
        ...,
        description=(
            "Case-insensitive, partial-match name search. Required; a missing or "
            "blank value is rejected rather than returning the full player collection."
        ),
    ),
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> PlayersResponse | JSONResponse:
    log(f"🔍 {credential.label} searched v1 players for {search!r}", "INFO")
    query = search.strip().lower()
    if not query:
        return application_error(
            422,
            "search_required",
            "A non-blank search query parameter is required.",
        )

    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, display_name, given_name, family_name FROM canonical_players"
        ).fetchall()

        matches = []
        for row in rows:
            name = _display_name(row)
            if name is not None and query in name.lower():
                matches.append((name, row))
        matches.sort(key=lambda item: (item[0].lower(), item[1]["id"]))
        matches = matches[:MAX_PLAYER_SEARCH_RESULTS]

        players = [
            CanonicalPlayer(
                canonical_player_id=row["id"],
                display_name=name,
                current_team=_current_team(conn, row["id"]),
                identifiers=_identifiers(conn, row["id"]),
            )
            for name, row in matches
        ]
    finally:
        conn.close()

    return PlayersResponse(players=players)


InterchangeSide = Literal["home", "away"]


class InterchangeStatus(BaseModel):
    """Current per-player CFS matchInterchange state (Issue #204).

    Promotion of the Issue #193 diagnostic investigation, confirmed against
    real Round 24 live diagnostic evidence (7 matches, plus an
    individually-cited per-poll export for one match) reviewed on PR #206 --
    see ``on_bench``'s description and ``afl_json.match_interchange`` module
    docstring for the evidence and its one residual caveat (CONCLUDED
    behaviour).
    """

    champion_data_player_id: str = Field(description="Source Champion Data player identifier. Always present.")
    canonical_player_id: int | None = Field(
        description="Canonical AFL-api player id, or null when this Champion Data id has no known crosswalk yet."
    )
    display_name: str | None = Field(description="Canonical display name, or null when unresolved.")
    side: InterchangeSide = Field(description="Which team's interchange array this player was last observed in.")
    team_id: int | None = Field(description="Canonical AFL-api team id, or null when unresolved.")
    champion_data_team_id: str | None = Field(description="Source Champion Data team identifier.")
    on_bench: bool = Field(
        description=(
            "Whether this player is currently on the interchange bench (off the ground), as of the most "
            "recent poll -- i.e. their Champion Data id is present in the source homeInterchange[]/"
            "awayInterchange[] array. Confirmed against real Round 24 live diagnostic evidence: array "
            "membership changes continuously during LIVE play (hundreds of paired appear/disappear "
            "events per match across 7 matches, tightly correlated with each team's own "
            "totalInterchangeCount incrementing; individually cited for a named player's repeated "
            "appear/disappear/reappear cycle in one match), and confirmed to freeze byte-for-byte across "
            "40 real POSTGAME polls (~10 minutes) with zero further transitions. Not yet independently "
            "verified for CONCLUDED, where this reflects the most recently observed state rather than a "
            "confirmed signal."
        )
    )
    interchange_count: int | None = Field(description="CFS interchangeCount, persisted exactly as supplied.")
    bench_reason: str | None = Field(
        description=(
            "CFS benchReason exactly as supplied (e.g. 'ROTATION'), never inferred. Null when the "
            "source did not supply one."
        )
    )
    time_on_ground_seconds: int | None = Field(description="CFS timeOnGround, in seconds, as supplied.")
    time_on_bench_seconds: int | None = Field(description="CFS timeOnBench, in seconds, as supplied.")
    power_rating: int | None = Field(description="CFS powerRating, persisted exactly as supplied.")
    first_observed_at: str = Field(description="UTC time AFL-api first observed this player in an interchange array.")
    observed_at: str = Field(description="UTC time of the most recent poll that refreshed this player's fields.")


class MatchInterchangesResponse(BaseModel):
    match: MatchInfo
    interchanges: list[InterchangeStatus]


class InterchangeEvent(BaseModel):
    """One meaningful CFS matchInterchange transition (Issue #204).

    Only meaningful transitions are persisted -- see
    ``afl_json.match_interchange.persist_match_interchange``. A poll where
    only timeOnGround/timeOnBench/powerRating changed never produces a row.
    """

    id: int = Field(description="Stable AFL-api-generated event identifier. Not a Champion Data id.")
    match_id: int
    champion_data_player_id: str
    canonical_player_id: int | None = Field(description="Canonical AFL-api player id, or null when unresolved.")
    display_name: str | None = Field(description="Canonical display name, or null when unresolved.")
    side: InterchangeSide
    team_id: int | None = Field(description="Canonical AFL-api team id, or null when unresolved.")
    champion_data_team_id: str | None
    event_type: Literal["appeared", "disappeared", "interchange_count_changed", "bench_reason_changed"]
    interchange_count: int | None
    previous_interchange_count: int | None
    bench_reason: str | None
    previous_bench_reason: str | None
    time_on_ground_seconds: int | None = Field(description="CFS timeOnGround at the time of this event, for context.")
    time_on_bench_seconds: int | None = Field(description="CFS timeOnBench at the time of this event, for context.")
    power_rating: int | None
    observed_at: str = Field(
        description=(
            "UTC time AFL-api observed this transition at the poll that detected it. This is the poll "
            "observation time, not an exact in-game clock instant -- matchInterchange does not supply "
            "periodNumber/periodSeconds, so no game-clock timestamp is fabricated here."
        )
    )


class MatchInterchangeEventsResponse(BaseModel):
    match: MatchInfo
    events: list[InterchangeEvent]


def _interchange_name_lookups(conn, rows: list[dict]) -> dict[int, str | None]:
    player_ids = {row["canonical_player_id"] for row in rows if row["canonical_player_id"] is not None}
    player_names: dict[int, str | None] = {}
    if player_ids:
        placeholders = ",".join("?" for _ in player_ids)
        for prow in conn.execute(
            f"SELECT id, display_name, given_name, family_name FROM canonical_players WHERE id IN ({placeholders})",
            tuple(player_ids),
        ):
            player_names[prow["id"]] = _display_name(prow)
    return player_names


def _interchange_status_from_row(row: dict, player_names: dict) -> InterchangeStatus:
    return InterchangeStatus(
        champion_data_player_id=row["player_provider_id"],
        canonical_player_id=row["canonical_player_id"],
        display_name=player_names.get(row["canonical_player_id"]),
        side=row["side"],
        team_id=row["canonical_team_id"],
        champion_data_team_id=row["team_provider_id"],
        on_bench=row["on_bench"],
        interchange_count=row["interchange_count"],
        bench_reason=row["bench_reason"],
        time_on_ground_seconds=row["time_on_ground"],
        time_on_bench_seconds=row["time_on_bench"],
        power_rating=row["power_rating"],
        first_observed_at=row["first_observed_at"],
        observed_at=row["last_observed_at"],
    )


@router.get(
    "/api/v1/matches/{match_id}/interchanges",
    response_model=MatchInterchangesResponse,
    responses={404: {"model": ApplicationErrorResponse, "description": "Match not found"}},
    summary="Get current interchange state for a match",
    description=(
        "Returns current per-player CFS matchInterchange state for one canonical match, backed by "
        "production persistence (Issue #204) rather than the separate interchange diagnostic evidence "
        "tables (Issue #193). Includes every player observed in either interchange array at any point "
        "in the match, so a player who has since left the array is still returned with "
        "on_bench=false and their last known field values, rather than disappearing from the response. "
        "See on_bench's field description for the real Round 24 live evidence confirming this semantic "
        "for LIVE play and POSTGAME, and the residual CONCLUDED caveat. bench_reason, interchange_count, "
        "time_on_ground_seconds, time_on_bench_seconds and power_rating are persisted and returned "
        "exactly as supplied by CFS; none are inferred. Not authoritative for match finality, lifecycle, "
        "or player statistics. A valid match with no interchange data yet returns an empty collection."
    ),
)
def get_match_interchanges(
    match_id: int,
    side: InterchangeSide | None = Query(None, description="Filter to one side (home or away)."),
    player_id: int | None = Query(None, description="Filter to one canonical player identifier."),
    on_bench_only: bool = Query(
        False, description="When true, return only players currently on the interchange bench."
    ),
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> MatchInterchangesResponse | JSONResponse:
    log(f"🔁 {credential.label} requested v1 interchanges for match {match_id}", "INFO")
    from afl_json.match_interchange import current_state_rows

    conn = get_db_connection()
    try:
        match_row = conn.execute(
            "SELECT match_id, match_provider_id, round_id, season_id, status FROM matches WHERE match_id = ?",
            (match_id,),
        ).fetchone()
        if match_row is None:
            return application_error(404, "match_not_found", "Match not found.")

        rows = current_state_rows(
            conn, match_id=match_id, side=side, canonical_player_id=player_id,
            on_bench_only=on_bench_only,
        )
        player_names = _interchange_name_lookups(conn, rows)
    finally:
        conn.close()

    return MatchInterchangesResponse(
        match=MatchInfo(
            match_id=match_row["match_id"], match_provider_id=match_row["match_provider_id"],
            round_id=match_row["round_id"], season_id=match_row["season_id"], status=match_row["status"],
        ),
        interchanges=[_interchange_status_from_row(row, player_names) for row in rows],
    )


def _interchange_event_from_row(row: dict, player_names: dict) -> InterchangeEvent:
    return InterchangeEvent(
        id=row["id"], match_id=row["match_id"], champion_data_player_id=row["player_provider_id"],
        canonical_player_id=row["canonical_player_id"], display_name=player_names.get(row["canonical_player_id"]),
        side=row["side"], team_id=row["canonical_team_id"], champion_data_team_id=row["team_provider_id"],
        event_type=row["event_type"], interchange_count=row["interchange_count"],
        previous_interchange_count=row["previous_interchange_count"], bench_reason=row["bench_reason"],
        previous_bench_reason=row["previous_bench_reason"], time_on_ground_seconds=row["time_on_ground"],
        time_on_bench_seconds=row["time_on_bench"], power_rating=row["power_rating"],
        observed_at=row["observed_at"],
    )


@router.get(
    "/api/v1/matches/{match_id}/interchanges/events",
    response_model=MatchInterchangeEventsResponse,
    responses={404: {"model": ApplicationErrorResponse, "description": "Match not found"}},
    summary="Get meaningful interchange transition history for a match",
    description=(
        "Returns the chronological (oldest-first) history of meaningful CFS matchInterchange "
        "transitions for one canonical match: a player appearing in or disappearing from an "
        "interchange array, and interchange_count/bench_reason changing. A poll where only "
        "time_on_ground_seconds/time_on_bench_seconds/power_rating changed never produces an event -- "
        "use GET /api/v1/matches/{match_id}/interchanges for current per-player values. observed_at is "
        "the UTC time AFL-api's poll detected the transition, not an exact in-game clock instant -- "
        "matchInterchange supplies no periodNumber/periodSeconds to correlate against, so none is "
        "fabricated here. A valid match with no interchange transitions yet returns an empty collection."
    ),
)
def get_match_interchange_events(
    match_id: int,
    player_id: int | None = Query(None, description="Filter to one canonical player identifier."),
    event_type: Literal["appeared", "disappeared", "interchange_count_changed", "bench_reason_changed"] | None = Query(
        None, description="Filter to one event type."
    ),
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> MatchInterchangeEventsResponse | JSONResponse:
    log(f"🔁 {credential.label} requested v1 interchange events for match {match_id}", "INFO")
    from afl_json.match_interchange import event_rows

    conn = get_db_connection()
    try:
        match_row = conn.execute(
            "SELECT match_id, match_provider_id, round_id, season_id, status FROM matches WHERE match_id = ?",
            (match_id,),
        ).fetchone()
        if match_row is None:
            return application_error(404, "match_not_found", "Match not found.")

        rows = event_rows(conn, match_id=match_id, canonical_player_id=player_id, event_type=event_type)
        player_names = _interchange_name_lookups(conn, rows)
    finally:
        conn.close()

    return MatchInterchangeEventsResponse(
        match=MatchInfo(
            match_id=match_row["match_id"], match_provider_id=match_row["match_provider_id"],
            round_id=match_row["round_id"], season_id=match_row["season_id"], status=match_row["status"],
        ),
        events=[_interchange_event_from_row(row, player_names) for row in rows],
    )


class RosterPlayer(BaseModel):
    """Minimal player identity attached to one roster selection or context record."""

    champion_data_player_id: str = Field(description="Source Champion Data player identifier.")
    canonical_player_id: int | None = Field(
        description="Canonical AFL-api player id, or null when unresolved. Never guessed from name."
    )
    display_name: str | None = Field(description="Canonical display name, or null when unresolved.")


class RosterSelection(BaseModel):
    """One selected positional-lineup entry (Issue #219).

    A selection is a team's choice, not evidence the player took the field --
    see GET /api/v1/matches/{match_id}/player-stats for actual participation
    and statistics.
    """

    player: RosterPlayer
    position: str | None = Field(
        description="CFS positional group name exactly as supplied (e.g. FORWARDS, INTERCHANGE), "
        "persisted verbatim. Never translated into a speculative enum."
    )
    jumper_number: int | None
    captain: bool | None = Field(description="Source captain flag exactly as supplied, or null when not supplied.")


class RosterContextRecord(BaseModel):
    """One in/out/late-change/club-debut/milestone record.

    Deliberately a separate collection from ``selections`` -- a change/context
    record is never merged into or inferred as lineup membership.
    """

    player: RosterPlayer
    reason: str | None = Field(description="Source-supplied reason, persisted verbatim, or null when not supplied.")


class RosterContext(BaseModel):
    """The five supported change/context collections, kept distinguishable from selections."""

    ins: list[RosterContextRecord]
    outs: list[RosterContextRecord]
    late_changes: list[RosterContextRecord]
    club_debuts: list[RosterContextRecord]
    milestones: list[RosterContextRecord]


class TeamRoster(BaseModel):
    """One side's current canonical roster state."""

    team: MatchTeam | None = Field(description="Canonical team identity, or null when unresolved.")
    champion_data_team_id: str | None = Field(description="Source Champion Data team identifier.")
    team_status: str | None = Field(
        description="Source teamStatus exactly as supplied (e.g. CONFIRMED), persisted verbatim."
    )
    selections: list[RosterSelection] = Field(
        description="Selected positional players for this side. Selection is not participation evidence."
    )
    context: RosterContext


class RosterMetadata(BaseModel):
    match_status_at_observation: str | None = Field(
        description="Source matchRoster.status for the most recent observation (e.g. PUBLISHED, "
        "CONCLUDED), persisted verbatim -- distinct from the canonical matches.status lifecycle field."
    )
    source_updated_at: str | None = Field(
        description="Source matchRoster.lastUpdated for the most recent observation, or null when "
        "no roster has been observed for this match yet."
    )


class MatchRostersResponse(BaseModel):
    match: MatchInfo
    metadata: RosterMetadata
    home_team: TeamRoster | None = Field(
        description="Null when no roster observation has been persisted for the home side yet."
    )
    away_team: TeamRoster | None = Field(
        description="Null when no roster observation has been persisted for the away side yet."
    )


_CONTEXT_TYPE_FIELDS = {
    "ins": "ins", "outs": "outs", "lateChanges": "late_changes",
    "clubDebuts": "club_debuts", "milestones": "milestones",
}


def _roster_player(row, player_names: dict) -> RosterPlayer:
    return RosterPlayer(
        champion_data_player_id=row["player_provider_id"],
        canonical_player_id=row["canonical_player_id"],
        display_name=player_names.get(row["canonical_player_id"]),
    )


def _roster_name_lookups(conn, rows: list) -> dict[int, str | None]:
    player_ids = {row["canonical_player_id"] for row in rows if row["canonical_player_id"] is not None}
    player_names: dict[int, str | None] = {}
    if player_ids:
        placeholders = ",".join("?" for _ in player_ids)
        for prow in conn.execute(
            f"SELECT id, display_name, given_name, family_name FROM canonical_players WHERE id IN ({placeholders})",
            tuple(player_ids),
        ):
            player_names[prow["id"]] = _display_name(prow)
    return player_names


@router.get(
    "/api/v1/matches/{match_id}/rosters",
    response_model=MatchRostersResponse,
    responses={404: {"model": ApplicationErrorResponse, "description": "Match not found"}},
    summary="Get canonical CFS rosters for a match",
    description=(
        "Returns the current canonical CFS matchRosters selection for one match (Issue #219), backed "
        "by production persistence (afl_json.rosters.persist_match_rosters) rather than the separate "
        "legacy rendered-HTML lineups compatibility routes/tables, which this resource never reads, "
        "writes, or falls back to -- see docs/architecture/data_authority_map.md. "
        "IMPORTANT: a returned selection is a team's choice, not evidence the player took the field. "
        "Use GET /api/v1/matches/{match_id}/player-stats for actual participation and statistics. "
        "home_team/away_team are null when no roster observation has been persisted for that side yet "
        "-- never inferred. Each side's selections and context (ins, outs, late_changes, club_debuts, "
        "milestones) are kept as separate collections; a context record is never merged into or read "
        "as lineup membership. canonical_player_id/team.team_id are null when the source Champion Data "
        "identifier has no resolved canonical crosswalk yet -- never guessed from name or jumper "
        "number, and self-healed on a later valid observation once a crosswalk exists. This resource "
        "reflects only the most recent replacement-safe (published, non-empty) observation for each "
        "side; an unavailable, empty, or malformed upstream response never erases a previously "
        "persisted roster -- see docs/match_rosters.md."
    ),
)
def get_match_rosters(
    match_id: int,
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> MatchRostersResponse | JSONResponse:
    log(f"🧑‍🤝‍🧑 {credential.label} requested v1 rosters for match {match_id}", "INFO")
    from afl_json.rosters import current_roster_context, current_roster_selections, current_roster_teams

    conn = get_db_connection()
    try:
        match_row = conn.execute(
            "SELECT match_id, match_provider_id, round_id, season_id, status FROM matches WHERE match_id = ?",
            (match_id,),
        ).fetchone()
        if match_row is None:
            return application_error(404, "match_not_found", "Match not found.")

        team_rows = current_roster_teams(conn, match_id)
        selection_rows = current_roster_selections(conn, match_id)
        context_rows = current_roster_context(conn, match_id)
        player_names = _roster_name_lookups(conn, [*selection_rows, *context_rows])
        teams = _team_projection(conn)
    finally:
        conn.close()

    match_status_at_observation = None
    source_updated_at = None
    sides: dict[str, TeamRoster] = {}
    for team_row in team_rows:
        side = team_row["side"]
        match_status_at_observation = team_row["match_status_at_observation"]
        source_updated_at = team_row["source_last_updated"]
        canonical_team_id = team_row["canonical_team_id"]
        team_identity = None
        if canonical_team_id is not None:
            name, _abbreviation = teams.get(canonical_team_id, (None, None))
            team_identity = MatchTeam(team_id=canonical_team_id, name=name)

        selections = [
            RosterSelection(
                player=_roster_player(row, player_names), position=row["position"],
                jumper_number=row["jumper_number"],
                captain=(bool(row["captain"]) if row["captain"] is not None else None),
            )
            for row in selection_rows if row["team_provider_id"] == team_row["team_provider_id"]
        ]
        context_by_field: dict[str, list[RosterContextRecord]] = {
            field_name: [] for field_name in _CONTEXT_TYPE_FIELDS.values()
        }
        for row in context_rows:
            if row["team_provider_id"] != team_row["team_provider_id"]:
                continue
            field_name = _CONTEXT_TYPE_FIELDS[row["context_type"]]
            context_by_field[field_name].append(
                RosterContextRecord(player=_roster_player(row, player_names), reason=row["reason"])
            )

        sides[side] = TeamRoster(
            team=team_identity, champion_data_team_id=team_row["team_provider_id"],
            team_status=team_row["team_status"], selections=selections,
            context=RosterContext(**context_by_field),
        )

    return MatchRostersResponse(
        match=MatchInfo(
            match_id=match_row["match_id"], match_provider_id=match_row["match_provider_id"],
            round_id=match_row["round_id"], season_id=match_row["season_id"], status=match_row["status"],
        ),
        metadata=RosterMetadata(
            match_status_at_observation=match_status_at_observation, source_updated_at=source_updated_at,
        ),
        home_team=sides.get("home"), away_team=sides.get("away"),
    )


class InjuryPlayer(BaseModel):
    """Minimal player identity attached to one injury row."""

    display_name: str | None = Field(
        description="Canonical display name, or null when not yet resolved."
    )


class InjuryRecord(BaseModel):
    """One current canonical injury (Issue #213)."""

    canonical_player_id: int = Field(
        description="Primary consumer player identity; always present -- see Scope below."
    )
    player: InjuryPlayer
    team: MatchTeam | None = Field(
        description="Canonical team identity, or null when the source club marker did not "
        "resolve to a canonical team."
    )
    injury: str | None = Field(description="Source injury description, persisted verbatim.")
    estimated_return: str | None = Field(
        description="Source estimated-return text, persisted verbatim (e.g. 'Round 5', 'Test')."
    )
    source_updated: str | None = Field(
        description="Source 'Updated:' text for this player's team block, or null when the "
        "source omitted it."
    )
    observed_at: str = Field(description="UTC time this row was last (re)collected.")
    current: bool = Field(description="Always true for this resource today; see Scope below.")


class InjuriesResponse(BaseModel):
    injuries: list[InjuryRecord]


@router.get(
    "/api/v1/injuries",
    response_model=InjuriesResponse,
    summary="List current canonical injuries",
    description=(
        "Returns current AFL injury rows using canonical player and team identity, resolved "
        "at collection time by the injury pipeline (scraper/injuries/resolution.py) rather than "
        "guessed at read time. canonical_player_id is the primary consumer identity, matching "
        "GET /api/v1/players/{canonical_player_id}; only rows with a resolved canonical player "
        "identity are returned -- an unresolved/ambiguous source row is never exposed under an "
        "invented or provider-only identity. team_id filters by canonical AFL team id (the same "
        "identifier used by home_team/away_team on the match resource); canonical_player_id "
        "filters to one player. Both are optional and conjunctive when combined. Ordering is "
        "deterministic: team_id ascending (unresolved-team rows last), then canonical_player_id "
        "ascending. This resource only ever returns current injuries -- historical injury "
        "querying is out of scope for now (Issue #213). A team omitted from the AFL source page "
        "is not evidence that team has zero injuries: see docs/architecture/injury_collector_pipeline.md."
    ),
)
def get_injuries(
    team_id: int | None = Query(
        None, ge=_SQLITE_INTEGER_MIN, le=_SQLITE_INTEGER_MAX,
        description="Filter to one canonical AFL team id.",
    ),
    canonical_player_id: int | None = Query(
        None, ge=_SQLITE_INTEGER_MIN, le=_SQLITE_INTEGER_MAX,
        description="Filter to one canonical AFL-api player id.",
    ),
    credential: AuthenticatedCredential = Depends(authenticate_api_key),
) -> InjuriesResponse:
    log(f"🩹 {credential.label} requested v1 injuries", "INFO")
    filters = ["i.current = 1", "i.canonical_player_id IS NOT NULL"]
    values: list[object] = []
    if team_id is not None:
        filters.append("i.canonical_team_id = ?")
        values.append(team_id)
    if canonical_player_id is not None:
        filters.append("i.canonical_player_id = ?")
        values.append(canonical_player_id)

    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT i.canonical_player_id, i.canonical_team_id, i.injury, i.return_info, "
            "i.updated, i.scraped_at, i.current "
            "FROM injuries i "
            f"WHERE {' AND '.join(filters)} "
            "ORDER BY i.canonical_team_id IS NULL, i.canonical_team_id, i.canonical_player_id",
            tuple(values),
        ).fetchall()

        player_names: dict[int, str | None] = {}
        player_ids = {row["canonical_player_id"] for row in rows}
        if player_ids:
            placeholders = ",".join("?" for _ in player_ids)
            for prow in conn.execute(
                "SELECT id, display_name, given_name, family_name FROM canonical_players "
                f"WHERE id IN ({placeholders})",
                tuple(player_ids),
            ):
                player_names[prow["id"]] = _display_name(prow)
        teams = _team_projection(conn)
    finally:
        conn.close()

    injuries = []
    for row in rows:
        team_id_value = row["canonical_team_id"]
        team = None
        if team_id_value is not None:
            name, _abbreviation = teams.get(team_id_value, (None, None))
            team = MatchTeam(team_id=team_id_value, name=name)
        injuries.append(InjuryRecord(
            canonical_player_id=row["canonical_player_id"],
            player=InjuryPlayer(display_name=player_names.get(row["canonical_player_id"])),
            team=team,
            injury=row["injury"],
            estimated_return=row["return_info"],
            source_updated=row["updated"] or None,
            observed_at=row["scraped_at"],
            current=bool(row["current"]),
        ))
    return InjuriesResponse(injuries=injuries)
