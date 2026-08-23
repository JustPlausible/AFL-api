"""Production CFS match-commentary persistence (Issue #201).

Follow-up to the diagnostic-only evidence capture added in migration
``0018_match_commentary_evidence.py`` (Issue #196). These are **new, separate**
tables -- the diagnostic ``commentary_evidence_polls``/``commentary_evidence_events``
tables are untouched and keep running independently for evidence/debugging
purposes (see ``docs/diagnostics_framework.md``). The consumer API and the
production scheduler read only the tables defined here.

Two tables, mirroring the shape that already proved itself for diagnostic
capture, but deliberately lighter:

* ``match_commentary_events`` -- one row per unique commentary event,
  deduplicated by a stable fingerprint over the source fields (the endpoint
  supplies no upstream event id -- see ``afl_json/match_commentary.py``).
  This is the table the consumer API reads. Carries both the source
  Champion Data ``playerId``/``teamId`` and, where resolvable, the linked
  canonical AFL-api identity -- never guessed, left NULL when unresolved.
  ``possible_edit_of_event_id`` is a heuristic, non-destructive link to an
  earlier event that this one likely republishes/corrects (e.g. an official
  score review): the earlier row is never overwritten or deleted, so the
  full source timeline stays intact.
* ``match_commentary_polls`` -- lightweight per-match poll bookkeeping
  (sequence continuity across restarts, endpoint outcome, feed metadata).
  Unlike the diagnostic poll table, this **never** retains the raw feed
  payload -- only ``match_commentary_events.raw_event_json`` retains a raw
  per-event payload, once, at first observation. This keeps production
  storage from duplicating the diagnostic evidence infrastructure while
  still giving the production scheduler a durable, restart-safe basis for
  poll-sequence numbering and live/postgame candidate selection.
"""

MIGRATION_ID = "0019"
DESCRIPTION = "Add production match-commentary event and poll tables"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS match_commentary_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            match_provider_id TEXT NOT NULL,
            event_fingerprint TEXT NOT NULL,
            slot_key TEXT NOT NULL,
            period_number INTEGER,
            period_seconds INTEGER,
            comment TEXT,
            score_event INTEGER,
            player_provider_id TEXT,
            canonical_player_id INTEGER,
            team_provider_id TEXT,
            canonical_team_id INTEGER,
            category TEXT,
            source_index INTEGER NOT NULL,
            possible_edit_of_event_id INTEGER,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            source_feed_last_updated TEXT,
            last_seen_feed_last_updated TEXT,
            source TEXT NOT NULL DEFAULT 'cfs_commentary_feed',
            raw_event_json TEXT NOT NULL,
            collector_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_provider_id, event_fingerprint),
            FOREIGN KEY(match_id) REFERENCES matches(match_id),
            FOREIGN KEY(canonical_player_id) REFERENCES canonical_players(id),
            FOREIGN KEY(canonical_team_id) REFERENCES afl_teams(afl_id),
            FOREIGN KEY(possible_edit_of_event_id) REFERENCES match_commentary_events(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_commentary_events_match_clock ON match_commentary_events(match_id, period_number, period_seconds)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_commentary_events_provider ON match_commentary_events(match_provider_id, first_observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_commentary_events_player ON match_commentary_events(match_id, canonical_player_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_commentary_events_team ON match_commentary_events(match_id, canonical_team_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_commentary_events_score ON match_commentary_events(match_id, score_event)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_commentary_events_slot ON match_commentary_events(match_provider_id, slot_key)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS match_commentary_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            match_provider_id TEXT NOT NULL,
            poll_sequence INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            match_status_at_poll TEXT,
            outcome TEXT NOT NULL,
            event_count_in_feed INTEGER,
            new_event_count INTEGER,
            feed_last_updated TEXT,
            collector_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_provider_id, poll_sequence),
            FOREIGN KEY(match_id) REFERENCES matches(match_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_commentary_polls_match ON match_commentary_polls(match_id, poll_sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_commentary_polls_provider ON match_commentary_polls(match_provider_id, poll_sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_commentary_polls_status ON match_commentary_polls(match_status_at_poll, observed_at)")
