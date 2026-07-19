# db/connection.py

import sqlite3
from pathlib import Path
import config
from utils.log import log


def get_db_path() -> Path:
    """Return the configured SQLite database path used by the application."""
    return Path(config.DB_PATH)



def validate_db_parent(db_path: Path | None = None) -> Path:
    """Fail clearly if the configured database parent is not an existing directory."""
    resolved_path = Path(db_path) if db_path is not None else get_db_path()
    parent = resolved_path.parent
    if not parent.exists():
        raise FileNotFoundError(
            f"Configured database parent directory does not exist: {parent}. "
            "Set DB_PATH to the intended SQLite database location and create the parent directory before initialising."
        )
    if not parent.is_dir():
        raise NotADirectoryError(f"Configured database parent is not a directory: {parent}")
    return resolved_path


def get_db_connection() -> sqlite3.Connection:
    """Returns a SQLite connection with row access by column name."""
    db_path = get_db_path()
    if not db_path.exists():
        log(f"❌ Database file not found: {db_path}", "ERROR")
        raise FileNotFoundError(f"Database file does not exist: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn
