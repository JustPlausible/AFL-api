"""Add columns and indexes required by current importer paths.

This migration preserves existing rows while aligning databases created solely by
v0.3.0 init_db() with schemas produced when importer paths had run.
"""
MIGRATION_ID = "0002"
DESCRIPTION = "Add importer schema compatibility columns and indexes"


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table, column, definition):
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate(conn):
    _add_column(conn, "players", "source", "TEXT")
    _add_column(conn, "players", "scraped_at", "TEXT")
    _add_column(conn, "players", "resolved_at", "TEXT")
    _add_column(conn, "matches", "match_time_label", "TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_scrape_log_match_scraped_at ON scrape_log(match_id, scraped_at)")
