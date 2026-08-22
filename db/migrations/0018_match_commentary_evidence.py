"""Diagnostic-only CFS commentaryFeed evidence capture for the ``commentary``
diagnostic profile (Issue #196).

These tables are written to only when the ``commentary`` diagnostic profile
is selected (AFL_DIAGNOSTICS_ENABLED=true, AFL_DIAGNOSTIC_PROFILES includes
``commentary``). They exist purely to retain evidence for manual/future
analysis and are never read by scheduler decision-making or the consumer API.

Kept as their own tables, separate from match_clock's
``match_state_evidence_observations`` (migration 0016) and interchange's
``match_interchange_evidence_observations`` (migration 0017) -- see
docs/diagnostics_framework.md for why the framework keeps one table (or
table set) per profile at this stage.

Two tables, not one, because commentary's evidence has a materially
different shape from a per-poll snapshot profile: the endpoint returns an
*accumulated* feed, so the meaningful unit of evidence is a deduplicated
*event* (``commentary_evidence_events``), while poll-level bookkeeping
(sequence continuity, endpoint outcome/availability, feed-level metadata)
is tracked separately (``commentary_evidence_polls``). See
``collection/match_commentary_evidence.py`` for the full persistence policy.
"""

MIGRATION_ID = "0018"
DESCRIPTION = "Add diagnostic match-commentary evidence poll and event tables"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS commentary_evidence_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            match_provider_id TEXT NOT NULL,
            poll_sequence INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            match_status_at_poll TEXT,
            outcome TEXT NOT NULL,
            feed_last_updated TEXT,
            event_count_in_feed INTEGER,
            new_event_count INTEGER,
            is_transition INTEGER NOT NULL CHECK(is_transition IN (0,1)),
            transition_flags_json TEXT NOT NULL,
            raw_commentary_json TEXT,
            collector_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_provider_id, poll_sequence),
            FOREIGN KEY(match_id) REFERENCES matches(match_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_commentary_evidence_polls_match ON commentary_evidence_polls(match_id, poll_sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_commentary_evidence_polls_provider ON commentary_evidence_polls(match_provider_id, poll_sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_commentary_evidence_polls_transitions ON commentary_evidence_polls(match_id, is_transition)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS commentary_evidence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            match_provider_id TEXT NOT NULL,
            event_fingerprint TEXT NOT NULL,
            slot_key TEXT NOT NULL,
            period_number INTEGER,
            period_seconds INTEGER,
            comment TEXT,
            player_id TEXT,
            team_id TEXT,
            score_event INTEGER,
            category TEXT,
            first_observed_at TEXT NOT NULL,
            first_observed_poll_sequence INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_seen_poll_sequence INTEGER NOT NULL,
            first_seen_feed_last_updated TEXT,
            last_seen_feed_last_updated TEXT,
            possible_edit_of_event_id INTEGER,
            raw_event_json TEXT NOT NULL,
            collector_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_provider_id, event_fingerprint),
            FOREIGN KEY(match_id) REFERENCES matches(match_id),
            FOREIGN KEY(possible_edit_of_event_id) REFERENCES commentary_evidence_events(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_commentary_evidence_events_match ON commentary_evidence_events(match_id, first_observed_poll_sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_commentary_evidence_events_provider ON commentary_evidence_events(match_provider_id, first_observed_poll_sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_commentary_evidence_events_category ON commentary_evidence_events(match_id, category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_commentary_evidence_events_slot ON commentary_evidence_events(match_provider_id, slot_key)")
