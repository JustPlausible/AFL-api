"""Offline API tests for GET /api/v1/matches/{match_id}/player-stats.

Fixture shape mirrors the DB-building conventions already used by
``tests/test_afl_season_report.py`` (raw inserts against a fully migrated
SQLite database) and ``tests/test_api_key_hashing.py`` (hashed API-key
fixtures against the real ``verify_api_key`` dependency). No test contacts
AFL/CFS; everything runs against an isolated on-disk SQLite fixture.
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
OLDER = datetime(2026, 8, 2, 11, tzinfo=timezone.utc).isoformat()
API_KEY = "v1-test-key"

HOME_TEAM_ID = 10
AWAY_TEAM_ID = 11
ROUND_ID = 101
SEASON_ID = 85
MATCH_ID = 8216
MATCH_PROVIDER_ID = "CD_M20260142001"


def _seed_metadata(conn):
    conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}',?)", (NOW,))
    conn.execute(
        "INSERT INTO afl_seasons VALUES(?,'CD_S85',1,'2026','2026',2026,1,1,NULL,NULL,'{}','{}',?)",
        (SEASON_ID, NOW),
    )
    for team_id, provider in ((HOME_TEAM_ID, "CD_T1"), (AWAY_TEAM_ID, "CD_T2")):
        conn.execute(
            "INSERT INTO afl_teams VALUES(?,?,?,?,?,?,?,?, 'MEN','{}','{}','{}',?)",
            (team_id, provider, SEASON_ID, provider, provider, provider, provider, provider, NOW),
        )
    conn.execute(
        "INSERT INTO rounds(round_id,round_label,season_id,competition_id,provider_id,round_number) "
        "VALUES(?,'Round 1',?,1,'CD_R1',1)",
        (ROUND_ID, SEASON_ID),
    )


def _seed_match(conn, *, match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID,
                 status="CONCLUDED"):
    conn.execute(
        "INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,"
        "status,start_time_utc,season_id,home_team_id,away_team_id) "
        "VALUES(?,?,?,'A','B','MCG',?,'2026-08-02T00:00:00+00:00',?,?,?)",
        (match_id, match_provider_id, ROUND_ID, status, SEASON_ID, HOME_TEAM_ID, AWAY_TEAM_ID),
    )


def _seed_player(conn, player_id, *, champion_data_id, afl_id=None, display_name=None):
    conn.execute(
        "INSERT INTO canonical_players VALUES(?,?,?,?,?,?)",
        (player_id, display_name, None, None, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) "
        "VALUES(?,'champion_data',?,?,?)",
        (player_id, champion_data_id, NOW, NOW),
    )
    if afl_id is not None:
        conn.execute(
            "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) "
            "VALUES(?,'afl',?,?,?)",
            (player_id, str(afl_id), NOW, NOW),
        )


def _seed_stat_row(conn, *, champion_data_player_id, side, snapshot_authority,
                    match_provider_id=MATCH_PROVIDER_ID, canonical_player_id=None,
                    resolved_match_status="CONCLUDED", collected_at=NOW,
                    goals=3, behinds=1, kicks=12, handballs=8, disposals=20,
                    marks=5, tackles=4, hitouts=0):
    conn.execute(
        "INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,afl_match_id,"
        "team_provider_id,side,collected_at,source_endpoint,resolved_match_status,"
        "snapshot_authority,goals,behinds,kicks,handballs,disposals,marks,tackles,hitouts,"
        "extra_stats_json,raw_player_json,canonical_player_id) "
        "VALUES(?,?,?,NULL,?,?,'match_player_stats',?,?,?,?,?,?,?,?,?,?,'{}','{}',?)",
        (match_provider_id, champion_data_player_id, str(MATCH_ID), side, collected_at,
         resolved_match_status, snapshot_authority, goals, behinds, kicks, handballs,
         disposals, marks, tackles, hitouts, canonical_player_id),
    )


def _seed_api_key(conn):
    create_api_keys_table(conn.cursor())
    conn.execute(
        "INSERT INTO api_keys (label, api_key, key_hash, key_prefix, is_active) VALUES (?, NULL, ?, ?, 1)",
        ("v1-tests", hash_api_key(API_KEY), api_key_prefix(API_KEY)),
    )
    key_id = conn.execute("SELECT id FROM api_keys WHERE label = 'v1-tests'").fetchone()[0]
    conn.execute(
        "INSERT INTO api_key_capabilities(api_key_id, capability) VALUES(?, 'standard-read')",
        (key_id,),
    )


def _grant_advanced(db_path):
    conn = sqlite3.connect(db_path)
    key_id = conn.execute("SELECT id FROM api_keys WHERE label = 'v1-tests'").fetchone()[0]
    conn.execute(
        "INSERT INTO api_key_capabilities(api_key_id, capability) VALUES(?, 'advanced-read')",
        (key_id,),
    )
    conn.commit()
    conn.close()


def _make_db(tmp_path, seed):
    path = tmp_path / "player_stats.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _seed_metadata(conn)
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


def _get(client, match_id=MATCH_ID, **params):
    params.setdefault("headers", {"x-api-key": API_KEY})
    headers = params.pop("headers")
    return client.get(f"/api/v1/matches/{match_id}/player-stats", params=params, headers=headers)


def test_unknown_match_id_returns_404(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_match(conn))
    client = _client(db_path, monkeypatch)

    response = _get(client, match_id=999999)

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "match_not_found", "message": "Match not found."}
    }


def test_match_without_provider_id_is_not_available(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_match(conn, match_provider_id=None))
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    body = response.json()
    assert body["match"]["match_provider_id"] is None
    assert body["players"] == []
    assert body["lifecycle"]["finality"] == "not_available"
    assert body["metadata"] == {"source_updated_at": None}


def test_provider_id_with_no_stat_rows_is_not_available(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_match(conn))
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    body = response.json()
    assert body["match"]["match_provider_id"] == MATCH_PROVIDER_ID
    assert body["players"] == []
    assert body["lifecycle"]["finality"] == "not_available"
    assert body["metadata"] == {"source_updated_at": None}


def test_live_partial_rows_are_not_available_but_populated(tmp_path, monkeypatch):
    def seed(conn):
        _seed_match(conn, status="LIVE")
        for idx, side in ((1, "home"), (2, "away")):
            _seed_player(conn, idx, champion_data_id=f"CD_I{idx}", afl_id=idx)
            _seed_stat_row(
                conn, champion_data_player_id=f"CD_I{idx}", side=side,
                snapshot_authority=1, canonical_player_id=idx,
                resolved_match_status="LIVE",
            )

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    body = response.json()
    assert body["lifecycle"]["finality"] == "not_available"
    assert len(body["players"]) == 2
    assert all("advanced" not in player for player in body["players"])
    assert body["metadata"]["source_updated_at"] == NOW


def test_one_sided_authoritative_rows_are_partial(tmp_path, monkeypatch):
    def seed(conn):
        _seed_match(conn)
        for idx in (1, 2):
            _seed_player(conn, idx, champion_data_id=f"CD_I{idx}", afl_id=idx)
            _seed_stat_row(
                conn, champion_data_player_id=f"CD_I{idx}", side="home",
                snapshot_authority=2, canonical_player_id=idx,
            )

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["lifecycle"]["finality"] == "partial"


def test_mixed_authority_rows_are_partial(tmp_path, monkeypatch):
    def seed(conn):
        _seed_match(conn)
        _seed_player(conn, 1, champion_data_id="CD_I1", afl_id=1)
        _seed_player(conn, 2, champion_data_id="CD_I2", afl_id=2)
        _seed_stat_row(conn, champion_data_player_id="CD_I1", side="home",
                        snapshot_authority=2, canonical_player_id=1)
        _seed_stat_row(conn, champion_data_player_id="CD_I2", side="away",
                        snapshot_authority=1, canonical_player_id=2)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["lifecycle"]["finality"] == "partial"


def test_two_sided_concluded_rows_are_final(tmp_path, monkeypatch):
    def seed(conn):
        _seed_match(conn)
        _seed_player(conn, 1, champion_data_id="CD_I1", afl_id=101, display_name="J. Smith")
        _seed_player(conn, 2, champion_data_id="CD_I2", afl_id=102, display_name="A. Jones")
        _seed_stat_row(conn, champion_data_player_id="CD_I1", side="home",
                        snapshot_authority=2, canonical_player_id=1)
        _seed_stat_row(conn, champion_data_player_id="CD_I2", side="away",
                        snapshot_authority=2, canonical_player_id=2)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    body = response.json()
    assert body["lifecycle"]["finality"] == "final"
    assert body["lifecycle"] == {"finality": "final"}
    assert body["metadata"] == {"source_updated_at": NOW}
    home_player = next(p for p in body["players"] if p["side"] == "home")
    assert home_player["champion_data_player_id"] == "CD_I1"
    assert home_player["canonical_player_id"] == 1
    assert home_player["afl_player_id"] == 101
    assert home_player["display_name"] == "J. Smith"
    assert home_player["team_id"] == HOME_TEAM_ID
    assert home_player["stats"] == {
        "goals": 3, "behinds": 1, "kicks": 12, "handballs": 8,
        "disposals": 20, "marks": 5, "tackles": 4, "hitouts": 0,
    }


def test_unresolved_canonical_and_afl_mapping_is_null_without_error(tmp_path, monkeypatch):
    def seed(conn):
        _seed_match(conn)
        _seed_stat_row(conn, champion_data_player_id="CD_UNMAPPED", side="home",
                        snapshot_authority=2, canonical_player_id=None)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    player = response.json()["players"][0]
    assert player["canonical_player_id"] is None
    assert player["afl_player_id"] is None
    assert player["display_name"] is None


def _two_player_seed(conn):
    _seed_match(conn)
    _seed_player(conn, 1, champion_data_id="CD_I1", afl_id=1)
    _seed_player(conn, 2, champion_data_id="CD_I2", afl_id=2)
    _seed_stat_row(conn, champion_data_player_id="CD_I1", side="home",
                    snapshot_authority=2, canonical_player_id=1)
    _seed_stat_row(conn, champion_data_player_id="CD_I2", side="away",
                    snapshot_authority=2, canonical_player_id=2)


def test_side_filter_narrows_results(tmp_path, monkeypatch):
    def seed(conn):
        _seed_match(conn)
        _seed_player(conn, 1, champion_data_id="CD_I1", afl_id=1)
        _seed_player(conn, 2, champion_data_id="CD_I2", afl_id=2)
        _seed_stat_row(conn, champion_data_player_id="CD_I1", side="home",
                       snapshot_authority=2, canonical_player_id=1, collected_at=OLDER)
        _seed_stat_row(conn, champion_data_player_id="CD_I2", side="away",
                       snapshot_authority=2, canonical_player_id=2, collected_at=NOW)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client, side="home")

    assert response.status_code == 200
    players = response.json()["players"]
    assert len(players) == 1
    assert players[0]["side"] == "home"
    assert response.json()["metadata"]["source_updated_at"] == OLDER


def test_champion_data_player_id_filter_narrows_to_one_player(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=_two_player_seed)
    client = _client(db_path, monkeypatch)

    response = _get(client, champion_data_player_id="CD_I2")

    assert response.status_code == 200
    players = response.json()["players"]
    assert len(players) == 1
    assert players[0]["champion_data_player_id"] == "CD_I2"


def test_filter_with_no_rows_has_no_source_timestamp(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=_two_player_seed)
    client = _client(db_path, monkeypatch)

    response = _get(client, champion_data_player_id="CD_MISSING")

    assert response.status_code == 200
    assert response.json()["players"] == []
    assert response.json()["metadata"] == {"source_updated_at": None}


def test_invalid_side_returns_422(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_match(conn))
    client = _client(db_path, monkeypatch)

    response = _get(client, side="north")

    assert response.status_code == 422


def test_missing_api_key_header_returns_401(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_match(conn))
    client = _client(db_path, monkeypatch)

    response = client.get(f"/api/v1/matches/{MATCH_ID}/player-stats")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


def test_invalid_api_key_returns_401(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_match(conn))
    client = _client(db_path, monkeypatch)

    response = _get(client, headers={"x-api-key": "wrong-key"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


def test_response_shape_regression(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=_two_player_seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"match", "lifecycle", "metadata", "players"}
    assert set(body["match"].keys()) == {
        "match_id", "match_provider_id", "round_id", "season_id", "status",
    }
    assert set(body["lifecycle"].keys()) == {"finality"}
    assert set(body["metadata"].keys()) == {"source_updated_at"}
    assert set(body["players"][0].keys()) == {
        "champion_data_player_id", "canonical_player_id", "afl_player_id",
        "display_name", "side", "team_id", "stats",
    }
    assert set(body["players"][0]["stats"].keys()) == {
        "goals", "behinds", "kicks", "handballs", "disposals", "marks", "tackles", "hitouts",
    }


def test_standard_key_cannot_request_advanced_metadata(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=_two_player_seed)
    client = _client(db_path, monkeypatch)

    response = _get(client, advanced="true")

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "advanced_access_required",
            "message": "This API key does not permit access to advanced metadata.",
        }
    }
    assert "snapshot_authority" not in response.text


def test_advanced_mode_is_strictly_additive(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=_two_player_seed)
    _grant_advanced(db_path)
    client = _client(db_path, monkeypatch)

    normal = _get(client).json()
    response = _get(client, advanced="true")

    assert response.status_code == 200
    advanced = response.json()
    assert set(advanced) == {*normal, "advanced"}
    evidence = advanced["advanced"]["finality_evidence"]
    assert set(evidence) == {
        "authoritative_rows", "authoritative_sides", "min_snapshot_authority",
        "max_snapshot_authority",
    }
    assert evidence == {
        "authoritative_rows": 2, "authoritative_sides": 2,
        "min_snapshot_authority": 2, "max_snapshot_authority": 2,
    }
    for normal_player, advanced_player in zip(normal["players"], advanced["players"]):
        provenance = advanced_player.pop("advanced")
        assert set(provenance) == {
            "snapshot_authority", "resolved_match_status", "collected_at",
        }
        assert advanced_player == normal_player
    advanced.pop("advanced")
    assert advanced == normal


def test_openapi_documents_advanced_and_application_errors(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_match(conn))
    client = _client(db_path, monkeypatch)

    operation = client.get("/openapi.json").json()["paths"][
        "/api/v1/matches/{match_id}/player-stats"
    ]["get"]

    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert parameters["advanced"]["schema"]["default"] is False
    assert {"200", "403", "404", "422"} <= set(operation["responses"])
    assert operation["responses"]["403"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApplicationErrorResponse"
    }


def test_existing_unversioned_routes_are_unchanged(tmp_path, monkeypatch):
    def seed(conn):
        _two_player_seed(conn)
        conn.execute(
            "INSERT INTO player_stats(match_id,round_id,afl_id,champion_id,player_name,"
            "team_code,goals,status,scraped_at) "
            "VALUES(?,?,1,'CD_I1','J. Smith','A',3,'COMPLETED',?)",
            (MATCH_ID, ROUND_ID, NOW),
        )

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    legacy_response = client.get(
        "/api/player-stats", params={"match_id": MATCH_ID}, headers={"x-api-key": API_KEY}
    )
    assert legacy_response.status_code == 200
    legacy_rows = legacy_response.json()
    assert len(legacy_rows) == 1
    assert set(legacy_rows[0].keys()) == {
        "id", "match_id", "round_id", "afl_id", "champion_id", "player_name",
        "jumper_number", "team_code", "af_score", "goals", "behinds",
        "disposals", "kicks", "handballs", "marks", "tackles", "hitouts",
        "clearances", "metres_gained", "goal_assists", "time_on_ground_pct",
        "status", "scraped_at",
    }
    assert legacy_rows[0]["champion_id"] == "CD_I1"

    match_response = client.get(
        f"/api/matches/{MATCH_ID}", headers={"x-api-key": API_KEY}
    )
    assert match_response.status_code == 200
    assert match_response.json()["match_id"] == MATCH_ID

    no_filter_response = client.get("/api/player-stats", headers={"x-api-key": API_KEY})
    assert no_filter_response.status_code == 400
