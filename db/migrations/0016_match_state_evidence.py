"""Diagnostic-only CFS matchItem evidence capture for quarter/half/three-quarter/
full-time investigation (Issue #148).

This table is written to only when AFL_CAPTURE_MATCH_STATE_EVIDENCE is enabled.
It exists purely to retain evidence for manual/future analysis and is never
read by scheduler decision-making.
"""

MIGRATION_ID = "0016"
DESCRIPTION = "Add diagnostic match-state evidence observations"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS match_state_evidence_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            match_provider_id TEXT NOT NULL,
            poll_sequence INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            match_status TEXT,
            score_status TEXT,
            periods_json TEXT NOT NULL,
            latest_period_number INTEGER,
            latest_period_seconds INTEGER,
            latest_period_completed INTEGER CHECK(latest_period_completed IN (0,1) OR latest_period_completed IS NULL),
            is_transition INTEGER NOT NULL CHECK(is_transition IN (0,1)),
            transition_flags_json TEXT NOT NULL,
            raw_match_item_json TEXT,
            collector_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_provider_id, poll_sequence),
            FOREIGN KEY(match_id) REFERENCES matches(match_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_state_evidence_match ON match_state_evidence_observations(match_id, poll_sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_state_evidence_provider ON match_state_evidence_observations(match_provider_id, poll_sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_state_evidence_transitions ON match_state_evidence_observations(match_id, is_transition)")
