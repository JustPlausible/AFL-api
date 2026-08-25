"""Offline contract tests for v1 discovery and canonical seasons."""

import sqlite3

from api.routes_v1 import ApiDiscoveryResponse, Season, SeasonsResponse
from tests.test_api_v1_player_stats import API_KEY, NOW, _client, _make_db


def _seed_historical_season(conn):
    conn.execute(
        "INSERT INTO afl_seasons VALUES(84,'CD_S84',1,'2025','2025',2025,0,24,"
        "NULL,NULL,'{\"private\": true}','{\"provider\": true}',?)",
        (NOW,),
    )


def test_discovery_requires_api_key_with_shared_auth_error(tmp_path, monkeypatch):
    client = _client(_make_db(tmp_path, _seed_historical_season), monkeypatch)

    missing = client.get("/api/v1")
    invalid = client.get("/api/v1", headers={"x-api-key": "invalid"})

    assert missing.status_code == invalid.status_code == 401
    assert missing.json() == invalid.json() == {"detail": "Invalid or missing API Key"}


def test_discovery_returns_only_typed_public_fields(tmp_path, monkeypatch):
    client = _client(_make_db(tmp_path, _seed_historical_season), monkeypatch)

    response = client.get("/api/v1", headers={"x-api-key": API_KEY})

    assert response.status_code == 200
    assert response.json() == {
        "name": "AFL-api",
        "version": "0.7.0",
        "documentation": "/docs",
    }
    assert ApiDiscoveryResponse.model_validate(response.json())


def test_ordinary_v1_reads_do_not_require_standard_read(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, _seed_historical_season)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM api_key_capabilities WHERE capability = 'standard-read'"
        )
    client = _client(db_path, monkeypatch)

    headers = {"x-api-key": API_KEY}
    assert client.get("/api/v1", headers=headers).status_code == 200
    assert client.get("/api/v1/seasons", headers=headers).status_code == 200


def test_seasons_requires_api_key_with_shared_auth_error(tmp_path, monkeypatch):
    client = _client(_make_db(tmp_path, _seed_historical_season), monkeypatch)

    missing = client.get("/api/v1/seasons")
    invalid = client.get("/api/v1/seasons", headers={"x-api-key": "invalid"})

    assert missing.status_code == invalid.status_code == 401
    assert missing.json() == invalid.json() == {"detail": "Invalid or missing API Key"}


def test_seasons_return_persisted_canonical_projection(tmp_path, monkeypatch):
    client = _client(_make_db(tmp_path, _seed_historical_season), monkeypatch)

    response = client.get("/api/v1/seasons", headers={"x-api-key": API_KEY})

    assert response.status_code == 200
    assert response.json() == {
        "seasons": [
            {
                "season_id": 85,
                "year": 2026,
                "name": "2026",
                "is_current": True,
                "current_round_number": 1,
            },
            {
                "season_id": 84,
                "year": 2025,
                "name": "2025",
                "is_current": False,
                "current_round_number": 24,
            },
        ]
    }
    payload = SeasonsResponse.model_validate(response.json())
    assert payload.seasons[0] == Season(
        season_id=85, year=2026, name="2026", is_current=True, current_round_number=1
    )
    assert sum(season.is_current for season in payload.seasons) == 1
    assert "metadata_json" not in response.text
    assert "source_json" not in response.text


def test_openapi_documents_discovery_and_seasons_models(tmp_path, monkeypatch):
    client = _client(_make_db(tmp_path, _seed_historical_season), monkeypatch)

    paths = client.get("/openapi.json").json()["paths"]

    assert paths["/api/v1"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/ApiDiscoveryResponse"}
    assert paths["/api/v1/seasons"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/SeasonsResponse"}
