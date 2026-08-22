"""Diagnostic-only CFS matchInterchange evidence capture for the ``interchange``
diagnostic profile (Issue #193).

This table is written to only when the ``interchange`` diagnostic profile is
selected (AFL_DIAGNOSTICS_ENABLED=true, AFL_DIAGNOSTIC_PROFILES includes
``interchange``). It exists purely to retain evidence for manual/future
analysis and is never read by scheduler decision-making or the consumer API.

Kept as its own table, separate from match_clock's
``match_state_evidence_observations`` (migration 0016), rather than a shared
generic diagnostics schema -- see docs/diagnostics_framework.md for why the
framework keeps one table per profile at this stage.
"""

MIGRATION_ID = "0017"
DESCRIPTION = "Add diagnostic match-interchange evidence observations"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS match_interchange_evidence_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            match_provider_id TEXT NOT NULL,
            poll_sequence INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            match_status_at_poll TEXT,
            home_interchange_json TEXT NOT NULL,
            away_interchange_json TEXT NOT NULL,
            home_counts_json TEXT NOT NULL,
            away_counts_json TEXT NOT NULL,
            is_transition INTEGER NOT NULL CHECK(is_transition IN (0,1)),
            transition_flags_json TEXT NOT NULL,
            raw_match_interchange_json TEXT,
            collector_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_provider_id, poll_sequence),
            FOREIGN KEY(match_id) REFERENCES matches(match_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_evidence_match ON match_interchange_evidence_observations(match_id, poll_sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_evidence_provider ON match_interchange_evidence_observations(match_provider_id, poll_sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_evidence_transitions ON match_interchange_evidence_observations(match_id, is_transition)")
