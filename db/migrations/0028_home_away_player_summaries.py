"""Add canonical round phase and derived Home & Away player summaries."""

MIGRATION_ID = "0028"
DESCRIPTION = "Add derived Home and Away player season summaries"


def migrate(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(rounds)")}
    if "competition_phase" not in columns:
        conn.execute("ALTER TABLE rounds ADD COLUMN competition_phase TEXT "
                     "CHECK(competition_phase IN ('HOME_AND_AWAY','FINALS'))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rounds_season_phase "
                 "ON rounds(season_id,competition_phase)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS derived_player_season_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id INTEGER NOT NULL,
            canonical_player_id INTEGER NOT NULL,
            team_id INTEGER,
            scope TEXT NOT NULL CHECK(scope='home_and_away'),
            source TEXT NOT NULL CHECK(source='DERIVED_MATCH_STATS'),
            population_source TEXT NOT NULL,
            games_played INTEGER NOT NULL CHECK(games_played >= 0),
            totals TEXT NOT NULL,
            derived_rates TEXT NOT NULL,
            built_at TEXT NOT NULL,
            source_max_updated_at TEXT,
            finalized INTEGER NOT NULL CHECK(finalized IN (0,1)),
            FOREIGN KEY(season_id) REFERENCES afl_seasons(afl_id),
            FOREIGN KEY(canonical_player_id) REFERENCES canonical_players(id),
            FOREIGN KEY(team_id) REFERENCES afl_teams(afl_id),
            UNIQUE(season_id,canonical_player_id,scope)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_derived_summary_season_team "
                 "ON derived_player_season_summaries(season_id,team_id,canonical_player_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_derived_summary_player_season "
                 "ON derived_player_season_summaries(canonical_player_id,season_id)")
