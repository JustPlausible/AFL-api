import json
import importlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from afl_json.client import AflJsonInvalidResponse
from afl_json.statspro import (LEAGUE_ROUND_TOTAL, SEASON_TOTAL,
                               normalise_statspro, persist_season)
statspro_player_summaries = importlib.import_module("db.migrations.0027_statspro_player_summaries")


FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"


def payload(name):
    return json.loads((FIXTURES / name).read_text())


def database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE canonical_players(id INTEGER PRIMARY KEY);
      CREATE TABLE player_provider_ids(player_id INTEGER, provider TEXT, provider_player_id TEXT);
      CREATE TABLE afl_seasons(afl_id INTEGER PRIMARY KEY);
      CREATE TABLE afl_teams(afl_id INTEGER PRIMARY KEY, provider_id TEXT, name TEXT);
      CREATE TABLE rounds(round_id INTEGER PRIMARY KEY);
      INSERT INTO canonical_players VALUES(7); INSERT INTO canonical_players VALUES(8);
      INSERT INTO player_provider_ids VALUES(7,'champion_data','CD_I100');
      INSERT INTO player_provider_ids VALUES(8,'champion_data','CD_I101');
      INSERT INTO afl_seasons VALUES(85); INSERT INTO afl_teams VALUES(10,'CD_T10','Brisbane Lions');
    """)
    statspro_player_summaries.migrate(conn)
    return conn


def test_season_preserves_finals_zero_null_and_published_averages():
    records = normalise_statspro(payload("statspro_season_total_2025.json"), context=SEASON_TOTAL)
    assert records[0].games_played == 27
    assert records[0].totals["kickins"] is None
    assert records[0].averages["kicks"] == 11.85
    assert records[1].games_played == 0 and records[1].totals["kicks"] == 0


def test_round_total_parses_corresponding_match_fields_without_derivation():
    record = normalise_statspro(payload("statspro_round_07_2026.json"), context=LEAGUE_ROUND_TOTAL)[0]
    assert record.totals == {"kicks": 14, "handballs": 9, "disposals": 23}
    assert record.opponent_provider_id == "CD_T20"


def test_persistence_is_idempotent_updates_corrections_and_retains_unresolved():
    conn = database()
    records = normalise_statspro(payload("statspro_season_total_2025.json"), context=SEASON_TOTAL)
    first = persist_season(conn, records, season_id=85, season_provider_id="CD_S2025014", collected_at="one")
    second = persist_season(conn, records, season_id=85, season_provider_id="CD_S2025014", collected_at="two")
    assert (first.inserted, second.unchanged, first.players_unresolved) == (3, 3, 1)
    assert conn.execute("SELECT canonical_player_id FROM statspro_player_season_summaries WHERE player_provider_id='CD_I_UNKNOWN'").fetchone()[0] is None
    corrected = list(records)
    corrected[0] = replace(records[0], totals={**records[0].totals, "kicks": 321})
    report = persist_season(conn, corrected, season_id=85, season_provider_id="CD_S2025014", collected_at="three")
    assert report.updated == 1 and conn.execute("SELECT count(*) FROM statspro_player_season_summaries").fetchone()[0] == 3


def test_malformed_refresh_cannot_erase_previous_snapshot():
    conn = database()
    records = normalise_statspro(payload("statspro_season_total_2025.json"), context=SEASON_TOTAL)
    persist_season(conn, records, season_id=85, season_provider_id="CD_S2025014", collected_at="one")
    with pytest.raises(AflJsonInvalidResponse):
        normalise_statspro({"players": []}, context=SEASON_TOTAL)
    assert conn.execute("SELECT count(*) FROM statspro_player_season_summaries").fetchone()[0] == 3


@pytest.mark.parametrize("missing_field", ["gamesPlayed", "totals", "averages"])
def test_season_rejects_missing_authoritative_fields(missing_field):
    source = payload("statspro_season_total_2025.json")
    del source["players"][0][missing_field]
    with pytest.raises(AflJsonInvalidResponse, match=f"missing required {missing_field}"):
        normalise_statspro(source, context=SEASON_TOTAL)


def test_incomplete_refresh_retains_previously_persisted_summary():
    conn = database()
    source = payload("statspro_season_total_2025.json")
    records = normalise_statspro(source, context=SEASON_TOTAL)
    persist_season(conn, records, season_id=85, season_provider_id="CD_S2025014", collected_at="good")
    before = tuple(conn.execute(
        "SELECT games_played,published_totals,published_averages,collected_at "
        "FROM statspro_player_season_summaries WHERE player_provider_id='CD_I100'"
    ).fetchone())
    del source["players"][0]["totals"]
    with pytest.raises(AflJsonInvalidResponse):
        normalise_statspro(source, context=SEASON_TOTAL)
    after = tuple(conn.execute(
        "SELECT games_played,published_totals,published_averages,collected_at "
        "FROM statspro_player_season_summaries WHERE player_provider_id='CD_I100'"
    ).fetchone())
    assert after == before


def test_zero_game_season_player_parses_and_persists():
    conn = database()
    records = normalise_statspro(payload("statspro_season_total_2025.json"), context=SEASON_TOTAL)
    zero_game = [record for record in records if record.games_played == 0]
    report = persist_season(conn, zero_game, season_id=85,
                            season_provider_id="CD_S2025014", collected_at="zero")
    assert report.inserted == report.zero_game_players == 1
    assert conn.execute(
        "SELECT games_played FROM statspro_player_season_summaries "
        "WHERE player_provider_id=?", (zero_game[0].player_provider_id,)
    ).fetchone()[0] == 0


def test_player_resolution_is_scoped_to_champion_data_provider():
    conn = database()
    conn.execute("INSERT INTO canonical_players VALUES(9)")
    conn.execute("INSERT INTO player_provider_ids VALUES(9,'other_provider','CD_I100')")
    records = normalise_statspro(payload("statspro_season_total_2025.json"), context=SEASON_TOTAL)
    persist_season(conn, records, season_id=85, season_provider_id="CD_S2025014", collected_at="one")
    assert conn.execute(
        "SELECT canonical_player_id FROM statspro_player_season_summaries "
        "WHERE player_provider_id='CD_I100'"
    ).fetchone()[0] == 7
