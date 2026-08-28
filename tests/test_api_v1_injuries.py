"""Offline API tests for GET /api/v1/injuries (Issue #213).

Fixture shape mirrors tests/test_api_v1_players.py: raw inserts against a
fully migrated SQLite database, hashed API-key fixtures against the real
``authenticate_api_key`` dependency. No test contacts AFL/CFS or a browser;
injury rows are inserted directly, exercising only the read contract.
"""

import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from api.routes_v1 import router as v1_router
from api_key_security import api_key_prefix, hash_api_key
from db.init_db import create_api_keys_table
from db.migration_runner import migrate_database

NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc).isoformat()
API_KEY = "v1-injuries-test-key"

SEASON_ID = 85
COLLINGWOOD_TEAM_ID = 3
ESSENDON_TEAM_ID = 12
DAICOS_PLAYER_ID = 584


def _seed_teams(conn):
    conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}',?)", (NOW,))
    conn.execute(
        "INSERT INTO afl_seasons(afl_id,provider_id,competition_id,year,is_current,updated_at) "
        "VALUES(?,'CD_S85',1,2026,1,?)",
        (SEASON_ID, NOW),
    )
    conn.executemany(
        "INSERT INTO afl_teams(afl_id,provider_id,name,abbreviation,updated_at) "
        "VALUES(?,?,?,?,?)",
        [
            (COLLINGWOOD_TEAM_ID, "CD_T3", "Collingwood", "COLL", NOW),
            (ESSENDON_TEAM_ID, "CD_T12", "Essendon", "ESS", NOW),
        ],
    )


def _seed_player(conn, player_id, display_name):
    conn.execute(
        "INSERT INTO canonical_players(id,display_name,given_name,family_name,created_at,updated_at) "
        "VALUES(?,?,NULL,NULL,?,?)",
        (player_id, display_name, NOW, NOW),
    )


def _seed_injury(conn, *, afl_id, canonical_player_id, canonical_team_id, player_name,
                  club="COLL", injury="Knee", return_info="Test", updated="August 18, 2026",
                  current=1, scraped_at=NOW):
    conn.execute(
        "INSERT INTO injuries (afl_id, club, player_name, injury, return_info, updated, "
        "first_updated, source, scraped_at, current, canonical_player_id, canonical_team_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'https://www.afl.com.au/matches/injury-list', ?, ?, ?, ?)",
        (afl_id, club, player_name, injury, return_info, updated, updated, scraped_at,
         current, canonical_player_id, canonical_team_id),
    )


def _seed_api_key(conn):
    create_api_keys_table(conn.cursor())
    conn.execute(
        "INSERT INTO api_keys (label, api_key, key_hash, key_prefix, is_active) VALUES (?, NULL, ?, ?, 1)",
        ("v1-injuries-tests", hash_api_key(API_KEY), api_key_prefix(API_KEY)),
    )


def _make_db(tmp_path, seed):
    path = tmp_path / "injuries.db"
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
    app.include_router(v1_router)
    return TestClient(app)


def _get(client, params=None, headers=None):
    headers = headers or {"x-api-key": API_KEY}
    return client.get("/api/v1/injuries", params=params, headers=headers)


def test_returns_current_injury_using_canonical_identity(tmp_path, monkeypatch):
    def seed(conn):
        _seed_teams(conn)
        _seed_player(conn, DAICOS_PLAYER_ID, "Nick Daicos")
        _seed_injury(
            conn, afl_id=5501, canonical_player_id=DAICOS_PLAYER_ID,
            canonical_team_id=COLLINGWOOD_TEAM_ID, player_name="Nick Daicos",
        )

    client = _client(_make_db(tmp_path, seed), monkeypatch)
    response = _get(client)

    assert response.status_code == 200
    assert response.json() == {
        "injuries": [{
            "canonical_player_id": DAICOS_PLAYER_ID,
            "player": {"display_name": "Nick Daicos"},
            "team": {"team_id": COLLINGWOOD_TEAM_ID, "name": "Collingwood"},
            "injury": "Knee",
            "estimated_return": "Test",
            "source_updated": "August 18, 2026",
            "observed_at": NOW,
            "current": True,
        }]
    }


def test_filter_by_team_id(tmp_path, monkeypatch):
    def seed(conn):
        _seed_teams(conn)
        _seed_player(conn, 1, "Collingwood Player")
        _seed_player(conn, 2, "Essendon Player")
        _seed_injury(conn, afl_id=1, canonical_player_id=1, canonical_team_id=COLLINGWOOD_TEAM_ID,
                     player_name="Collingwood Player", club="COLL")
        _seed_injury(conn, afl_id=2, canonical_player_id=2, canonical_team_id=ESSENDON_TEAM_ID,
                     player_name="Essendon Player", club="ESS")

    client = _client(_make_db(tmp_path, seed), monkeypatch)
    response = _get(client, params={"team_id": ESSENDON_TEAM_ID})

    assert response.status_code == 200
    ids = [row["canonical_player_id"] for row in response.json()["injuries"]]
    assert ids == [2]


def test_filter_by_canonical_player_id(tmp_path, monkeypatch):
    def seed(conn):
        _seed_teams(conn)
        _seed_player(conn, 1, "Player One")
        _seed_player(conn, 2, "Player Two")
        _seed_injury(conn, afl_id=1, canonical_player_id=1, canonical_team_id=COLLINGWOOD_TEAM_ID,
                     player_name="Player One")
        _seed_injury(conn, afl_id=2, canonical_player_id=2, canonical_team_id=COLLINGWOOD_TEAM_ID,
                     player_name="Player Two")

    client = _client(_make_db(tmp_path, seed), monkeypatch)
    response = _get(client, params={"canonical_player_id": 2})

    assert response.status_code == 200
    ids = [row["canonical_player_id"] for row in response.json()["injuries"]]
    assert ids == [2]


def test_combined_filters_naming_different_players_return_empty(tmp_path, monkeypatch):
    def seed(conn):
        _seed_teams(conn)
        _seed_player(conn, 1, "Collingwood Player")
        _seed_injury(conn, afl_id=1, canonical_player_id=1, canonical_team_id=COLLINGWOOD_TEAM_ID,
                     player_name="Collingwood Player")

    client = _client(_make_db(tmp_path, seed), monkeypatch)
    response = _get(client, params={"team_id": ESSENDON_TEAM_ID, "canonical_player_id": 1})

    assert response.status_code == 200
    assert response.json() == {"injuries": []}


def test_rows_without_resolved_canonical_player_id_are_excluded(tmp_path, monkeypatch):
    """A legacy/unresolved row has no canonical identity to expose safely, so it
    is omitted from this canonical-first resource rather than surfaced under an
    invented identity."""
    def seed(conn):
        _seed_teams(conn)
        conn.execute(
            "INSERT INTO injuries (afl_id, club, player_name, injury, return_info, updated, "
            "first_updated, source, scraped_at, current, canonical_player_id, canonical_team_id) "
            "VALUES (99, 'COLL', 'Unresolved Legacy Player', 'Knee', 'Test', 'Today', 'Today', "
            "'legacy', ?, 1, NULL, NULL)",
            (NOW,),
        )

    client = _client(_make_db(tmp_path, seed), monkeypatch)
    response = _get(client)

    assert response.status_code == 200
    assert response.json() == {"injuries": []}


def test_non_current_rows_are_excluded(tmp_path, monkeypatch):
    def seed(conn):
        _seed_teams(conn)
        _seed_player(conn, 1, "No Longer Injured")
        _seed_injury(conn, afl_id=1, canonical_player_id=1, canonical_team_id=COLLINGWOOD_TEAM_ID,
                     player_name="No Longer Injured", current=0)

    client = _client(_make_db(tmp_path, seed), monkeypatch)
    response = _get(client)

    assert response.status_code == 200
    assert response.json() == {"injuries": []}


def test_row_with_unresolved_team_reports_null_team(tmp_path, monkeypatch):
    def seed(conn):
        _seed_teams(conn)
        _seed_player(conn, 1, "Club Unresolved Player")
        _seed_injury(conn, afl_id=1, canonical_player_id=1, canonical_team_id=None,
                     player_name="Club Unresolved Player")

    client = _client(_make_db(tmp_path, seed), monkeypatch)
    response = _get(client)

    assert response.status_code == 200
    assert response.json()["injuries"][0]["team"] is None


def test_ordering_is_deterministic_by_team_then_player(tmp_path, monkeypatch):
    def seed(conn):
        _seed_teams(conn)
        _seed_player(conn, 2, "Essendon Player")
        _seed_player(conn, 3, "Collingwood Player B")
        _seed_player(conn, 1, "Collingwood Player A")
        _seed_injury(conn, afl_id=2, canonical_player_id=2, canonical_team_id=ESSENDON_TEAM_ID,
                     player_name="Essendon Player", club="ESS")
        _seed_injury(conn, afl_id=3, canonical_player_id=3, canonical_team_id=COLLINGWOOD_TEAM_ID,
                     player_name="Collingwood Player B")
        _seed_injury(conn, afl_id=1, canonical_player_id=1, canonical_team_id=COLLINGWOOD_TEAM_ID,
                     player_name="Collingwood Player A")

    client = _client(_make_db(tmp_path, seed), monkeypatch)
    response = _get(client)

    assert response.status_code == 200
    ids = [row["canonical_player_id"] for row in response.json()["injuries"]]
    assert ids == [1, 3, 2]


def test_empty_database_returns_200_with_empty_collection(tmp_path, monkeypatch):
    client = _client(_make_db(tmp_path, seed=_seed_teams), monkeypatch)
    response = _get(client)

    assert response.status_code == 200
    assert response.json() == {"injuries": []}


def test_missing_api_key_returns_401(tmp_path, monkeypatch):
    client = _client(_make_db(tmp_path, seed=_seed_teams), monkeypatch)
    response = client.get("/api/v1/injuries")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


def test_invalid_api_key_returns_401(tmp_path, monkeypatch):
    client = _client(_make_db(tmp_path, seed=_seed_teams), monkeypatch)
    response = _get(client, headers={"x-api-key": "wrong-key"})

    assert response.status_code == 401


def test_out_of_range_team_id_returns_422(tmp_path, monkeypatch):
    client = _client(_make_db(tmp_path, seed=_seed_teams), monkeypatch)
    response = _get(client, params={"team_id": 2**63})

    assert response.status_code == 422


def test_openapi_documents_injuries_route(tmp_path, monkeypatch):
    client = _client(_make_db(tmp_path, seed=_seed_teams), monkeypatch)
    operation = client.get("/openapi.json").json()["paths"]["/api/v1/injuries"]["get"]

    assert operation["summary"] == "List current canonical injuries"
    param_names = {p["name"] for p in operation["parameters"]}
    assert {"team_id", "canonical_player_id"} <= param_names
