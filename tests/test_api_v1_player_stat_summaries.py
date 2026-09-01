"""Public contracts for derived Home & Away player summaries."""
import sqlite3

from tests.test_api_v1_player_stats import (API_KEY, HOME_TEAM_ID, NOW, SEASON_ID,
    _client, _grant_advanced, _make_db, _seed_player)


PLAYER_ID = 501


def _seed(conn):
    _seed_player(conn, PLAYER_ID, champion_data_id="CD_I501")
    _seed_player(conn, 502, champion_data_id="CD_I502")
    for finalized in (0, 1):
        conn.execute(
            "INSERT INTO derived_player_season_summaries(season_id,canonical_player_id,team_id,"
            "scope,source,population_source,games_played,totals,derived_rates,built_at,"
            "source_max_updated_at,finalized) VALUES(?,?,?,'home_and_away',"
            "'DERIVED_MATCH_STATS','competition_season_players',2,'{\"kicks\":7}',"
            "'{\"goal_accuracy\":50.0}',?,?,?)",
            (SEASON_ID, PLAYER_ID if finalized else 502, HOME_TEAM_ID, NOW, NOW, finalized),
        )


def test_ordinary_h_and_a_response_hides_all_provider_ids(tmp_path, monkeypatch):
    client = _client(_make_db(tmp_path, _seed), monkeypatch)
    response = client.get(
        f"/api/v1/seasons/{SEASON_ID}/player-stat-summaries?scope=home_and_away",
        headers={"x-api-key": API_KEY},
    )
    assert response.status_code == 200
    assert len(response.json()["summaries"]) == 1  # draft player 502 is excluded
    summary = response.json()["summaries"][0]
    assert summary["player"]["champion_data_id"] is None
    assert summary["season"]["provider_id"] is None
    assert summary["team"]["provider_id"] is None


def test_advanced_h_and_a_response_includes_player_season_team_provider_ids(tmp_path, monkeypatch):
    path = _make_db(tmp_path, _seed); _grant_advanced(path)
    client = _client(path, monkeypatch)
    response = client.get(
        f"/api/v1/players/{PLAYER_ID}/seasons/{SEASON_ID}/player-stat-summary"
        "?scope=home_and_away&advanced=true", headers={"x-api-key": API_KEY},
    )
    assert response.status_code == 200
    summary = response.json()
    assert summary["player"]["champion_data_id"] == "CD_I501"
    assert summary["season"]["provider_id"] == "CD_S85"
    assert summary["team"]["provider_id"] == "CD_T1"


def test_preview_is_invisible_to_individual_endpoint(tmp_path, monkeypatch):
    client = _client(_make_db(tmp_path, _seed), monkeypatch)
    response = client.get(
        f"/api/v1/players/502/seasons/{SEASON_ID}/player-stat-summary?scope=home_and_away",
        headers={"x-api-key": API_KEY},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "player_stat_summary_not_found"
