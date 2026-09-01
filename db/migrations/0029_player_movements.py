"""Add supplemental AFL editorial player-movement observations."""
MIGRATION_ID="0029"
DESCRIPTION="Add AFL editorial player movement snapshot evidence"
def migrate(conn):
 conn.execute('''CREATE TABLE IF NOT EXISTS player_movement_observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_player_id INTEGER, movement_season_year INTEGER NOT NULL,
 from_team_id INTEGER, movement_type TEXT NOT NULL CHECK(movement_type IN ('RETIRED','DELISTED','TRADED','FREE_AGENT','DELISTED_FREE_AGENT','OTHER')),
 source_label TEXT NOT NULL, source_detail TEXT, source_player_name TEXT NOT NULL, source_team_name TEXT NOT NULL,
 article_url TEXT, source_family TEXT NOT NULL DEFAULT 'AFL_EDITORIAL' CHECK(source_family='AFL_EDITORIAL'),
 source_url TEXT NOT NULL, source_archived_at TEXT, source_snapshot_at TEXT NOT NULL, observed_at TEXT NOT NULL,
 resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved','unresolved','ambiguous')), resolution_reason TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(canonical_player_id) REFERENCES canonical_players(id), FOREIGN KEY(from_team_id) REFERENCES afl_teams(afl_id),
 UNIQUE(movement_season_year,source_url,source_snapshot_at,source_team_name,source_player_name,source_label)
 )''')
 conn.execute('CREATE INDEX IF NOT EXISTS idx_player_movements_player ON player_movement_observations(canonical_player_id,movement_season_year)')
 conn.execute('CREATE INDEX IF NOT EXISTS idx_player_movements_snapshot ON player_movement_observations(source_url,source_snapshot_at)')
