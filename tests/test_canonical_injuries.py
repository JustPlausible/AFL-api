from __future__ import annotations

import sqlite3

import pytest

from db.import_to_db import save_injuries_to_db
from db.migration_runner import migrate_database
from merge.helpers import resolve_canonical_injury_player


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "injuries.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO afl_competitions VALUES (1,'CD_C014','AFL','AFL','{}','{}','now')")
    conn.execute("INSERT INTO afl_seasons(afl_id,provider_id,competition_id,year,is_current,updated_at) VALUES (86,'CD_S2026014',1,2026,1,'now')")
    conn.executemany(
        "INSERT INTO afl_teams(afl_id,provider_id,season_id,name,abbreviation,updated_at) VALUES (?,?,?,?,?,'now')",
        [(10, "CD_T10", 86, "Adelaide Crows", "ADE"),
         (20, "CD_T20", 86, "Other Club", "OTH")],
    )
    conn.executemany("INSERT INTO afl_team_seasons VALUES (86,?,'now','now')", [(10,), (20,)])
    conn.commit()
    yield conn
    conn.close()


def add_player(conn, name, afl_id, *, team_id=10, provider=True):
    given, family = name.split(" ", 1)
    cursor = conn.execute(
        "INSERT INTO canonical_players(display_name,given_name,family_name,created_at,updated_at) VALUES (?,?,?,?,?)",
        (name, given, family, "now", "now"),
    )
    player_id = cursor.lastrowid
    if provider:
        conn.execute(
            "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) VALUES (?,?,?,?,?)",
            (player_id, "afl", str(afl_id), "now", "now"),
        )
    conn.execute(
        "INSERT INTO competition_season_players(player_id,competition_season_id,team_id,source_provider,source_json,created_at,updated_at) VALUES (?,86,?,'champion_data','{}','now','now')",
        (player_id, team_id),
    )
    conn.commit()
    return player_id


@pytest.mark.parametrize(("source_name", "canonical_name", "afl_id"), [
    ("Hugh Bond", "Hugh Bond", 1),
    ("Darcy Fogarty", "Darcy Fogarty", 2),
    ("Mitch Hinge", "Mitchell Hinge", 3),
    ("Rory Laird", "Rory Laird", 4),
    ("James Peatling", "James Peatling", 5),
    ("Luke Pedlar", "Luke Pedlar", 6),
])
def test_current_adelaide_players_resolve_without_legacy_players(
    database, source_name, canonical_name, afl_id
):
    add_player(database, canonical_name, afl_id)

    resolution = resolve_canonical_injury_player(source_name, "ADE", database)

    assert resolution.status == "resolved"
    assert resolution.afl_id == afl_id
    assert database.execute("SELECT COUNT(*) FROM players").fetchone() == (0,)


def test_suffix_normalisation_and_wrong_club_protection(database):
    add_player(database, "Rory Laird", 4)
    add_player(database, "Rory Laird", 99, team_id=20)

    assert resolve_canonical_injury_player("Rory Laird Jnr.", "ADE", database).afl_id == 4
    wrong = resolve_canonical_injury_player("Rory Laird", "XXX", database)
    assert wrong.status == "unresolved" and wrong.afl_id is None


def test_ambiguous_match_is_never_assigned(database):
    add_player(database, "Alex Example", 10)
    add_player(database, "Alex Example", 11)

    resolution = resolve_canonical_injury_player("Alex Example", "ADE", database)

    assert resolution.status == "ambiguous"
    assert resolution.afl_id is None


def test_missing_afl_provider_id_is_not_fabricated(database):
    add_player(database, "No Id", 0, provider=False)

    resolution = resolve_canonical_injury_player("No Id", "ADE", database)

    assert resolution.status == "unresolved"
    assert resolution.afl_id is None
    assert "no AFL provider identifier" in resolution.reason


def injury(name, afl_id, status="resolved", reason=None):
    return {"name": name, "afl_id": afl_id, "injury": "Hamstring", "return": "2 weeks",
            "resolution_status": status, "resolution_reason": reason}


def test_persistence_continues_and_reports_partial_counts(database):
    data = {"source": "offline-fixture", "scraped_at": "2026-07-31T00:00:00+00:00",
            "teams": [{"club": "ADE", "updated": "31 July 2026", "players": [
                injury("Hugh Bond", 1),
                injury("Unknown Player", None, "unresolved", "no canonical match"),
                injury("Alex Example", None, "ambiguous", "two canonical matches"),
            ]}]}

    summary = save_injuries_to_db(data, database)

    assert {key: summary[key] for key in (
        "rows_parsed", "rows_resolved", "rows_persisted", "rows_unresolved", "rows_ambiguous"
    )} == {"rows_parsed": 3, "rows_resolved": 1, "rows_persisted": 1,
           "rows_unresolved": 1, "rows_ambiguous": 1}
    assert summary["status"] == "partial"
    assert database.execute("SELECT afl_id,player_name FROM injuries").fetchall() == [(1, "Hugh Bond")]
    assert len(summary["diagnostics"]) == 2
    assert all(item["player_name"] and item["club"] and item["reason"]
               for item in summary["diagnostics"])
