import sqlite3
from fastapi import Header, HTTPException
from utils.log import log
from pathlib import Path

DB_PATH = Path("data/afl_players.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def verify_api_key(x_api_key: str = Header(...)) -> str:
    conn = get_db_connection()
    cursor = conn.execute("SELECT label FROM api_keys WHERE api_key = ?", (x_api_key,))
    result = cursor.fetchone()
    conn.close()

    if not result:
        log(f"🔐 Invalid API Key attempted: {x_api_key}", "WARN")
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")

    client_label = result["label"]
    log(f"🔐 Authenticated request from: {client_label}", "DEBUG")
    return client_label
