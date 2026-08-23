"""Add append-only player-stat history and sparse period checkpoints (Issue #195).

Follow-up to migration 0006 (``cfs_player_stats``) and the Issue #187
``MatchPeriodState`` contract (``afl_json/match_period.py``). ``cfs_player_stats``
remains the sole authoritative current/final player-stat model; these are new,
additive tables only -- nothing here changes its upsert/finality semantics.

* ``cfs_player_stat_history`` -- append-only observed field-level transitions
  between successive *accepted* canonical player-stat observations for a
  player. A row is written only when ``afl_json.player_stats.upsert_player_stats``
  actually accepted and applied the incoming observation (the existing
  snapshot-authority/finality guard on ``cfs_player_stats`` already decided
  that); a rejected/stale observation never produces a history row, and an
  identical repeated observation produces none either. The very first
  accepted observation for a player is a baseline, not history -- see the
  ``BASELINE`` checkpoint marker below rather than a flood of ``NULL`` ->
  value rows.
* ``cfs_player_stat_checkpoints`` -- sparse, full canonical-stat-line
  snapshots at shared period/finality markers (``BASELINE``, ``QT``, ``HT``,
  ``3QT``, ``FT``, ``CONCLUDED``). ``FT`` is the internal ``MatchPeriodState``
  marker for Q4 completing; ``CONCLUDED`` is the separate, later authoritative
  match-lifecycle marker (Issue #187: Q4/``FT`` completing does not itself
  imply ``CONCLUDED``) -- both can therefore coexist for the same player when
  a postgame adjustment changes the canonical line after ``FT`` but before
  ``CONCLUDED``. The unique constraint below is what stops repeated polling
  during a break (e.g. halftime) from writing duplicate checkpoint rows.
"""

MIGRATION_ID = "0020"
DESCRIPTION = "Add append-only CFS player-stat history and period checkpoints"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfs_player_stat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_provider_id TEXT NOT NULL,
            afl_match_id TEXT,
            champion_data_player_id TEXT NOT NULL,
            canonical_player_id INTEGER REFERENCES canonical_players(id),
            observed_at TEXT NOT NULL,
            match_period_state TEXT,
            stat_field TEXT NOT NULL,
            previous_value NUMERIC,
            new_value NUMERIC,
            delta NUMERIC,
            source_endpoint TEXT NOT NULL,
            resolved_match_status TEXT,
            snapshot_authority INTEGER NOT NULL CHECK(snapshot_authority IN (1, 2)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_player_stat_history_match_time "
                 "ON cfs_player_stat_history(match_provider_id, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_player_stat_history_match_player_time "
                 "ON cfs_player_stat_history(match_provider_id, champion_data_player_id, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_player_stat_history_match_player_field_time "
                 "ON cfs_player_stat_history(match_provider_id, champion_data_player_id, stat_field, observed_at)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfs_player_stat_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_provider_id TEXT NOT NULL,
            afl_match_id TEXT,
            champion_data_player_id TEXT NOT NULL,
            canonical_player_id INTEGER REFERENCES canonical_players(id),
            checkpoint_marker TEXT NOT NULL CHECK(checkpoint_marker IN
                ('BASELINE', 'QT', 'HT', '3QT', 'FT', 'CONCLUDED')),
            observed_at TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            resolved_match_status TEXT,
            snapshot_authority INTEGER NOT NULL CHECK(snapshot_authority IN (1, 2)),
            goals NUMERIC, behinds NUMERIC, kicks NUMERIC, handballs NUMERIC,
            disposals NUMERIC, marks NUMERIC, tackles NUMERIC, hitouts NUMERIC,
            UNIQUE(match_provider_id, champion_data_player_id, checkpoint_marker)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_player_stat_checkpoints_match_marker "
                 "ON cfs_player_stat_checkpoints(match_provider_id, checkpoint_marker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_player_stat_checkpoints_match_player "
                 "ON cfs_player_stat_checkpoints(match_provider_id, champion_data_player_id, checkpoint_marker)")
