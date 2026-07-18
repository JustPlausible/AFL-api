import sqlite3
from fastapi import Header, HTTPException
from utils.log import log
from pathlib import Path
import config

DB_PATH = Path(config.DB_PATH)


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fingerprint_api_key(api_key: str) -> str:
    if not api_key:
        return "<empty>"
    if len(api_key) <= 10:
        return "<redacted>"
    return f"{api_key[:6]}…{api_key[-4:]}"


def verify_api_key(x_api_key: str = Header(...)) -> str:
    conn = get_db_connection()
    cursor = conn.execute(
        "SELECT label FROM api_keys WHERE api_key = ? AND is_active = 1",
        (x_api_key,),
    )
    result = cursor.fetchone()
    conn.close()

    if not result:
        log(f"🔐 Invalid API Key attempted: {_fingerprint_api_key(x_api_key)}", "WARN")
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")

    client_label = result["label"]
    log(f"🔐 Authenticated request from: {client_label}", "DEBUG")
    return client_label
