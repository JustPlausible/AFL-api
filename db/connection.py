# db/connection.py

import sqlite3
from pathlib import Path
import config
from utils.log import log

DB_PATH = Path(config.DB_PATH)

def get_db_connection() -> sqlite3.Connection:
    """Returns a SQLite connection with row access by column name."""
    if not DB_PATH.exists():
        log(f"❌ Database file not found: {DB_PATH}", "ERROR")
        raise FileNotFoundError(f"Database file does not exist: {DB_PATH}")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
