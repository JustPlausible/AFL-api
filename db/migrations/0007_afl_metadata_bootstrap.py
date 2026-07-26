"""Persist the public AFL competition hierarchy used by first-run bootstrap."""

MIGRATION_ID = "0007"
DESCRIPTION = "Add persistent public AFL season metadata"


def _add_column(conn, table, definition):
    name = definition.split()[0]
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS afl_competitions (
            afl_id INTEGER PRIMARY KEY, provider_id TEXT UNIQUE, code TEXT,
            name TEXT, metadata_json TEXT, source_json TEXT, updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS afl_seasons (
            afl_id INTEGER PRIMARY KEY, provider_id TEXT UNIQUE,
            competition_id INTEGER NOT NULL, name TEXT, short_name TEXT, year INTEGER,
            is_current INTEGER, current_round_number INTEGER, start_time TEXT, end_time TEXT,
            metadata_json TEXT, source_json TEXT, updated_at TEXT NOT NULL,
            FOREIGN KEY (competition_id) REFERENCES afl_competitions(afl_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS afl_teams (
            afl_id INTEGER PRIMARY KEY, provider_id TEXT UNIQUE, season_id INTEGER NOT NULL,
            name TEXT, abbreviation TEXT, nickname TEXT, display_name TEXT, short_name TEXT,
            team_type TEXT, metadata_json TEXT, club_json TEXT, source_json TEXT,
            updated_at TEXT NOT NULL, FOREIGN KEY (season_id) REFERENCES afl_seasons(afl_id)
        )
    """)
    for definition in (
        "provider_id TEXT", "round_number INTEGER", "abbreviation TEXT",
        "start_time TEXT", "end_time TEXT", "byes_json TEXT", "metadata_json TEXT",
        "source_json TEXT", "updated_at TEXT",
    ):
        _add_column(conn, "rounds", definition)
    for definition in (
        "season_id INTEGER", "home_team_id INTEGER", "away_team_id INTEGER",
        "home_json TEXT", "away_json TEXT", "venue_json TEXT", "metadata_json TEXT",
        "source_json TEXT", "updated_at TEXT",
    ):
        _add_column(conn, "matches", definition)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_rounds_provider_id ON rounds(provider_id) WHERE provider_id IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_provider_id ON matches(match_provider_id) WHERE match_provider_id IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_afl_seasons_year ON afl_seasons(competition_id, year)")
