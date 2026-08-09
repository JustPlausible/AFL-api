"""Versioned, consumer-facing API routes over canonical data sources."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from afl_json.season_report import authoritative_stats_finality_for_match
from auth import verify_api_key
from db.connection import get_db_connection
from utils.log import log


router = APIRouter()


class MatchSummary(BaseModel):
    match_id: int
    match_provider_id: str | None
    round_id: int | None
    season_id: int | None
    status: str | None


class MatchLifecycle(BaseModel):
    finality: Literal["final", "partial", "not_available"]
    authoritative_rows: int
    authoritative_sides: int
    min_snapshot_authority: int | None
    max_snapshot_authority: int | None


class StablePlayerStats(BaseModel):
    goals: int | float | None
    behinds: int | float | None
    kicks: int | float | None
    handballs: int | float | None
    disposals: int | float | None
    marks: int | float | None
    tackles: int | float | None
    hitouts: int | float | None


class MatchPlayerStatsRow(BaseModel):
    champion_data_player_id: str
    canonical_player_id: int | None
    afl_player_id: int | None
    display_name: str | None
    side: Literal["home", "away"]
    team_id: int | None
    stats: StablePlayerStats
    snapshot_authority: int
    resolved_match_status: str | None
    collected_at: str


class MatchPlayerStatsResponse(BaseModel):
    match: MatchSummary
    lifecycle: MatchLifecycle
    players: list[MatchPlayerStatsRow]


def _finality_label(finality) -> Literal["final", "partial", "not_available"]:
    if not finality.has_authoritative_snapshot:
        return "not_available"
    if finality.is_partial_authoritative_snapshot:
        return "partial"
    return "final"


@router.get(
    "/api/v1/matches/{match_id}/player-stats",
    response_model=MatchPlayerStatsResponse,
    summary="Get canonical player statistics for a match",
    description=(
        "Returns the latest persisted CFS player-stat observations for a match, "
        "including canonical identity and match-level finality evidence. This "
        "read-only endpoint does not fetch from CFS on demand."
    ),
)
def get_match_player_stats(
    match_id: int,
    side: Literal["home", "away"] | None = Query(
        None, description="Restrict results to the home or away side."
    ),
    champion_data_player_id: str | None = Query(
        None, description="Restrict results to one Champion Data player ID."
    ),
    client_label: str = Depends(verify_api_key),
) -> MatchPlayerStatsResponse:
    """Read the stable Stage 1 player-stat contract from canonical CFS storage."""
    log(f"📊 {client_label} requested canonical player stats for match {match_id}", "INFO")
    conn = get_db_connection()
    try:
        match = conn.execute(
            "SELECT match_id,match_provider_id,round_id,season_id,status,"
            "home_team_id,away_team_id "
            "FROM matches WHERE match_id=?",
            (match_id,),
        ).fetchone()
        if match is None:
            raise HTTPException(status_code=404, detail="Match not found")

        match_provider_id = match["match_provider_id"]
        finality = authoritative_stats_finality_for_match(conn, match_provider_id)
        rows = []
        if match_provider_id is not None:
            predicates = ["s.match_provider_id=?"]
            parameters: list[object] = [match_provider_id]
            if side is not None:
                predicates.append("s.side=?")
                parameters.append(side)
            if champion_data_player_id is not None:
                predicates.append("s.champion_data_player_id=?")
                parameters.append(champion_data_player_id)

            rows = conn.execute(
                "SELECT s.champion_data_player_id,s.canonical_player_id,"
                "afl.provider_player_id AS afl_player_id,"
                "COALESCE(NULLIF(TRIM(cp.display_name),''),"
                "NULLIF(TRIM(COALESCE(cp.given_name,'') || ' ' || "
                "COALESCE(cp.family_name,'')),'')) AS display_name,"
                "s.side,team.afl_id AS team_id,s.goals,s.behinds,s.kicks,"
                "s.handballs,s.disposals,s.marks,s.tackles,s.hitouts,"
                "s.snapshot_authority,s.resolved_match_status,s.collected_at "
                "FROM cfs_player_stats s "
                "LEFT JOIN canonical_players cp ON cp.id=s.canonical_player_id "
                "LEFT JOIN player_provider_ids afl "
                "ON afl.player_id=s.canonical_player_id AND afl.provider='afl' "
                "LEFT JOIN afl_teams team ON team.afl_id=CASE s.side "
                "WHEN 'home' THEN ? WHEN 'away' THEN ? END "
                f"WHERE {' AND '.join(predicates)} "
                "ORDER BY s.side,s.champion_data_player_id",
                [match["home_team_id"], match["away_team_id"], *parameters],
            ).fetchall()

        players = [
            MatchPlayerStatsRow(
                champion_data_player_id=row["champion_data_player_id"],
                canonical_player_id=row["canonical_player_id"],
                afl_player_id=row["afl_player_id"],
                display_name=row["display_name"],
                side=row["side"],
                team_id=row["team_id"],
                stats=StablePlayerStats(**{name: row[name] for name in StablePlayerStats.model_fields}),
                snapshot_authority=row["snapshot_authority"],
                resolved_match_status=row["resolved_match_status"],
                collected_at=row["collected_at"],
            )
            for row in rows
        ]
        return MatchPlayerStatsResponse(
            match=MatchSummary(**dict(match)),
            lifecycle=MatchLifecycle(
                finality=_finality_label(finality),
                authoritative_rows=finality.authoritative_rows,
                authoritative_sides=finality.authoritative_sides,
                min_snapshot_authority=finality.min_authority,
                max_snapshot_authority=finality.max_authority,
            ),
            players=players,
        )
    finally:
        conn.close()
