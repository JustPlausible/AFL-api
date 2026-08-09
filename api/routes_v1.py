"""Versioned canonical read routes (``/api/v1``).

This module is strictly additive: it introduces the first canonical,
versioned consumer surface over authoritative ``cfs_player_stats`` and never
touches ``api/routes.py``'s unversioned compatibility routes, tables, or
response shapes. See
``docs/architecture/workflows/player_stats_api_design.md`` for the accepted
API contract this module implements.
"""

from enum import Enum
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from afl_json.player_stats import CANONICAL_STAT_FIELDS
from afl_json.season_report import authoritative_stats_finality_for_match
from auth import verify_api_key
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
    authoritative_rows: int
    authoritative_sides: int
    min_snapshot_authority: int | None
    max_snapshot_authority: int | None


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
    snapshot_authority: int
    resolved_match_status: str | None
    collected_at: str


class MatchPlayerStatsResponse(BaseModel):
    match: MatchInfo
    lifecycle: Lifecycle
    players: list[PlayerStat]


def _finality_payload(finality) -> Lifecycle:
    status: FinalityStatus
    if not finality.has_authoritative_snapshot:
        status = "not_available"
    elif finality.is_partial_authoritative_snapshot:
        status = "partial"
    else:
        status = "final"
    return Lifecycle(
        finality=status,
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
    response_model=MatchPlayerStatsResponse,
    summary="Get canonical CFS player statistics for a match",
    description=(
        "Returns authoritative Champion Data (CFS) player statistics for a match, "
        "resolved via matches.match_id -> matches.match_provider_id. Reads only "
        "cfs_player_stats (never the legacy player_stats table) and reports explicit "
        "final/partial/not_available lifecycle semantics computed fresh on every "
        "request via the repository's shared authoritative finality predicate."
    ),
)
def get_match_player_stats(
    match_id: int,
    side: MatchSide | None = Query(None, description="Filter players by side (home or away)"),
    champion_data_player_id: str | None = Query(
        None, description="Filter to a single player by Champion Data player ID"
    ),
    client_label: str = Depends(verify_api_key),
):
    log(f"📊 {client_label} requested v1 player stats for match {match_id}", "INFO")
    conn = get_db_connection()
    try:
        match_row = conn.execute(
            "SELECT match_id, match_provider_id, round_id, season_id, status "
            "FROM matches WHERE match_id = ?",
            (match_id,),
        ).fetchone()

        if not match_row:
            log(f"❌ No match found with Match ID: {match_id}", "WARN")
            raise HTTPException(status_code=404, detail="Match not found")

        match_provider_id = match_row["match_provider_id"]
        match_payload = MatchInfo(
            match_id=match_row["match_id"],
            match_provider_id=match_provider_id,
            round_id=match_row["round_id"],
            season_id=match_row["season_id"],
            status=match_row["status"],
        )
        lifecycle_payload = _finality_payload(
            authoritative_stats_finality_for_match(conn, match_provider_id)
        )

        if not match_provider_id:
            return MatchPlayerStatsResponse(match=match_payload, lifecycle=lifecycle_payload, players=[])

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

    players = [
        PlayerStat(
            champion_data_player_id=row["champion_data_player_id"],
            canonical_player_id=row["canonical_player_id"],
            afl_player_id=(
                int(row["afl_player_id"]) if row["afl_player_id"] is not None else None
            ),
            display_name=_display_name(row),
            side=row["side"],
            team_id=row["team_id"],
            stats=PlayerStatValues(**{name: row[name] for name in CANONICAL_STAT_FIELDS}),
            snapshot_authority=row["snapshot_authority"],
            resolved_match_status=row["resolved_match_status"],
            collected_at=row["collected_at"],
        )
        for row in rows
    ]

    return MatchPlayerStatsResponse(match=match_payload, lifecycle=lifecycle_payload, players=players)
