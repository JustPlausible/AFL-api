import sqlite3

import pytest

from cli_runtime import _resolve_statspro_season


def _database(competition_rows, season_rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE afl_competitions(
            afl_id INTEGER PRIMARY KEY, provider_id TEXT, code TEXT
        );
        CREATE TABLE afl_seasons(
            afl_id INTEGER PRIMARY KEY, provider_id TEXT,
            competition_id INTEGER, year INTEGER
        );
    """)
    conn.executemany("INSERT INTO afl_competitions VALUES(?,?,?)", competition_rows)
    conn.executemany("INSERT INTO afl_seasons VALUES(?,?,?,?)", season_rows)
    return conn


@pytest.mark.parametrize("reverse", [False, True])
def test_statspro_season_uses_configured_afl_competition_regardless_of_insertion_order(reverse):
    seasons = [(85, "CD_S_AFL", 1, 2025), (86, "CD_S_AFLW", 2, 2025)]
    conn = _database(
        [(1, "CD_C014", "AFL"), (2, "CD_C999", "AFLW")],
        list(reversed(seasons)) if reverse else seasons,
    )
    selected = _resolve_statspro_season(
        conn, year=2025, competition_code="AFL", competition_provider_id="CD_C014"
    )
    assert (selected["afl_id"], selected["provider_id"]) == (85, "CD_S_AFL")


@pytest.mark.parametrize("competition_rows,season_rows,error", [
    ([], [], "configured AFL competition could not be resolved uniquely"),
    ([(1, "CD_C014", "AFL"), (2, "CD_C014", "AFL")], [],
     "configured AFL competition could not be resolved uniquely"),
    ([(1, "CD_C014", "AFL")],
     [(85, "CD_S1", 1, 2025), (86, "CD_S2", 1, 2025)],
     "AFL season 2025 could not be resolved uniquely"),
])
def test_statspro_season_fails_clearly_when_configuration_is_missing_or_ambiguous(
        competition_rows, season_rows, error):
    conn = _database(competition_rows, season_rows)
    with pytest.raises(ValueError, match=error):
        _resolve_statspro_season(
            conn, year=2025, competition_code="AFL", competition_provider_id="CD_C014"
        )
