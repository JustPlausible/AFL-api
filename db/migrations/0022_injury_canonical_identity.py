"""Persist canonical player/team identity on injury rows (Issue #213).

The injury collection pipeline's ``InjuryResolver`` already resolves a
source injury row to a ``canonical_player_id`` (and, via the canonical club
seed, a canonical ``team_id``), but the ``injuries`` table previously kept
only the source AFL id, club code, and player name. This migration adds the
two canonical columns so consumers -- in particular the new
``/api/v1/injuries`` resource -- can use canonical identity directly instead
of re-deriving it at read time.

Backfill is deliberately conservative and deterministic only:

* ``canonical_player_id`` is backfilled from ``player_provider_ids`` only
  when a row's ``afl_id`` maps to *exactly one* canonical player via a
  persisted ``provider = 'afl'`` crosswalk. An ambiguous or absent mapping
  is left ``NULL`` rather than guessed.
* ``canonical_team_id`` is backfilled from the persisted ``club`` code
  (already the canonical club code assigned by the resolver) using the
  committed club seed's ``teamId``, the same identifier space as
  ``afl_teams.afl_id``. An unresolvable code is left ``NULL``, and so is a
  code whose seeded ``teamId`` has no matching ``afl_teams`` row yet -- the
  column has a ``REFERENCES afl_teams(afl_id)`` foreign key and the
  migration runner enables ``PRAGMA foreign_keys = ON``, so backfilling a
  team id the database hasn't bootstrapped yet would fail the migration
  outright on a database that has legacy injury rows without a populated
  ``afl_teams`` table.

Both lookups use only pure, already-validated data (``player_provider_ids``
already in this connection's transaction, and the static committed club
seed) -- neither opens a second database connection, which would contend
with the migration runner's own transaction.
"""

from db.club_seed import load_club_seed

MIGRATION_ID = "0022"
DESCRIPTION = "Persist canonical player/team identity on injury rows"


def migrate(conn):
    conn.execute("ALTER TABLE injuries ADD COLUMN canonical_player_id INTEGER REFERENCES canonical_players(id)")
    conn.execute("ALTER TABLE injuries ADD COLUMN canonical_team_id INTEGER REFERENCES afl_teams(afl_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_injuries_canonical_player ON injuries(canonical_player_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_injuries_canonical_team ON injuries(canonical_team_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_injuries_current ON injuries(current)")

    conn.execute("""
        UPDATE injuries
        SET canonical_player_id = (
            SELECT ppi.player_id FROM player_provider_ids ppi
            WHERE ppi.provider = 'afl' AND ppi.provider_player_id = CAST(injuries.afl_id AS TEXT)
        )
        WHERE canonical_player_id IS NULL
          AND (
            SELECT COUNT(*) FROM player_provider_ids ppi
            WHERE ppi.provider = 'afl' AND ppi.provider_player_id = CAST(injuries.afl_id AS TEXT)
          ) = 1
    """)

    existing_team_ids = {row[0] for row in conn.execute("SELECT afl_id FROM afl_teams").fetchall()}
    team_id_by_code = {club["code"]: club["teamId"] for club in load_club_seed()}
    club_codes = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT club FROM injuries WHERE club IS NOT NULL AND canonical_team_id IS NULL"
        ).fetchall()
    ]
    for code in club_codes:
        team_id = team_id_by_code.get(code)
        if team_id is None or team_id not in existing_team_ids:
            continue
        conn.execute(
            "UPDATE injuries SET canonical_team_id = ? WHERE club = ? AND canonical_team_id IS NULL",
            (team_id, code),
        )
