from __future__ import annotations

import sqlite3

import pytest

from afl_json import (
    CanonicalPlayerStat, PlayerCollectionResult, PlayerIdentityConflict,
    PlayerStatsCollectionResult, PlayerStatsStatus, persist_player_seasons,
    upsert_player_stats,
)
from db.migration_runner import migrate_database


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "players.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO afl_competitions VALUES (1,'CD_C014','AFL','AFL','{}','{}','now')")
    for season_id, provider, year in ((85, "CD_S1", 2025), (86, "CD_S2", 2026)):
        conn.execute(
            "INSERT INTO afl_seasons(afl_id,provider_id,competition_id,year,updated_at) VALUES (?,?,?,?,?)",
            (season_id, provider, 1, year, "now"),
        )
    conn.execute(
        "INSERT INTO afl_teams(afl_id,provider_id,season_id,name,updated_at) VALUES (10,'CD_T1',86,'Cats','now')"
    )
    conn.executemany(
        "INSERT INTO afl_team_seasons VALUES (?,?,?,?)", ((85, 10, "now", "now"), (86, 10, "now", "now")),
    )
    conn.commit()
    yield conn
    conn.close()


def result(season, *, afl_id=7001, team="CD_T1", name="Ada Example"):
    identity = {"champion_data_player_id": "CD_I100", "afl_player_id": afl_id,
                "name": name, "given_name": "Ada", "family_name": "Example"}
    association = {"champion_data_player_id": "CD_I100", "provider_season_id": season,
                   "team_id": team, "jumper_number": 7, "listed_position": "MID",
                   "photo_url": None, "source": {"playerId": "CD_I100"}}
    return PlayerCollectionResult([identity], [association], [], "published")


def test_canonical_identity_separate_provider_ids_and_idempotent_seasons(database):
    first = persist_player_seasons(database, result("CD_S1"), provider_season_id="CD_S1")
    repeat = persist_player_seasons(database, result("CD_S1"), provider_season_id="CD_S1")
    second = persist_player_seasons(database, result("CD_S2"), provider_season_id="CD_S2")

    assert (first.players_inserted, first.mappings_inserted, first.associations_inserted) == (1, 2, 1)
    assert repeat.rows_written == 0 and repeat.unchanged == 1
    assert second.players_inserted == 0 and second.associations_inserted == 1
    assert database.execute(
        "SELECT provider,provider_player_id FROM player_provider_ids ORDER BY provider"
    ).fetchall() == [("afl", "7001"), ("champion_data", "CD_I100")]
    assert database.execute(
        "SELECT competition_season_id,team_id FROM competition_season_players ORDER BY competition_season_id"
    ).fetchall() == [(85, 10), (86, 10)]


def test_optional_or_unknown_team_is_non_destructive_diagnostic(database):
    missing = persist_player_seasons(database, result("CD_S1", team=None), provider_season_id="CD_S1")
    assert missing.missing_team_links == 0
    assert database.execute("SELECT team_id FROM competition_season_players").fetchone() == (None,)

    changed = persist_player_seasons(database, result("CD_S1", team="CD_UNKNOWN"), provider_season_id="CD_S1")
    assert changed.missing_team_links == 1
    assert database.execute("SELECT team_id FROM competition_season_players").fetchone() == (None,)


def test_conflicting_crosswalk_rolls_back_without_reassignment(database):
    persist_player_seasons(database, result("CD_S1"), provider_season_id="CD_S1")
    other = PlayerCollectionResult(
        [{"champion_data_player_id": "CD_I200", "afl_player_id": 7002, "name": "Bea Other",
          "given_name": "Bea", "family_name": "Other"}],
        [{"champion_data_player_id": "CD_I200", "provider_season_id": "CD_S1",
          "team_id": None, "jumper_number": None, "listed_position": None,
          "photo_url": None, "source": {}}], [], "published",
    )
    persist_player_seasons(database, other, provider_season_id="CD_S1")
    contradictory = result("CD_S2", afl_id=7002)

    with pytest.raises(PlayerIdentityConflict, match="conflicting crosswalk"):
        persist_player_seasons(database, contradictory, provider_season_id="CD_S2")
    assert database.execute("SELECT COUNT(*) FROM competition_season_players").fetchone() == (2,)
    assert database.execute(
        "SELECT player_id FROM player_provider_ids WHERE provider_player_id='CD_I100'"
    ).fetchone() == (1,)


def test_empty_and_unavailable_never_delete_existing_membership(database):
    persist_player_seasons(database, result("CD_S1"), provider_season_id="CD_S1")
    empty = persist_player_seasons(
        database, PlayerCollectionResult([], [], [], "empty"), provider_season_id="CD_S1"
    )
    unavailable = persist_player_seasons(
        database, PlayerCollectionResult([], [], [], "unavailable"), provider_season_id="CD_S1"
    )
    assert empty.status == "empty" and unavailable.status == "unavailable"
    assert database.execute("SELECT COUNT(*) FROM competition_season_players").fetchone() == (1,)


def test_partial_collection_persists_usable_records_without_replacement(database):
    partial = result("CD_S1")
    partial = PlayerCollectionResult(
        partial.players, partial.player_seasons, partial.diagnostics, "partial"
    )
    summary = persist_player_seasons(database, partial, provider_season_id="CD_S1")
    assert summary.status == "partial" and summary.associations_inserted == 1
    assert database.execute("SELECT COUNT(*) FROM competition_season_players").fetchone() == (1,)


def test_persistence_failure_rolls_back_entire_player_batch(database):
    database.execute("""
        CREATE TRIGGER fail_season_player BEFORE INSERT ON competition_season_players
        BEGIN SELECT RAISE(ABORT, 'simulated failure'); END
    """)
    database.commit()
    with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
        persist_player_seasons(database, result("CD_S1"), provider_season_id="CD_S1")
    assert database.execute("SELECT COUNT(*) FROM canonical_players").fetchone() == (0,)
    assert database.execute("SELECT COUNT(*) FROM player_provider_ids").fetchone() == (0,)


def test_cfs_stats_link_to_canonical_player_without_changing_legacy_contract(database):
    persist_player_seasons(database, result("CD_S1"), provider_season_id="CD_S1")
    record = CanonicalPlayerStat(
        "CD_M1", "CD_I100", "home", "2026-01-01T00:00:00+00:00", "/stats",
        "CONCLUDED", "CONCLUDED", 1, "CD_T1", 1, 0, 2, 3, 5, 1, 4, 0, {}, {},
    )
    stats = PlayerStatsCollectionResult("CD_M1", PlayerStatsStatus.CONCLUDED, [record], [],
                                        "2026-01-01T00:00:00+00:00",
                                        endpoint_source_status="CONCLUDED",
                                        resolved_match_status="CONCLUDED")
    assert upsert_player_stats(database, stats) == 1
    assert database.execute(
        "SELECT canonical_player_id,champion_data_player_id FROM cfs_player_stats"
    ).fetchone() == (1, "CD_I100")
