"""Migration 0022: canonical player/team identity on injury rows (Issue #213)."""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
from pathlib import Path

from db.migration_runner import MIGRATIONS_DIR, migrate_database

MIGRATION_PATH = MIGRATIONS_DIR / "0022_injury_canonical_identity.py"


def _migrate_up_to_0021(tmp_path: Path, db_path: Path) -> None:
    """Build a database at the schema state immediately before 0022."""
    migrations_copy = tmp_path / "migrations_0021"
    migrations_copy.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("0022_"):
            continue
        shutil.copy(path, migrations_copy / path.name)
    migrate_database(db_path, migrations_copy)


def _run_0022(conn: sqlite3.Connection) -> None:
    spec = importlib.util.spec_from_file_location("injury_canonical_identity_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.migrate(conn)


def test_fresh_database_has_canonical_identity_columns(tmp_path):
    db = tmp_path / "fresh.db"
    migrate_database(db)
    conn = sqlite3.connect(db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(injuries)")}
    assert {"canonical_player_id", "canonical_team_id"} <= columns
    conn.close()


def _seed_minimal_afl_hierarchy(conn, team_ids: list[int]) -> None:
    """Insert just enough afl_competitions/afl_seasons/afl_teams rows for the
    migration's canonical_team_id foreign key to be satisfiable under
    PRAGMA foreign_keys=ON, mirroring the shape already used by
    tests/test_canonical_injuries.py."""
    conn.execute("INSERT INTO afl_competitions VALUES (1,'CD_C014','AFL','AFL','{}','{}','now')")
    conn.execute(
        "INSERT INTO afl_seasons(afl_id,provider_id,competition_id,year,is_current,updated_at) "
        "VALUES (85,'CD_S2026014',1,2026,NULL,'now')"
    )
    conn.executemany(
        "INSERT INTO afl_teams(afl_id,provider_id,season_id,name,abbreviation,updated_at) "
        "VALUES (?,?,85,?,?,'now')",
        [(team_id, f"CD_T{team_id}", f"Team {team_id}", f"T{team_id}") for team_id in team_ids],
    )


def _insert_legacy_injury(conn, afl_id, club, name):
    conn.execute(
        "INSERT INTO injuries (afl_id, club, player_name, injury, return_info, updated, "
        "first_updated, source, scraped_at, current) VALUES (?, ?, ?, 'Knee', '1 week', "
        "'Today', 'Today', 'legacy', 'now', 1)",
        (afl_id, club, name),
    )


def test_backfill_is_deterministic_and_independent_per_column(tmp_path):
    """Backfill only where safe: a unique AFL provider crosswalk resolves a player,
    a known club code resolves a team, and each is independent of the other --
    an unresolved side stays explicitly null rather than guessed."""
    db = tmp_path / "legacy.db"
    _migrate_up_to_0021(tmp_path, db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    _seed_minimal_afl_hierarchy(conn, [1, 5])

    conn.execute(
        "INSERT INTO canonical_players(id, display_name, created_at, updated_at) "
        "VALUES (1, 'Matched Player', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO player_provider_ids(player_id, provider, provider_player_id, created_at, updated_at) "
        "VALUES (1, 'afl', '111', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO canonical_players(id, display_name, created_at, updated_at) "
        "VALUES (4, 'Club Unresolved Player', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO player_provider_ids(player_id, provider, provider_player_id, created_at, updated_at) "
        "VALUES (4, 'afl', '333', 'now', 'now')"
    )

    # 111: club resolves (ADE) and the player crosswalk is unique -> both backfilled.
    _insert_legacy_injury(conn, 111, "ADE", "Matched Player")
    # 222: club resolves (CAR) but no player crosswalk exists -> only team backfilled.
    _insert_legacy_injury(conn, 222, "CAR", "No Crosswalk Player")
    # 333: club code is not canonical, but the player crosswalk is unique -> only player backfilled.
    _insert_legacy_injury(conn, 333, "ZZZ", "Club Unresolved Player")
    conn.commit()

    _run_0022(conn)
    conn.commit()

    rows = {
        row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT afl_id, canonical_player_id, canonical_team_id FROM injuries"
        ).fetchall()
    }
    assert rows[111] == (1, 1)
    assert rows[222] == (None, 5)
    assert rows[333] == (4, None)
    conn.close()


def test_backfill_leaves_afl_provider_crosswalk_intact_when_only_club_resolves(tmp_path):
    """The player-provider crosswalk table's own UNIQUE(provider, provider_player_id)
    constraint means a given afl_id can never map to more than one canonical player --
    so an afl_id with no crosswalk row at all (the only realistic "not deterministic"
    case) is left null rather than guessed, independent of a successful club backfill."""
    db = tmp_path / "legacy.db"
    _migrate_up_to_0021(tmp_path, db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    _seed_minimal_afl_hierarchy(conn, [12])
    _insert_legacy_injury(conn, 555, "ESS", "Unknown Crosswalk Player")
    conn.commit()

    _run_0022(conn)
    conn.commit()

    row = conn.execute(
        "SELECT canonical_player_id, canonical_team_id FROM injuries WHERE afl_id=555"
    ).fetchone()
    assert row == (None, 12)
    conn.close()


def test_backfill_does_not_fail_when_afl_teams_has_no_matching_row(tmp_path):
    """A legacy database can have injury rows before afl_teams is bootstrapped.
    canonical_team_id has a REFERENCES afl_teams(afl_id) foreign key and the
    migration runner enables PRAGMA foreign_keys=ON, so backfilling a seeded
    team id that afl_teams doesn't have yet must be skipped (left null)
    rather than raising a FOREIGN KEY constraint failure (PR #214 review
    finding)."""
    db = tmp_path / "legacy.db"
    _migrate_up_to_0021(tmp_path, db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    # Deliberately no afl_teams rows at all.
    _insert_legacy_injury(conn, 777, "ADE", "No Team Row Yet")
    conn.commit()

    _run_0022(conn)  # must not raise sqlite3.IntegrityError
    conn.commit()

    row = conn.execute(
        "SELECT canonical_player_id, canonical_team_id FROM injuries WHERE afl_id=777"
    ).fetchone()
    assert row == (None, None)
    conn.close()
