"""Lightweight SQLite schema migration runner for application-owned tables."""
from __future__ import annotations

import hashlib
import importlib.util
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Iterable

from db.connection import get_db_path, validate_db_parent

MIGRATION_RE = re.compile(r"^(?P<identifier>\d{4})_(?P<slug>[a-z0-9_]+)\.py$")
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""
BASELINE_ID = "0001"

class MigrationError(RuntimeError):
    """Raised when migration discovery, validation, or execution fails."""

@dataclass(frozen=True)
class Migration:
    identifier: str
    description: str
    checksum: str
    path: Path
    module: ModuleType


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"db.migrations.{path.stem}", path)
    if spec is None or spec.loader is None:
        raise MigrationError(f"Cannot load migration {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _checksum(path: Path, identifier: str, description: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"identifier:{identifier}\ndescription:{description}\n".encode())
    digest.update(path.read_bytes())
    return digest.hexdigest()


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations: list[Migration] = []
    seen: set[str] = set()
    for path in sorted(migrations_dir.glob("*.py"), key=lambda p: p.name):
        if path.name == "__init__.py":
            continue
        match = MIGRATION_RE.match(path.name)
        if not match:
            raise MigrationError(f"Malformed migration filename: {path.name}; expected NNNN_description.py")
        identifier = match.group("identifier")
        if identifier in seen:
            raise MigrationError(f"Duplicate migration identifier: {identifier}")
        module = _load_module(path)
        declared_id = getattr(module, "MIGRATION_ID", None)
        description = getattr(module, "DESCRIPTION", None)
        if declared_id != identifier:
            raise MigrationError(f"Migration {path.name} declares MIGRATION_ID={declared_id!r}, expected {identifier!r}")
        if not isinstance(description, str) or not description.strip():
            raise MigrationError(f"Migration {path.name} must declare non-empty DESCRIPTION")
        if not callable(getattr(module, "migrate", None)):
            raise MigrationError(f"Migration {path.name} must define migrate(conn)")
        seen.add(identifier)
        migrations.append(Migration(identifier, description, _checksum(path, identifier, description), path, module))
    return migrations


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})")}

# Exact baseline signature for the v0.3.0 init_db schema. Importer-created extra
# columns are accepted separately below so databases touched by importer paths can
# be safely baselined without replaying CREATE TABLE statements.
BASELINE_TABLES: dict[str, set[str]] = {
    "api_keys": {"id", "label", "api_key", "key_hash", "key_prefix", "created_at", "is_active"},
    "clubs": {"code", "name", "slug", "website", "squad_url", "aliases"},
    "players": {"afl_id", "full_name", "first_name", "last_name", "nickname", "formatted_nickname", "formatted_last_name", "club", "guernsey", "position", "club_profile_url", "image_url", "club_player_id", "afl_url", "champion_data_id", "last_updated"},
    "rounds": {"round_id", "round_label", "season_id", "competition_id", "scraped_at"},
    "matches": {"match_id", "match_provider_id", "round_id", "home_team", "away_team", "venue", "status", "start_time_utc", "score_home", "score_away", "scraped_at"},
    "injuries": {"afl_id", "club", "player_name", "injury", "return_info", "updated", "first_updated", "source", "scraped_at", "current"},
    "lineups": {"round_number", "match_id", "afl_id", "first_name", "surname", "team", "position_group", "champion_id", "scraped_at"},
    "player_stats": {"id", "match_id", "round_id", "afl_id", "champion_id", "player_name", "jumper_number", "team_code", "af_score", "goals", "behinds", "disposals", "kicks", "handballs", "marks", "tackles", "hitouts", "clearances", "metres_gained", "goal_assists", "time_on_ground_pct", "status", "scraped_at"},
    "scrape_log": {"id", "match_id", "round_id", "status", "scraped_at"},
    "scrape_summary": {"match_id", "round_id", "total_scrapes", "first_scraped", "last_scraped", "completed_scrape", "notes"},
}
ALLOWED_EXTRA_COLUMNS = {
    "players": {"id", "source", "scraped_at", "resolved_at"},
    "matches": {"match_time_label"},
}


def classify_existing_database(conn: sqlite3.Connection) -> str:
    tables = _tables(conn) - {"schema_migrations"}
    if not tables:
        return "empty"
    if tables == {"api_keys"}:
        # Temporary compatibility for databases created by legacy API-key-only tests/scripts.
        # Full v0.3.0 baselining remains strict; this path runs CREATE IF NOT EXISTS
        # migrations to add the missing application tables and then upgrades keys.
        return "legacy-api-keys-only"
    expected = set(BASELINE_TABLES)
    if tables != expected:
        missing = sorted(expected - tables)
        extra = sorted(tables - expected)
        raise MigrationError(f"Unexpected pre-migration database tables; missing={missing}, extra={extra}")
    for table, required in BASELINE_TABLES.items():
        cols = set(_columns(conn, table))
        allowed = required | ALLOWED_EXTRA_COLUMNS.get(table, set())
        if not required.issubset(cols) or not cols.issubset(allowed):
            raise MigrationError(f"Table {table} is not compatible with the v0.3.0 baseline; expected columns {sorted(required)}, found {sorted(cols)}")
    return "v0.3.0-compatible"


def _ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA_MIGRATIONS_SQL)


def _record(conn: sqlite3.Connection, migration: Migration) -> None:
    conn.execute(
        "INSERT INTO schema_migrations (migration_id, description, checksum, applied_at) VALUES (?, ?, ?, ?)",
        (migration.identifier, migration.description, migration.checksum, datetime.now(timezone.utc).isoformat()),
    )


def _applied(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    if "schema_migrations" not in _tables(conn):
        return {}
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT migration_id, description, checksum FROM schema_migrations")}


def migrate_database(db_path: Path | str | None = None, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    resolved = validate_db_parent(Path(db_path) if db_path is not None else get_db_path())
    migrations = discover_migrations(migrations_dir)
    conn = sqlite3.connect(resolved, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        applied = _applied(conn)
        if not applied:
            state = classify_existing_database(conn)
            if state == "v0.3.0-compatible":
                baseline = next((m for m in migrations if m.identifier == BASELINE_ID), None)
                if baseline is None:
                    raise MigrationError(f"Baseline migration {BASELINE_ID} is missing")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    _ensure_migration_table(conn)
                    _record(conn, baseline)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                applied = _applied(conn)
        else:
            _ensure_migration_table(conn)
        applied_ids = set(applied)
        by_id = {m.identifier: m for m in migrations}
        for identifier, (description, checksum) in applied.items():
            migration = by_id.get(identifier)
            if migration is None:
                raise MigrationError(f"Applied migration {identifier} is not present on disk")
            if checksum != migration.checksum or description != migration.description:
                raise MigrationError(f"Applied migration {identifier} has changed; expected checksum {checksum}, found {migration.checksum}")
        ran: list[str] = []
        for migration in migrations:
            if migration.identifier in applied_ids:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                migration.module.migrate(conn)
                _ensure_migration_table(conn)
                _record(conn, migration)
                conn.execute("COMMIT")
                ran.append(migration.identifier)
            except Exception as exc:
                conn.execute("ROLLBACK")
                raise MigrationError(f"Migration {migration.identifier} ({migration.description}) failed: {exc}") from exc
        return ran
    finally:
        conn.close()
