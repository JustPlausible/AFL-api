"""Add durable match-window plans and leases for CFS match statistics."""

MIGRATION_ID = "0013"
DESCRIPTION = "Add durable match stat windows"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS match_stat_windows (
            window_id TEXT PRIMARY KEY,
            match_id INTEGER NOT NULL,
            afl_match_id INTEGER,
            match_provider_id TEXT,
            champion_data_match_id TEXT GENERATED ALWAYS AS (match_provider_id) VIRTUAL,
            competition_id TEXT,
            season_id TEXT,
            policy_version TEXT NOT NULL,
            lifecycle TEXT NOT NULL,
            collection_phase TEXT NOT NULL CHECK(collection_phase IN ('not_started','pre_match','live','post_game','final_confirmation','complete','none')),
            status TEXT NOT NULL CHECK(status IN ('planned','due','leased','backoff','awaiting_final','planning_error','complete','failed_terminal','disabled','cancelled','not_applicable')),
            next_due_at TEXT,
            cadence_profile TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
            last_attempted_at TEXT,
            last_successful_collection_at TEXT,
            last_successful_write_at TEXT,
            last_observed_snapshot_authority INTEGER CHECK(last_observed_snapshot_authority IN (1,2) OR last_observed_snapshot_authority IS NULL),
            finality_state TEXT NOT NULL CHECK(finality_state IN ('not_applicable','unconfirmed','partial','authoritative_complete')),
            lease_owner TEXT,
            lease_token TEXT,
            lease_generation INTEGER NOT NULL DEFAULT 0,
            lease_claimed_at TEXT,
            lease_expires_at TEXT,
            lifecycle_observed_at TEXT,
            reason_code TEXT NOT NULL,
            diagnostic_summary TEXT CHECK(length(diagnostic_summary) <= 500 OR diagnostic_summary IS NULL),
            planner_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(match_id) REFERENCES matches(match_id)
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_match_stat_windows_active_policy
        ON match_stat_windows(match_id, policy_version)
        WHERE status IN ('planned','due','leased','backoff','awaiting_final','planning_error','disabled')
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_stat_windows_due ON match_stat_windows(status, next_due_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_stat_windows_lease_expiry ON match_stat_windows(status, lease_expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_stat_windows_match ON match_stat_windows(match_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_stat_windows_provider ON match_stat_windows(match_provider_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_stat_windows_status ON match_stat_windows(status)")
