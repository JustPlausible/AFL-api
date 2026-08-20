"""Offline API tests for GET /api/v1/players/{canonical_player_id}.

Fixture shape mirrors the DB-building conventions already used by
``tests/test_api_v1_player_stats.py`` (raw inserts against a fully migrated
SQLite database, hashed API-key fixtures against the real
``authenticate_api_key`` dependency). No test contacts AFL/CFS; everything
runs against an isolated on-disk SQLite fixture.
"""

import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from api.routes import router as legacy_router
from api.routes_v1 import router as v1_router
from api_key_security import api_key_prefix, hash_api_key
from db.init_db import create_api_keys_table
from db.migration_runner import migrate_database

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc).isoformat()
API_KEY = "v1-players-test-key"

CURRENT_SEASON_ID = 85
OTHER_SEASON_ID = 84
TEAM_ID = 10
PLAYER_ID = 584


def _seed_seasons(conn, *, current_season_id=CURRENT_SEASON_ID, other_season_id=OTHER_SEASON_ID):
    conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}',?)", (NOW,))
    conn.execute(
        "INSERT INTO afl_seasons VALUES(?,'CD_S85',1,'2026','2026',2026,1,1,NULL,NULL,'{}','{}',?)",
        (current_season_id, NOW),
    )
    conn.execute(
        "INSERT INTO afl_seasons VALUES(?,'CD_S84',1,'2025','2025',2025,0,1,NULL,NULL,'{}','{}',?)",
        (other_season_id, NOW),
    )
    conn.execute(
        "INSERT INTO afl_teams VALUES(?,'CD_T1',?,'Collingwood','COLL','Magpies','Collingwood',"
        "'Collingwood','MEN','{}','{}','{}',?)",
        (TEAM_ID, current_season_id, NOW),
    )
    conn.execute("INSERT INTO afl_team_seasons VALUES(?,?,?,?)", (current_season_id, TEAM_ID, NOW, NOW))


def _seed_player(conn, player_id=PLAYER_ID, *, display_name=None, given_name=None, family_name=None):
    conn.execute(
        "INSERT INTO canonical_players VALUES(?,?,?,?,?,?)",
        (player_id, display_name, given_name, family_name, NOW, NOW),
    )


def _seed_provider_id(conn, player_id, provider, provider_player_id):
    conn.execute(
        "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) "
        "VALUES(?,?,?,?,?)",
        (player_id, provider, provider_player_id, NOW, NOW),
    )


def _seed_membership(conn, player_id, competition_season_id, team_id):
    conn.execute(
        "INSERT INTO competition_season_players(player_id,competition_season_id,team_id,"
        "source_provider,source_json,created_at,updated_at) VALUES(?,?,?,'champion_data','{}',?,?)",
        (player_id, competition_season_id, team_id, NOW, NOW),
    )


def _seed_api_key(conn):
    create_api_keys_table(conn.cursor())
    conn.execute(
        "INSERT INTO api_keys (label, api_key, key_hash, key_prefix, is_active) VALUES (?, NULL, ?, ?, 1)",
        ("v1-players-tests", hash_api_key(API_KEY), api_key_prefix(API_KEY)),
    )


def _make_db(tmp_path, seed):
    path = tmp_path / "players.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    seed(conn)
    _seed_api_key(conn)
    conn.commit()
    conn.close()
    return path


def _client(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    app = FastAPI()
    app.include_router(legacy_router)
    app.include_router(v1_router)
    return TestClient(app)


def _get(client, canonical_player_id=PLAYER_ID, headers=None):
    headers = headers or {"x-api-key": API_KEY}
    return client.get(f"/api/v1/players/{canonical_player_id}", headers=headers)


def test_successful_lookup_returns_full_identity(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, display_name="Nick Daicos")
        _seed_provider_id(conn, PLAYER_ID, "afl", "5501")
        _seed_provider_id(conn, PLAYER_ID, "champion_data", "CD_I1023261")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert response.json() == {
        "player": {
            "canonical_player_id": PLAYER_ID,
            "display_name": "Nick Daicos",
            "current_team": {"team_id": TEAM_ID, "name": "Collingwood"},
            "identifiers": {
                "afl_player_id": 5501,
                "champion_data_player_id": "CD_I1023261",
            },
        }
    }


def test_unknown_canonical_player_returns_404(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = _get(client, canonical_player_id=999999)

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "player_not_found", "message": "Player not found."}
    }


def test_missing_optional_provider_mappings_are_explicit_null(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, given_name="J", family_name="Smith")
        # No provider IDs, and no current-season membership row at all.

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    body = response.json()["player"]
    assert body["display_name"] == "J Smith"
    assert body["current_team"] is None
    assert body["identifiers"] == {"afl_player_id": None, "champion_data_player_id": None}


def test_one_missing_provider_mapping_is_null_the_other_present(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, display_name="A. Jones")
        _seed_provider_id(conn, PLAYER_ID, "champion_data", "CD_I999")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    identifiers = response.json()["player"]["identifiers"]
    assert identifiers == {"afl_player_id": None, "champion_data_player_id": "CD_I999"}


def test_membership_in_non_current_season_does_not_leak_into_current_team(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, display_name="Old Season Player")
        conn.execute(
            "INSERT INTO afl_team_seasons VALUES(?,?,?,?)", (OTHER_SEASON_ID, TEAM_ID, NOW, NOW)
        )
        _seed_membership(conn, PLAYER_ID, OTHER_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["player"]["current_team"] is None


def test_current_season_membership_with_unresolved_team_is_null(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, display_name="No Team Player")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, None)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["player"]["current_team"] is None


def test_missing_api_key_header_returns_401(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = client.get(f"/api/v1/players/{PLAYER_ID}")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


def test_invalid_api_key_returns_401(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = _get(client, headers={"x-api-key": "wrong-key"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


def test_non_integer_canonical_player_id_returns_422(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = client.get("/api/v1/players/not-a-number", headers={"x-api-key": API_KEY})

    assert response.status_code == 422


def test_response_shape_regression(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, display_name="Nick Daicos")
        _seed_provider_id(conn, PLAYER_ID, "afl", "5501")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"player"}
    assert set(body["player"].keys()) == {
        "canonical_player_id", "display_name", "current_team", "identifiers",
    }
    assert set(body["player"]["current_team"].keys()) == {"team_id", "name"}
    assert set(body["player"]["identifiers"].keys()) == {
        "afl_player_id", "champion_data_player_id",
    }


def test_openapi_documents_player_route_and_404(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    operation = client.get("/openapi.json").json()["paths"]["/api/v1/players/{canonical_player_id}"]["get"]

    assert {"200", "404", "422"} <= set(operation["responses"])
    assert operation["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApplicationErrorResponse"
    }


def test_existing_match_player_stats_and_legacy_players_route_are_unchanged(tmp_path, monkeypatch):
    """Regression guard: this endpoint must not alter the match player-stats
    contract or the legacy /api/players route (both out of scope for #180)."""

    def seed(conn):
        _seed_seasons(conn)
        conn.execute(
            "INSERT INTO rounds(round_id,round_label,season_id,competition_id,provider_id,round_number) "
            "VALUES(101,'Round 1',?,1,'CD_R1',1)",
            (CURRENT_SEASON_ID,),
        )
        conn.execute(
            "INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,"
            "status,start_time_utc,season_id,home_team_id,away_team_id) "
            "VALUES(8216,NULL,101,'A','B','MCG','SCHEDULED','2026-08-02T00:00:00+00:00',?,?,?)",
            (CURRENT_SEASON_ID, TEAM_ID, TEAM_ID),
        )
        conn.execute(
            "INSERT INTO players(afl_id, first_name, last_name, club) VALUES(1,'J','Smith','COLL')"
        )

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    match_stats_response = client.get(
        "/api/v1/matches/8216/player-stats", headers={"x-api-key": API_KEY}
    )
    assert match_stats_response.status_code == 200
    assert match_stats_response.json()["players"] == []
    assert match_stats_response.json()["lifecycle"]["finality"] == "not_available"

    legacy_response = client.get("/api/players/1", headers={"x-api-key": API_KEY})
    assert legacy_response.status_code == 200
    assert legacy_response.json()["afl_id"] == 1
