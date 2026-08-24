"""Production CFS match-interchange persistence (Issue #204).

Production promotion of the Issue #193 diagnostic-only evidence capture
(``collection/match_interchange_evidence.py``, migration
``0017_match_interchange_evidence.py``). These are **new, separate** tables --
the diagnostic ``match_interchange_evidence_observations`` table is untouched
and keeps running independently for evidence/debugging purposes (see
``docs/diagnostics_framework.md``). The consumer API and the production
scheduler read only the tables defined here.

Real evidence reviewed for this promotion includes a single captured
concluded-match snapshot (``tests/fixtures/afl/interchange/match_interchange_8216_concluded.json``),
real Round 24 live diagnostic observations across 7 matches
(``scripts/report_interchange_evidence.py`` output reviewed during PR #206)
confirming ``homeInterchange[]``/``awayInterchange[]`` array membership
genuinely changes during LIVE play, and a follow-up full per-poll export
for one match individually confirming a named player's repeated
appear/disappear/reappear cycle and a byte-for-byte state freeze across 40
real POSTGAME polls -- see ``afl_json/match_interchange.py`` module
docstring "Array-membership semantics: confirmed by real Round 24 live
evidence" for the full evidence. Persistence here therefore stores the
membership signal as ``on_bench``, confirmed for LIVE play and POSTGAME;
only CONCLUDED behaviour remains unverified.

Three tables, mirroring the shape already proven for commentary (migration
``0019``) but adapted for interchange's "current per-player state" shape
rather than an append-only event stream:

* ``match_interchange_state`` -- one row per unique ``(match_provider_id,
  player_provider_id)`` pair observed in either interchange array, holding
  the latest known field values (``interchange_count``, ``bench_reason``,
  ``time_on_ground``, ``time_on_bench``, ``power_rating``) and the current
  ``on_bench`` membership flag. This is the table the consumer
  API's current-state route reads. Carries both the source Champion Data
  ``playerId``/``teamId`` and, where resolvable, the linked canonical
  AFL-api identity -- re-resolved on every update (unlike commentary's
  immutable events) so a crosswalk added after first observation still
  self-heals a *current*-state row.
* ``match_interchange_events`` -- append-only, meaningful-only transition
  history: a player appearing in / disappearing from an interchange array,
  or ``interchange_count``/``bench_reason`` changing. Never written merely
  because ``time_on_ground``/``time_on_bench``/``power_rating`` ticked on a
  poll where nothing else changed -- see ``afl_json.match_interchange.persist_match_interchange``.
* ``match_interchange_polls`` -- lightweight per-match poll bookkeeping
  (sequence continuity across restarts, endpoint outcome, feed metadata),
  mirroring ``match_commentary_polls``. Never retains the raw feed payload.
"""

MIGRATION_ID = "0021"
DESCRIPTION = "Add production match-interchange state, event and poll tables"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS match_interchange_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            match_provider_id TEXT NOT NULL,
            player_provider_id TEXT NOT NULL,
            canonical_player_id INTEGER,
            team_provider_id TEXT,
            canonical_team_id INTEGER,
            side TEXT NOT NULL CHECK(side IN ('home','away')),
            on_bench INTEGER NOT NULL CHECK(on_bench IN (0,1)),
            interchange_count INTEGER,
            bench_reason TEXT,
            time_on_ground INTEGER,
            time_on_bench INTEGER,
            power_rating INTEGER,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            last_transition_at TEXT NOT NULL,
            match_status_at_last_observation TEXT,
            collector_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_provider_id, player_provider_id),
            FOREIGN KEY(match_id) REFERENCES matches(match_id),
            FOREIGN KEY(canonical_player_id) REFERENCES canonical_players(id),
            FOREIGN KEY(canonical_team_id) REFERENCES afl_teams(afl_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_state_match ON match_interchange_state(match_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_state_provider ON match_interchange_state(match_provider_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_state_player ON match_interchange_state(match_id, canonical_player_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_state_on_bench ON match_interchange_state(match_id, on_bench)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS match_interchange_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            match_provider_id TEXT NOT NULL,
            player_provider_id TEXT NOT NULL,
            canonical_player_id INTEGER,
            team_provider_id TEXT,
            canonical_team_id INTEGER,
            side TEXT NOT NULL CHECK(side IN ('home','away')),
            event_type TEXT NOT NULL CHECK(event_type IN
                ('appeared', 'disappeared', 'interchange_count_changed', 'bench_reason_changed')),
            interchange_count INTEGER,
            previous_interchange_count INTEGER,
            bench_reason TEXT,
            previous_bench_reason TEXT,
            time_on_ground INTEGER,
            time_on_bench INTEGER,
            power_rating INTEGER,
            observed_at TEXT NOT NULL,
            match_status_at_poll TEXT,
            source TEXT NOT NULL DEFAULT 'cfs_match_interchange',
            collector_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(match_id) REFERENCES matches(match_id),
            FOREIGN KEY(canonical_player_id) REFERENCES canonical_players(id),
            FOREIGN KEY(canonical_team_id) REFERENCES afl_teams(afl_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_events_match_time ON match_interchange_events(match_id, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_events_provider ON match_interchange_events(match_provider_id, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_events_player ON match_interchange_events(match_id, canonical_player_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_events_type ON match_interchange_events(match_id, event_type)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS match_interchange_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            match_provider_id TEXT NOT NULL,
            poll_sequence INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            match_status_at_poll TEXT,
            outcome TEXT NOT NULL,
            home_count_in_feed INTEGER,
            away_count_in_feed INTEGER,
            collector_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_provider_id, poll_sequence),
            FOREIGN KEY(match_id) REFERENCES matches(match_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_polls_match ON match_interchange_polls(match_id, poll_sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_polls_provider ON match_interchange_polls(match_provider_id, poll_sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_interchange_polls_status ON match_interchange_polls(match_status_at_poll, observed_at)")
