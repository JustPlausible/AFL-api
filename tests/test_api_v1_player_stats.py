from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from api.routes import router as legacy_router
from api.routes_v1 import router as v1_router
from api_key_security import api_key_prefix, hash_api_key
from db.migration_runner import migrate_database


API_KEY = "offline-test-key"
NOW = "2026-08-09T09:32:11+00:00"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    path = tmp_path / "api.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO api_keys(label,key_hash,key_prefix,is_active) VALUES(?,?,?,1)",
        ("tests", hash_api_key(API_KEY), api_key_prefix(API_KEY)),
    )
    for team_id in (10, 11):
        conn.execute(
            "INSERT INTO afl_teams(afl_id,season_id,updated_at) VALUES(?,85,?)",
            (team_id, NOW),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(path))
    app = FastAPI()
    app.include_router(legacy_router)
    app.include_router(v1_router)
    return TestClient(app), path


def add_match(path, *, match_id=8216, provider_id="CD_M1"):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO matches(match_id,match_provider_id,round_id,season_id,status,"
        "home_team_id,away_team_id) VALUES(?,?,12,85,'CONCLUDED',10,11)",
        (match_id, provider_id),
    )
    conn.commit()
    conn.close()


def add_player(path, player, side, authority, *, mapped=True):
    conn = sqlite3.connect(path)
    canonical_id = player if mapped else None
    if mapped:
        conn.execute(
            "INSERT INTO canonical_players(id,display_name,given_name,family_name,created_at,updated_at) "
            "VALUES(?,NULL,'Player',?,?,?)",
            (player, str(player), NOW, NOW),
        )
        conn.execute(
            "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) "
            "VALUES(?,'afl',?,?,?)",
            (player, str(1000 + player), NOW, NOW),
        )
    conn.execute(
        "INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,afl_match_id,"
        "side,collected_at,source_endpoint,resolved_match_status,snapshot_authority,goals,behinds,"
        "kicks,handballs,disposals,marks,tackles,hitouts,extra_stats_json,raw_player_json,canonical_player_id) "
        "VALUES('CD_M1',?,'8216',?,?,'match_player_statistics',?, ?,2,1,12,8,20,5,4,0,'{\"clearances\": 6}','{\"private\": true}',?)",
        (f"CD_I{player}", side, NOW, "CONCLUDED" if authority == 2 else "LIVE", authority, canonical_id),
    )
    conn.commit()
    conn.close()


def get(client, path, *, key=API_KEY):
    return client.get(path, headers={"X-Api-Key": key})


def test_unknown_match_returns_404(api):
    client, _ = api
    response = get(client, "/api/v1/matches/999/player-stats")
    assert response.status_code == 404
    assert response.json() == {"detail": "Match not found"}


@pytest.mark.parametrize("provider_id", [None, "CD_EMPTY"])
def test_unresolved_or_empty_match_is_not_available(api, provider_id):
    client, path = api
    add_match(path, provider_id=provider_id)
    response = get(client, "/api/v1/matches/8216/player-stats")
    assert response.status_code == 200
    assert response.json()["players"] == []
    assert response.json()["lifecycle"] == {
        "finality": "not_available",
        "authoritative_rows": 0,
        "authoritative_sides": 0,
        "min_snapshot_authority": None,
        "max_snapshot_authority": None,
    }


def test_live_rows_are_returned_but_not_available(api):
    client, path = api
    add_match(path)
    add_player(path, 1, "home", 1)
    body = get(client, "/api/v1/matches/8216/player-stats").json()
    assert body["lifecycle"]["finality"] == "not_available"
    assert body["lifecycle"]["min_snapshot_authority"] == 1
    assert body["players"][0]["snapshot_authority"] == 1


@pytest.mark.parametrize(
    ("rows", "expected"),
    [([("home", 2)], "partial"), (["mixed"], "partial"),
     ([('home', 2), ('away', 2)], "final")],
)
def test_authoritative_finality_states(api, rows, expected):
    client, path = api
    add_match(path)
    if rows == ["mixed"]:
        rows = [("home", 2), ("away", 1)]
    for player, (side, authority) in enumerate(rows, start=1):
        add_player(path, player, side, authority)
    lifecycle = get(client, "/api/v1/matches/8216/player-stats").json()["lifecycle"]
    assert lifecycle["finality"] == expected
    assert lifecycle["authoritative_rows"] == sum(authority == 2 for _, authority in rows)
    assert lifecycle["authoritative_sides"] == len({side for side, authority in rows if authority == 2})


def test_identity_filters_order_and_exact_response_shape(api):
    client, path = api
    add_match(path)
    add_player(path, 2, "away", 2, mapped=False)
    add_player(path, 1, "home", 2)

    body = get(client, "/api/v1/matches/8216/player-stats?side=home").json()
    assert set(body) == {"match", "lifecycle", "players"}
    assert set(body["match"]) == {"match_id", "match_provider_id", "round_id", "season_id", "status"}
    assert set(body["lifecycle"]) == {
        "finality", "authoritative_rows", "authoritative_sides",
        "min_snapshot_authority", "max_snapshot_authority",
    }
    player = body["players"][0]
    assert set(player) == {
        "champion_data_player_id", "canonical_player_id", "afl_player_id", "display_name",
        "side", "team_id", "stats", "snapshot_authority", "resolved_match_status", "collected_at",
    }
    assert set(player["stats"]) == {
        "goals", "behinds", "kicks", "handballs", "disposals", "marks", "tackles", "hitouts",
    }
    assert player["display_name"] == "Player 1"
    assert player["team_id"] == 10

    unmapped = get(
        client, "/api/v1/matches/8216/player-stats?champion_data_player_id=CD_I2"
    ).json()["players"]
    assert len(unmapped) == 1
    assert unmapped[0]["canonical_player_id"] is None
    assert unmapped[0]["afl_player_id"] is None
    assert unmapped[0]["display_name"] is None
    assert unmapped[0]["team_id"] == 11


def test_invalid_side_and_auth_use_existing_validation(api):
    client, path = api
    add_match(path)
    assert get(client, "/api/v1/matches/8216/player-stats?side=centre").status_code == 422
    assert client.get("/api/v1/matches/8216/player-stats").status_code == 422
    response = get(client, "/api/v1/matches/8216/player-stats", key="wrong")
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


def test_legacy_player_stats_route_shape_and_table_are_unchanged(api):
    client, path = api
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO player_stats(match_id,player_name,team_code,status,scraped_at,goals) "
        "VALUES(8216,'Legacy Player','LEG','COMPLETED',?,9)",
        (NOW,),
    )
    expected = dict(zip(
        [row[1] for row in conn.execute("PRAGMA table_info(player_stats)")],
        conn.execute("SELECT * FROM player_stats").fetchone(),
    ))
    conn.commit()
    conn.close()
    response = get(client, "/api/player-stats?match_id=8216")
    assert response.status_code == 200
    assert response.json() == [expected]
