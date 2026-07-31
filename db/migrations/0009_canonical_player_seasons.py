"""Add canonical player identities and competition-season membership."""

MIGRATION_ID = "0009"
DESCRIPTION = "Add canonical player identity and season associations"


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS canonical_players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT,
            given_name TEXT,
            family_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_provider_ids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            provider_player_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (player_id) REFERENCES canonical_players(id),
            UNIQUE (provider, provider_player_id),
            UNIQUE (player_id, provider)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS afl_team_seasons (
            competition_season_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (competition_season_id, team_id),
            FOREIGN KEY (competition_season_id) REFERENCES afl_seasons(afl_id),
            FOREIGN KEY (team_id) REFERENCES afl_teams(afl_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS competition_season_players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            competition_season_id INTEGER NOT NULL,
            team_id INTEGER,
            source_provider TEXT NOT NULL,
            jumper_number INTEGER,
            listed_position TEXT,
            photo_url TEXT,
            source_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (player_id) REFERENCES canonical_players(id),
            FOREIGN KEY (competition_season_id) REFERENCES afl_seasons(afl_id),
            FOREIGN KEY (competition_season_id, team_id)
                REFERENCES afl_team_seasons(competition_season_id, team_id),
            UNIQUE (player_id, competition_season_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_player_provider_ids_player ON player_provider_ids(player_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_season_players_season_team ON competition_season_players(competition_season_id, team_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_season_players_player ON competition_season_players(player_id)")
    if "canonical_player_id" not in _columns(conn, "cfs_player_stats"):
        conn.execute("ALTER TABLE cfs_player_stats ADD COLUMN canonical_player_id INTEGER REFERENCES canonical_players(id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_player_stats_canonical_player ON cfs_player_stats(canonical_player_id)")
