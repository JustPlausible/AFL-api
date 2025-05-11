from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pathlib import Path
import json
from datetime import datetime
from auth import verify_api_key
import sqlite3
from utils.log import log
from db.connection import get_db_connection
from db.helpers import get_round_start_times

router = APIRouter()

@router.get("/")
def read_root():
    return {"message": "AFL Supplemental API up and running!"}

@router.get("/api/players")
def get_all_players(client_label: str = Depends(verify_api_key)):
    log(f"📄 {client_label} requested full player list", "INFO")
    conn = get_db_connection()
    players = conn.execute("SELECT * FROM players").fetchall()
    conn.close()
    return JSONResponse(content=[dict(row) for row in players])


@router.get("/api/players/{afl_id}")
def get_player_by_id(afl_id: int, client_label: str = Depends(verify_api_key)):
    log(f"🔍 {client_label} requested player by AFL ID: {afl_id}", "INFO")
    conn = get_db_connection()
    player = conn.execute("SELECT * FROM players WHERE afl_id = ?", (afl_id,)).fetchone()
    conn.close()

    if not player:
        log(f"❌ No player found with AFL ID: {afl_id}", "WARN")
        raise HTTPException(status_code=404, detail="Player not found")
    return JSONResponse(content=dict(player))


@router.get("/api/players/club/{club_slug}", summary="Get current players for a given club", description="Returns current players.")
def get_players_by_club(club_slug: str, client_label: str = Depends(verify_api_key)):
    log(f"📦 {client_label} requested players for club: {club_slug.upper()}", "INFO")
    conn = get_db_connection()
    players = conn.execute("SELECT * FROM players WHERE lower(club) = ?", (club_slug.lower(),)).fetchall()
    conn.close()

    if not players:
        log(f"⚠️ No players found for club: {club_slug}", "WARN")
    
    return JSONResponse(content=[dict(row) for row in players])

@router.get("/api/injuries", summary="Get current injuries for all players", description="Returns current injury record(s) for all players. Use `?history=1` to include historical entries.")
def get_all_injuries(
    client_label: str = Depends(verify_api_key),
    history: int = Query(0, description="Include all historical injuries if set to 1")
):
    log(f"📄 {client_label} requested full injury list (history={history})", "INFO")
    conn = get_db_connection()

    if history:
        query = "SELECT * FROM injuries ORDER BY updated DESC, club, player_name"
        injuries = conn.execute(query).fetchall()
    else:
        query = "SELECT * FROM injuries WHERE current = 1 ORDER BY updated DESC, club, player_name"
        injuries = conn.execute(query).fetchall()

    conn.close()
    return JSONResponse(content=[dict(row) for row in injuries])

@router.get("/api/injuries/{afl_id}", summary="Get current injuries for player", description="Returns current injury record(s) for a given player. Use `?history=1` to include historical entries.")
def get_injuries_by_id(
    afl_id: int,
    client_label: str = Depends(verify_api_key),
    history: int = Query(0, description="Include all historical injuries if set to 1")
):
    log(f"🔍 {client_label} requested player injuries by AFL ID: {afl_id} (history={history})", "INFO")
    conn = get_db_connection()

    if history:
        query = "SELECT * FROM injuries WHERE afl_id = ? ORDER BY updated DESC"
        rows = conn.execute(query, (afl_id,)).fetchall()
    else:
        query = "SELECT * FROM injuries WHERE afl_id = ? AND current = 1 ORDER BY updated DESC"
        rows = conn.execute(query, (afl_id,)).fetchall()

    conn.close()

    if not rows:
        log(f"❌ No injury record found for AFL ID: {afl_id}", "WARN")
        raise HTTPException(status_code=404, detail="Player not found")

    return JSONResponse(content=[dict(row) for row in rows])

@router.get("/api/lineups/latest/{afl_id}", summary="Get most recent line-up for player", description="Returns the most recent line-up data available for a given player.")
def get_latest_lineup_for_player(
    afl_id: int,
    client_label: str = Depends(verify_api_key)
):
    log(f"🆕 {client_label} requested latest lineup for AFL ID {afl_id}", "INFO")
    conn = get_db_connection()

    query = """
        SELECT * FROM lineups
        WHERE afl_id = ?
        ORDER BY round_number DESC
        LIMIT 1
    """
    row = conn.execute(query, (afl_id,)).fetchone()
    conn.close()

    if not row:
        log(f"❌ No lineup record found for AFL ID: {afl_id}", "WARN")
        raise HTTPException(status_code=404, detail="No lineup found for this player")

    return JSONResponse(content=dict(row))

@router.get("/api/lineups/{round_number}", summary="Get all lineups for a round", description="Returns all player line-up data for a given round.")
def get_lineups_by_round(
    round_number: int,
    client_label: str = Depends(verify_api_key)
):
    log(f"📋 {client_label} requested lineups for Round {round_number}", "INFO")
    conn = get_db_connection()

    query = """
        SELECT * FROM lineups
        WHERE round_number = ?
        ORDER BY team, position_group, surname
    """
    rows = conn.execute(query, (round_number,)).fetchall()
    conn.close()

    if not rows:
        log(f"❌ No lineups found for Round {round_number}", "WARN")
        raise HTTPException(status_code=404, detail="No lineups found for this round")

    return JSONResponse(content=[dict(row) for row in rows])

@router.get("/api/lineups/{round_number}/{afl_id}", summary="Get player line-up in a round", description="Returns line-up entry for a specific player in a given round.")
def get_lineup_by_player_and_round(
    round_number: int,
    afl_id: int,
    client_label: str = Depends(verify_api_key)
):
    log(f"🔎 {client_label} requested lineup for AFL ID {afl_id} in Round {round_number}", "INFO")
    conn = get_db_connection()

    query = """
        SELECT * FROM lineups
        WHERE round_number = ? AND afl_id = ?
    """
    row = conn.execute(query, (round_number, afl_id)).fetchone()
    conn.close()

    if not row:
        log(f"❌ No lineup found for AFL ID {afl_id} in Round {round_number}", "WARN")
        raise HTTPException(status_code=404, detail="Lineup not found for player in this round")

    return JSONResponse(content=dict(row))

@router.get("/api/rounds", summary="Get all rounds", description="Returns metadata for all available AFL rounds.")
def get_all_rounds(client_label: str = Depends(verify_api_key)):
    log(f"📄 {client_label} requested full round list", "INFO")
    conn = get_db_connection()
    rounds = conn.execute("SELECT * FROM rounds").fetchall()

    # Get start times as a dict: {round_id: round_start_utc}
    round_starts = dict(get_round_start_times(conn))
    conn.close()

    enriched = []
    for row in rounds:
        data = dict(row)
        data["round_start_utc"] = round_starts.get(data["round_id"])  # May be None
        enriched.append(data)

    return JSONResponse(content=enriched)

@router.get("/api/rounds/{round_id}", summary="Get a round by ID", description="Returns details for a specific round by round ID.")
def get_round_by_id(round_id: int, client_label: str = Depends(verify_api_key)):
    log(f"🔍 {client_label} requested round by Round ID: {round_id}", "INFO")
    conn = get_db_connection()
    round = conn.execute("SELECT * FROM rounds WHERE round_id = ?", (round_id,)).fetchone()

    # Inject live start time for this round if available
    start_time = conn.execute("""
        SELECT MIN(start_time_utc)
        FROM matches
        WHERE round_id = ? AND start_time_utc IS NOT NULL
    """, (round_id,)).fetchone()[0]
    conn.close()

    if not round:
        log(f"❌ No round found with Round ID: {round_id}", "WARN")
        raise HTTPException(status_code=404, detail="Round not found")

    round_data = dict(round)
    round_data["round_start_utc"] = start_time
    return JSONResponse(content=round_data)

@router.get("/api/matches", summary="Get matches", description="Returns all matches or filters by round ID.")
def get_all_matches(
    round_id: int = Query(None, description="Filter matches by round ID"),
    client_label: str = Depends(verify_api_key)
):
    conn = get_db_connection()

    if round_id:
        log(f"📦 {client_label} requested matches for Round {round_id}", "INFO")
        query = "SELECT * FROM matches WHERE round_id = ? ORDER BY start_time_text"
        matches = conn.execute(query, (round_id,)).fetchall()
    else:
        log(f"📄 {client_label} requested all matches", "INFO")
        query = "SELECT * FROM matches ORDER BY match_id"
        matches = conn.execute(query).fetchall()

    conn.close()
    return JSONResponse(content=[dict(row) for row in matches])

@router.get("/api/matches/{match_id}", summary="Get a match by ID", description="Returns match metadata for a specific match.")
def get_match_by_id(
    match_id: int,
    client_label: str = Depends(verify_api_key)
):
    log(f"🔍 {client_label} requested match by Match ID: {match_id}", "INFO")
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    conn.close()

    if not row:
        log(f"❌ No match found with Match ID: {match_id}", "WARN")
        raise HTTPException(status_code=404, detail="Match not found")

    return JSONResponse(content=dict(row))

@router.get("/api/player-stats", summary="Get player stats", description="Returns player statistics filtered by match ID, round ID, or AFL ID. At least one filter is required.")
def get_player_stats(
    match_id: int = Query(None, description="Filter by match ID"),
    round_id: int = Query(None, description="Filter by round ID"),
    afl_id: int = Query(None, description="Filter by player AFL ID"),
    client_label: str = Depends(verify_api_key)
):
    log(f"🧠 {client_label} requested player stats", "INFO")
    conn = get_db_connection()

    # Build dynamic WHERE clause
    filters = []
    values = []

    if match_id:
        filters.append("match_id = ?")
        values.append(match_id)
    if round_id:
        filters.append("round_id = ?")
        values.append(round_id)
    if afl_id:
        filters.append("afl_id = ?")
        values.append(afl_id)

    if not filters:
        conn.close()
        raise HTTPException(status_code=400, detail="At least one filter is required (match_id, round_id, or afl_id)")

    query = f"SELECT * FROM player_stats WHERE {' AND '.join(filters)} ORDER BY match_id, afl_id"
    rows = conn.execute(query, tuple(values)).fetchall()
    conn.close()

    return JSONResponse(content=[dict(row) for row in rows])
