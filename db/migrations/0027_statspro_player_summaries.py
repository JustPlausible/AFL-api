"""Add current AFL StatsPro season and round published summaries."""

MIGRATION_ID = "0027"
DESCRIPTION = "Add AFL StatsPro player season and round summaries"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS statspro_player_season_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_player_id INTEGER,
            player_provider_id TEXT NOT NULL,
            season_id INTEGER NOT NULL,
            season_provider_id TEXT NOT NULL,
            team_id INTEGER,
            team_provider_id TEXT,
            source TEXT NOT NULL CHECK(source = 'AFL_STATSPRO'),
            source_context TEXT NOT NULL CHECK(source_context = 'SEASON_TOTAL'),
            scope TEXT NOT NULL CHECK(scope = 'full_season'),
            games_played INTEGER NOT NULL CHECK(games_played >= 0),
            published_totals TEXT NOT NULL,
            published_averages TEXT NOT NULL,
            source_updated_at TEXT,
            collected_at TEXT NOT NULL,
            FOREIGN KEY(canonical_player_id) REFERENCES canonical_players(id),
            FOREIGN KEY(season_id) REFERENCES afl_seasons(afl_id),
            FOREIGN KEY(team_id) REFERENCES afl_teams(afl_id),
            UNIQUE(season_id, player_provider_id, source_context)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_statspro_season_context ON statspro_player_season_summaries(season_id, source_context, canonical_player_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_statspro_season_team ON statspro_player_season_summaries(season_id, team_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS statspro_player_round_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_player_id INTEGER,
            player_provider_id TEXT NOT NULL,
            season_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            round_provider_id TEXT NOT NULL,
            team_id INTEGER,
            team_provider_id TEXT,
            opponent_provider_id TEXT,
            result_context TEXT,
            source TEXT NOT NULL CHECK(source = 'AFL_STATSPRO'),
            source_context TEXT NOT NULL CHECK(source_context = 'LEAGUE_ROUND_TOTAL'),
            published_totals TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            FOREIGN KEY(canonical_player_id) REFERENCES canonical_players(id),
            FOREIGN KEY(season_id) REFERENCES afl_seasons(afl_id),
            FOREIGN KEY(round_id) REFERENCES rounds(round_id),
            UNIQUE(round_id, player_provider_id, source_context)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_statspro_round_season_round ON statspro_player_round_summaries(season_id, round_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_statspro_round_player_season ON statspro_player_round_summaries(canonical_player_id, season_id)")
