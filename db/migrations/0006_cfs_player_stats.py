"""Add canonical current CFS player-stat observations."""

MIGRATION_ID = "0006"
DESCRIPTION = "Add canonical CFS match player statistics"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfs_player_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_provider_id TEXT NOT NULL,
            champion_data_player_id TEXT NOT NULL,
            afl_match_id TEXT,
            team_provider_id TEXT,
            side TEXT NOT NULL CHECK(side IN ('home', 'away')),
            collected_at TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            source_status TEXT,
            snapshot_authority INTEGER NOT NULL CHECK(snapshot_authority IN (1, 2)),
            goals NUMERIC, behinds NUMERIC, kicks NUMERIC, handballs NUMERIC,
            disposals NUMERIC, marks NUMERIC, tackles NUMERIC, hitouts NUMERIC,
            extra_stats_json TEXT NOT NULL,
            raw_player_json TEXT NOT NULL,
            UNIQUE(match_provider_id, champion_data_player_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_player_stats_match ON cfs_player_stats(match_provider_id)")
