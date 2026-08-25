"""Production CFS match-roster (selection) persistence (Issue #219).

Promotes the read-only ``afl_json.rosters.MatchRosterCollector`` into canonical
persistence, mirroring the shape already proven for match interchange
(migration ``0021``) but adapted for a roster's "full snapshot per publish"
contract rather than interchange's incremental per-player membership tracking.

## Core semantic rule

A CFS roster is a **team selection**, not proof of participation. These
tables never claim, and the ``/api/v1`` roster resource built on top of them
never implies, that a selected player took the field. Actual participation
remains the sole responsibility of ``cfs_player_stats``.

## Three tables

* ``cfs_match_rosters`` -- one row per ``(match_provider_id, team_provider_id)``
  pair, holding the source ``teamStatus``, the roster-level ``matchRoster.status``
  and ``lastUpdated`` observed with it, and canonical match/team identity where
  resolvable. This is the row the consumer API reads for per-team roster
  metadata (see ``docs/api_v1_rosters.md``).
* ``cfs_match_roster_selections`` -- current positional-selection membership:
  one row per ``(match_provider_id, team_provider_id, player_provider_id)``
  observed in the team's ``positions`` groups. ``position`` is the CFS group
  name (e.g. ``FORWARDS``, ``INTERCHANGE``) exactly as supplied; ``group_order``/
  ``player_order`` are retained only as non-authoritative presentation
  metadata (see ``afl_json.rosters`` module docstring on provider ordering).
* ``cfs_match_roster_context`` -- ``ins``/``outs``/``lateChanges``/``clubDebuts``/
  ``milestones`` change/context records, deliberately kept in a table separate
  from selections so a change record can never be read as lineup membership.
  ``context_type`` identifies which of the five supported list-shaped source
  collections a row came from.

## Current-state, not append-only history

Unlike ``match_interchange_events``, there is no separate roster event-history
table. Repository evidence (``docs/match_rosters.md``: a pre-bounce and a LIVE
capture of the same match differed only in the roster timestamp) does not
establish that roster selections change often enough in observed practice to
justify an event log; the existing ``afl_json.rosters.compare_rosters`` diff
already treats each valid published response as the complete current
selection/context state for that team. This migration mirrors that model:
``afl_json.rosters.persist_match_rosters`` fully replaces (delete absent,
upsert present) a team's selection and per-context-type rows on every
``RosterStatus.PUBLISHED`` observation. A player's row is *updated* in place
when e.g. their position changes -- ``first_observed_at`` is preserved,
``last_observed_at``/``position`` are refreshed -- rather than duplicated, so
this is the smallest durable schema evidence currently supports. If future
live evidence shows selection churn that a current-state table cannot
usefully represent, an append-only history table can be added later without
altering this shape.

## Replacement safety

``persist_match_rosters`` never runs (and these tables are therefore never
touched) for an ``UNAVAILABLE`` (top-level ``null``) or ``EMPTY`` (top-level
``[]``) round observation -- see ``afl_json.rosters.RosterStatus`` and
``afl_json.rosters.compare_rosters``'s existing ``replacement_safe`` gate,
reused unchanged here. A malformed/partial response never reaches persistence
at all: ``MatchRosterCollector.collect`` raises before returning a result, so
the caller (the production scheduler poller, or a manual
``collect_operational`` trigger) never calls ``persist_match_rosters`` for
that observation and any previously persisted roster is left untouched.

## Canonical identity resolution

Reuses the same crosswalks as every other CFS production table added so far:
``player_provider_ids`` (``provider='champion_data'``) for players, and
``afl_teams.provider_id`` for teams -- see
``afl_json.rosters.resolve_canonical_player``/``resolve_canonical_team``.
Unresolved identities persist as ``NULL`` canonical columns, never guessed
from name/jumper number. Canonical identity is re-resolved on every write (a
current-state table, like ``match_interchange_state``), so a crosswalk added
after a player's first roster observation still self-heals the row on the
next valid publish -- no separate backfill/repair path is required.

## Legacy ``lineups`` boundary

This migration does not touch, read, or write the legacy ``lineups`` table
(rendered-HTML operational persistence, migration ``0001``). That path
remains the unversioned lineup routes' own compatibility authority; see
``docs/architecture/data_authority_map.md`` and
``collection/source_policy.py``'s ``OperationalDomain.LINEUPS`` policy, which
is unchanged by this migration.
"""

MIGRATION_ID = "0024"
DESCRIPTION = "Add production CFS match-roster selection and context tables (Issue #219)"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfs_match_rosters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            match_provider_id TEXT NOT NULL,
            round_provider_id TEXT NOT NULL,
            team_provider_id TEXT NOT NULL,
            canonical_team_id INTEGER,
            side TEXT NOT NULL CHECK(side IN ('home','away')),
            team_status TEXT,
            match_status_at_observation TEXT,
            source_last_updated TEXT,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            collector_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_provider_id, team_provider_id),
            FOREIGN KEY(match_id) REFERENCES matches(match_id),
            FOREIGN KEY(canonical_team_id) REFERENCES afl_teams(afl_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_match_rosters_match ON cfs_match_rosters(match_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_match_rosters_provider ON cfs_match_rosters(match_provider_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_match_rosters_round ON cfs_match_rosters(round_provider_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfs_match_roster_selections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            match_provider_id TEXT NOT NULL,
            team_provider_id TEXT NOT NULL,
            canonical_team_id INTEGER,
            side TEXT NOT NULL CHECK(side IN ('home','away')),
            player_provider_id TEXT NOT NULL,
            canonical_player_id INTEGER,
            position TEXT,
            jumper_number INTEGER,
            captain INTEGER,
            group_order INTEGER,
            player_order INTEGER,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            collector_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_provider_id, team_provider_id, player_provider_id),
            FOREIGN KEY(match_id) REFERENCES matches(match_id),
            FOREIGN KEY(canonical_team_id) REFERENCES afl_teams(afl_id),
            FOREIGN KEY(canonical_player_id) REFERENCES canonical_players(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_match_roster_selections_match ON cfs_match_roster_selections(match_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_match_roster_selections_provider ON cfs_match_roster_selections(match_provider_id, team_provider_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_match_roster_selections_player ON cfs_match_roster_selections(match_id, canonical_player_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfs_match_roster_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            match_provider_id TEXT NOT NULL,
            team_provider_id TEXT NOT NULL,
            canonical_team_id INTEGER,
            side TEXT NOT NULL CHECK(side IN ('home','away')),
            context_type TEXT NOT NULL CHECK(context_type IN
                ('ins','outs','lateChanges','clubDebuts','milestones')),
            player_provider_id TEXT NOT NULL,
            canonical_player_id INTEGER,
            reason TEXT,
            player_order INTEGER,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            collector_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_provider_id, team_provider_id, context_type, player_provider_id),
            FOREIGN KEY(match_id) REFERENCES matches(match_id),
            FOREIGN KEY(canonical_team_id) REFERENCES afl_teams(afl_id),
            FOREIGN KEY(canonical_player_id) REFERENCES canonical_players(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_match_roster_context_match ON cfs_match_roster_context(match_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_match_roster_context_provider ON cfs_match_roster_context(match_provider_id, team_provider_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cfs_match_roster_context_type ON cfs_match_roster_context(match_id, context_type)")
