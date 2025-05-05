from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pathlib import Path
import json
from datetime import datetime
from auth import verify_api_key
import sqlite3
from utils.log import log


router = APIRouter()
DB_PATH = Path("data/afl_players.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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


@router.get("/api/players/club/{club_slug}")
def get_players_by_club(club_slug: str, client_label: str = Depends(verify_api_key)):
    log(f"📦 {client_label} requested players for club: {club_slug.upper()}", "INFO")
    conn = get_db_connection()
    players = conn.execute("SELECT * FROM players WHERE lower(club) = ?", (club_slug.lower(),)).fetchall()
    conn.close()

    if not players:
        log(f"⚠️ No players found for club: {club_slug}", "WARN")
    
    return JSONResponse(content=[dict(row) for row in players])

@router.get("/api/injuries")
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

@router.get("/api/injuries/{afl_id}")
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
