"""The shared SQLite connection policy for application processes.

``journal_mode`` is persistent and is established only by
``initialize_database_policy`` at a migration/startup boundary.  The other
PRAGMAs are connection-local and are therefore applied whenever a connection
is opened.  Python's implicit (DEFERRED) transaction behaviour is retained.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config
from utils.log import log


@dataclass(frozen=True)
class SQLitePolicy:
    connect_timeout_seconds: float = 10.0
    busy_timeout_ms: int = 10_000
    journal_mode: str = "wal"
    synchronous: str = "NORMAL"
    isolation_level: str = "DEFERRED"


SQLITE_POLICY = SQLitePolicy()


class SQLitePolicyError(RuntimeError):
    """Raised when a mandatory persistent SQLite setting cannot be applied."""


_POLICY_RETRY_BACKOFF_SECONDS = 0.05


def _sqlite_error_code(exc: BaseException) -> int | None:
    return getattr(exc, "sqlite_errorcode", None)


def _is_sqlite_busy_or_locked(exc: BaseException) -> bool:
    code = _sqlite_error_code(exc)
    if code is not None:
        return code in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_BUSY_RECOVERY,
            sqlite3.SQLITE_BUSY_SNAPSHOT,
            sqlite3.SQLITE_LOCKED,
            sqlite3.SQLITE_LOCKED_SHAREDCACHE,
        }
    busy_markers = ("database is locked", "database table is locked", "database is busy")
    return isinstance(exc, sqlite3.OperationalError) and any(
        marker in str(exc).lower() for marker in busy_markers
    )


def _is_policy_lock_error(exc: SQLitePolicyError) -> bool:
    cause = exc.__cause__
    return cause is not None and _is_sqlite_busy_or_locked(cause)


def get_db_path() -> Path:
    return Path(config.DB_PATH)


def validate_db_parent(db_path: Path | None = None) -> Path:
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


def _configure_connection(conn: sqlite3.Connection, *, read_only: bool) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_POLICY.busy_timeout_ms}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA synchronous = {SQLITE_POLICY.synchronous}")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    return conn


def get_db_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    if not db_path.exists():
        log(f"❌ Database file not found: {db_path}", "ERROR")
        raise FileNotFoundError(f"Database file does not exist: {db_path}")
    conn = sqlite3.connect(
        db_path,
        timeout=SQLITE_POLICY.connect_timeout_seconds,
        isolation_level=SQLITE_POLICY.isolation_level,
    )
    return _configure_connection(conn, read_only=False)


def get_read_only_db_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"Database file does not exist: {db_path}")
    # mode=ro prevents file creation and writes at the SQLite VFS boundary.
    conn = sqlite3.connect(
        f"file:{db_path.resolve()}?mode=ro", uri=True,
        timeout=SQLITE_POLICY.connect_timeout_seconds,
        isolation_level=SQLITE_POLICY.isolation_level,
    )
    return _configure_connection(conn, read_only=True)


def _initialize_database_policy_once(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(path, timeout=SQLITE_POLICY.connect_timeout_seconds, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_POLICY.busy_timeout_ms}")
        returned = str(conn.execute(f"PRAGMA journal_mode = {SQLITE_POLICY.journal_mode}").fetchone()[0]).lower()
        if returned != SQLITE_POLICY.journal_mode:
            raise SQLitePolicyError(
                f"required journal_mode={SQLITE_POLICY.journal_mode}, database returned {returned}"
            )
        conn.execute(f"PRAGMA synchronous = {SQLITE_POLICY.synchronous}")
        conn.execute("PRAGMA foreign_keys = ON")
        return inspect_database_policy(conn)
    except sqlite3.Error as exc:
        raise SQLitePolicyError(f"failed to establish SQLite policy for {path}: {exc}") from exc
    finally:
        conn.close()


def initialize_database_policy(db_path: Path | str | None = None) -> dict[str, Any]:
    """Idempotently establish and verify persistent policy (startup/migrations only).

    Concurrent startup can briefly lock ``PRAGMA journal_mode=WAL`` before the
    migration runner's ``BEGIN IMMEDIATE`` claim exists. Retry only those
    transient SQLite busy/locked failures for the declared policy timeout; a
    process returns only after WAL is established or verified.
    """
    path = validate_db_parent(Path(db_path) if db_path is not None else get_db_path())
    deadline = time.monotonic() + SQLITE_POLICY.connect_timeout_seconds
    last_lock_error: SQLitePolicyError | None = None
    while True:
        try:
            return _initialize_database_policy_once(path)
        except SQLitePolicyError as exc:
            if not _is_policy_lock_error(exc):
                raise
            last_lock_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SQLitePolicyError(
                    f"timed out after {SQLITE_POLICY.connect_timeout_seconds:.1f}s while establishing "
                    f"SQLite policy for {path}; database remained locked"
                ) from last_lock_error
            time.sleep(min(_POLICY_RETRY_BACKOFF_SECONDS, remaining))


def inspect_database_policy(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Inspect effective settings without changing persistent database state."""
    owned = conn is None
    db = conn if conn is not None else get_read_only_db_connection()
    try:
        return {
            "journal_mode": str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
            "synchronous": int(db.execute("PRAGMA synchronous").fetchone()[0]),
            "busy_timeout_ms": int(db.execute("PRAGMA busy_timeout").fetchone()[0]),
            "foreign_keys": bool(db.execute("PRAGMA foreign_keys").fetchone()[0]),
            "query_only": bool(db.execute("PRAGMA query_only").fetchone()[0]),
            "isolation_level": db.isolation_level,
        }
    finally:
        if owned:
            db.close()
