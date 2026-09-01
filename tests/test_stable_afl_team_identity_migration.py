"""Upgrade coverage for the Issue #229 canonical-team schema cleanup."""
from __future__ import annotations

import sqlite3
import shutil
from pathlib import Path

from db.migration_runner import migrate_database


def test_existing_database_upgrade_preserves_team_identity_memberships_and_fks(tmp_path):
    path = tmp_path / "existing.db"
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    migrations = Path(__file__).parents[1] / "db" / "migrations"
    for source in migrations.glob("*.py"):
        if source.name == "__init__.py" or source.name[:4] <= "0024":
            shutil.copy(source, old_migrations / source.name)
    migrate_database(path, migrations_dir=old_migrations)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    now = "2026-08-28T00:00:00+00:00"
    conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}',?)", (now,))
    for season, year in ((84, 2024), (85, 2025)):
        conn.execute(
            "INSERT INTO afl_seasons(afl_id,competition_id,year,updated_at) VALUES(?,?,?,?)",
            (season, 1, year, now),
        )
    conn.execute(
        "INSERT INTO afl_teams(afl_id,provider_id,season_id,name,updated_at) VALUES(10,'CD_T10',84,'Club',?)",
        (now,),
    )
    conn.executemany("INSERT INTO afl_team_seasons VALUES(?,10,?,?)", ((84, now, now), (85, now, now)))
    conn.commit()
    conn.close()

    assert migrate_database(path) == ["0025", "0026", "0027", "0028"]
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")

    assert "season_id" not in {row[1] for row in conn.execute("PRAGMA table_info(afl_teams)")}
    assert conn.execute("SELECT afl_id,provider_id,name FROM afl_teams").fetchall() == [(10, "CD_T10", "Club")]
    assert conn.execute("SELECT competition_season_id,team_id FROM afl_team_seasons ORDER BY 1").fetchall() == [(84, 10), (85, 10)]
    team_fks = {row[2] for row in conn.execute("PRAGMA foreign_key_list(afl_team_seasons)")}
    player_fks = {row[2] for row in conn.execute("PRAGMA foreign_key_list(competition_season_players)")}
    assert "afl_teams" in team_fks
    assert "afl_team_seasons" in player_fks
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
