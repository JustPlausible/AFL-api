import sqlite3
from fastapi import Header, HTTPException
from utils.log import log
from pathlib import Path
import config
from api_key_security import api_key_prefix, verify_api_key_hash
from db.init_db import create_api_keys_table

DB_PATH = Path(config.DB_PATH)


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fingerprint_api_key(api_key: str) -> str:
    if not api_key:
        return "<empty>"
    return f"{api_key_prefix(api_key)}…"


def verify_api_key(x_api_key: str = Header(...)) -> str:
    conn = get_db_connection()
    create_api_keys_table(conn.cursor())
    conn.commit()
    cursor = conn.execute(
        "SELECT label, key_hash FROM api_keys WHERE is_active = 1 AND key_hash IS NOT NULL",
    )
    result = None
    for row in cursor.fetchall():
        if verify_api_key_hash(x_api_key, row["key_hash"]):
            result = row
            break
    conn.close()

    if not result:
        log(f"🔐 Invalid API Key attempted: {_fingerprint_api_key(x_api_key)}", "WARN")
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")

    client_label = result["label"]
    log(f"🔐 Authenticated request from: {client_label}", "DEBUG")
    return client_label
