"""Offline API tests for GET /api/v1/players/{canonical_player_id} and
GET /api/v1/players?search=.

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
        "INSERT INTO afl_teams VALUES(?,'CD_T1','Collingwood','COLL','Magpies','Collingwood',"
        "'Collingwood','MEN','{}','{}','{}',?)",
        (TEAM_ID, NOW),
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


def _search(client, params=None, headers=None):
    headers = headers or {"x-api-key": API_KEY}
    return client.get("/api/v1/players", params=params, headers=headers)


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


def test_membership_found_even_when_another_season_also_marked_current(tmp_path, monkeypatch):
    """A second season sharing is_current=1 (e.g. a second competition) must not
    hide a player's real current-season membership in the other one."""

    def seed(conn):
        _seed_seasons(conn)
        # A second, higher-afl_id "current" season the player has no membership in.
        conn.execute(
            "INSERT INTO afl_seasons VALUES(?,'CD_S99',1,'2026W','2026W',2026,1,1,NULL,NULL,'{}','{}',?)",
            (99, NOW),
        )
        _seed_player(conn, display_name="Nick Daicos")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["player"]["current_team"] == {"team_id": TEAM_ID, "name": "Collingwood"}


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


# --- GET /api/v1/players?search= ------------------------------------------

JOSH_DAICOS_ID = 396
NICK_DAICOS_ID = PLAYER_ID  # 584, matches the issue's worked example


def test_search_matches_a_player_name(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, NICK_DAICOS_ID, display_name="Nick Daicos")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _search(client, params={"search": "Nick Daicos"})

    assert response.status_code == 200
    assert [p["display_name"] for p in response.json()["players"]] == ["Nick Daicos"]


def test_search_matches_partial_name(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, NICK_DAICOS_ID, display_name="Nick Daicos")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _search(client, params={"search": "daic"})

    assert response.status_code == 200
    assert [p["display_name"] for p in response.json()["players"]] == ["Nick Daicos"]


def test_search_is_case_insensitive(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, NICK_DAICOS_ID, display_name="Nick Daicos")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    for term in ("DAICOS", "DaIcOs", "daicos"):
        response = _search(client, params={"search": term})
        assert response.status_code == 200
        assert [p["display_name"] for p in response.json()["players"]] == ["Nick Daicos"]


def test_search_returns_multiple_players_sharing_a_surname(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, JOSH_DAICOS_ID, display_name="Josh Daicos")
        _seed_player(conn, NICK_DAICOS_ID, display_name="Nick Daicos")
        _seed_provider_id(conn, JOSH_DAICOS_ID, "afl", "1321")
        _seed_provider_id(conn, NICK_DAICOS_ID, "afl", "5501")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _search(client, params={"search": "daicos"})

    assert response.status_code == 200
    players = response.json()["players"]
    assert {p["display_name"] for p in players} == {"Josh Daicos", "Nick Daicos"}
    assert {p["canonical_player_id"] for p in players} == {JOSH_DAICOS_ID, NICK_DAICOS_ID}


def test_search_result_ordering_is_deterministic(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        # Seed in reverse insertion order relative to expected (name, then id) output.
        _seed_player(conn, NICK_DAICOS_ID, display_name="Nick Daicos")
        _seed_player(conn, JOSH_DAICOS_ID, display_name="Josh Daicos")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    first = _search(client, params={"search": "daicos"}).json()["players"]
    second = _search(client, params={"search": "daicos"}).json()["players"]

    expected_order = [JOSH_DAICOS_ID, NICK_DAICOS_ID]  # "Josh" sorts before "Nick"
    assert [p["canonical_player_id"] for p in first] == expected_order
    assert [p["canonical_player_id"] for p in second] == expected_order


def test_search_with_no_matches_returns_200_with_empty_collection(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, NICK_DAICOS_ID, display_name="Nick Daicos")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _search(client, params={"search": "zzznotaplayer"})

    assert response.status_code == 200
    assert response.json() == {"players": []}


def test_search_result_reflects_missing_provider_mappings(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Daic Nomap")
        # No provider IDs and no current-season membership row at all.

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _search(client, params={"search": "daic"})

    assert response.status_code == 200
    player = response.json()["players"][0]
    assert player["current_team"] is None
    assert player["identifiers"] == {"afl_player_id": None, "champion_data_player_id": None}


def test_search_result_includes_current_team_and_partial_provider_mapping(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        _seed_provider_id(conn, PLAYER_ID, "champion_data", "CD_I1023261")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _search(client, params={"search": "daicos"})

    assert response.status_code == 200
    player = response.json()["players"][0]
    assert player["current_team"] == {"team_id": TEAM_ID, "name": "Collingwood"}
    assert player["identifiers"] == {"afl_player_id": None, "champion_data_player_id": "CD_I1023261"}


def test_missing_search_parameter_returns_422(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = _search(client, params=None)

    assert response.status_code == 422


def test_blank_search_parameter_returns_422(tmp_path, monkeypatch):
    """Both an empty and a whitespace-only search must reach the application's
    own search_required branch, not just get FastAPI's default validation
    error — a bare min_length constraint would only catch the empty case and
    reject it during framework validation before the handler ever runs."""

    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    expected_body = {
        "error": {
            "code": "search_required",
            "message": "A non-blank search query parameter is required.",
        }
    }

    empty_response = _search(client, params={"search": ""})
    whitespace_response = _search(client, params={"search": "   "})

    assert empty_response.status_code == 422
    assert empty_response.json() == expected_body
    assert whitespace_response.status_code == 422
    assert whitespace_response.json() == expected_body


def test_search_missing_api_key_returns_401(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = client.get("/api/v1/players", params={"search": "daicos"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


def test_search_invalid_api_key_returns_401(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = _search(client, params={"search": "daicos"}, headers={"x-api-key": "wrong-key"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


def test_search_does_not_affect_legacy_players_route(tmp_path, monkeypatch):
    """Regression guard: search discovery must not alter the legacy
    unversioned /api/players contract, which keeps reading the players table."""

    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        conn.execute(
            "INSERT INTO players(afl_id, first_name, last_name, club) VALUES(1,'J','Smith','COLL')"
        )

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    search_response = _search(client, params={"search": "daicos"})
    assert search_response.status_code == 200
    assert [p["display_name"] for p in search_response.json()["players"]] == ["Nick Daicos"]

    legacy_response = client.get("/api/players", headers={"x-api-key": API_KEY})
    assert legacy_response.status_code == 200
    legacy_names = [(row["first_name"], row["last_name"]) for row in legacy_response.json()]
    assert legacy_names == [("J", "Smith")]


def test_search_response_shape_and_openapi_documentation(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        _seed_provider_id(conn, PLAYER_ID, "afl", "5501")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _search(client, params={"search": "daicos"})

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"players"}
    player = body["players"][0]
    assert set(player.keys()) == {
        "canonical_player_id", "display_name", "current_team", "identifiers",
    }

    operation = client.get("/openapi.json").json()["paths"]["/api/v1/players"]["get"]
    assert {"200", "422"} <= set(operation["responses"])


# --- GET /api/v1/players/{canonical_player_id}/seasons --------------------

FUTURE_SEASON_ID = 86
TEAM_B_ID = 11


def _seed_future_season(conn, *, future_season_id=FUTURE_SEASON_ID, team_id=TEAM_B_ID):
    """Seed a 2027 season and a second club (Team B), distinct from the
    2025/2026 fixture seeded by ``_seed_seasons``/``TEAM_ID`` (Team A)."""
    conn.execute(
        "INSERT INTO afl_seasons VALUES(?,'CD_S86',1,'2027','2027',2027,0,1,NULL,NULL,'{}','{}',?)",
        (future_season_id, NOW),
    )
    conn.execute(
        "INSERT INTO afl_teams VALUES(?,'CD_T2','Essendon','ESS','Bombers','Essendon',"
        "'Essendon','MEN','{}','{}','{}',?)",
        (team_id, NOW),
    )
    conn.execute("INSERT INTO afl_team_seasons VALUES(?,?,?,?)", (future_season_id, team_id, NOW, NOW))


def _get_seasons(client, canonical_player_id=PLAYER_ID, headers=None):
    headers = headers or {"x-api-key": API_KEY}
    return client.get(f"/api/v1/players/{canonical_player_id}/seasons", headers=headers)


def test_seasons_returns_multiple_memberships_most_recent_first(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, display_name="Nick Daicos")
        _seed_membership(conn, PLAYER_ID, OTHER_SEASON_ID, TEAM_ID)
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get_seasons(client)

    assert response.status_code == 200
    body = response.json()
    assert body["canonical_player_id"] == PLAYER_ID
    assert [s["season_id"] for s in body["seasons"]] == [CURRENT_SEASON_ID, OTHER_SEASON_ID]
    assert [s["year"] for s in body["seasons"]] == [2026, 2025]


def test_seasons_reports_the_same_team_across_seasons(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, display_name="Nick Daicos")
        _seed_membership(conn, PLAYER_ID, OTHER_SEASON_ID, TEAM_ID)
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    seasons = _get_seasons(client).json()["seasons"]

    assert all(s["team"] == {"team_id": TEAM_ID, "name": "Collingwood"} for s in seasons)


def test_seasons_reflects_a_team_change_without_rewriting_earlier_history(tmp_path, monkeypatch):
    """Core historical-correctness scenario from Issue #182: a player moving
    from Team A (2025, 2026) to Team B (2027) must keep 2025/2026 reporting
    Team A -- the newest season's team must never be back-applied to older
    persisted season rows."""

    def seed(conn):
        _seed_seasons(conn)
        _seed_future_season(conn)
        _seed_player(conn, display_name="Journeyman Player")
        _seed_membership(conn, PLAYER_ID, OTHER_SEASON_ID, TEAM_ID)  # 2025 -> Team A
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)  # 2026 -> Team A
        _seed_membership(conn, PLAYER_ID, FUTURE_SEASON_ID, TEAM_B_ID)  # 2027 -> Team B

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get_seasons(client)

    assert response.status_code == 200
    seasons_by_year = {s["year"]: s for s in response.json()["seasons"]}
    assert seasons_by_year[2025]["team"] == {"team_id": TEAM_ID, "name": "Collingwood"}
    assert seasons_by_year[2026]["team"] == {"team_id": TEAM_ID, "name": "Collingwood"}
    assert seasons_by_year[2027]["team"] == {"team_id": TEAM_B_ID, "name": "Essendon"}
    # Most-recent-first ordering, not merely grouped correctly.
    assert [s["year"] for s in response.json()["seasons"]] == [2027, 2026, 2025]


def test_seasons_with_unresolved_team_reports_null(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, display_name="No Team Player")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, None)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get_seasons(client)

    assert response.status_code == 200
    assert response.json()["seasons"] == [
        {"season_id": CURRENT_SEASON_ID, "year": 2026, "name": "2026", "team": None}
    ]


def test_player_with_no_season_membership_returns_empty_collection(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, display_name="Unlisted Player")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get_seasons(client)

    assert response.status_code == 200
    assert response.json() == {"canonical_player_id": PLAYER_ID, "seasons": []}


def test_seasons_unknown_canonical_player_returns_404(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = _get_seasons(client, canonical_player_id=999999)

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "player_not_found", "message": "Player not found."}
    }


def test_seasons_missing_api_key_returns_401(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = client.get(f"/api/v1/players/{PLAYER_ID}/seasons")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


def test_seasons_invalid_api_key_returns_401(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = _get_seasons(client, headers={"x-api-key": "wrong-key"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


def test_seasons_response_shape_and_openapi_documentation(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, display_name="Nick Daicos")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get_seasons(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"canonical_player_id", "seasons"}
    assert set(body["seasons"][0].keys()) == {"season_id", "year", "name", "team"}
    assert set(body["seasons"][0]["team"].keys()) == {"team_id", "name"}

    operation = client.get("/openapi.json").json()["paths"][
        "/api/v1/players/{canonical_player_id}/seasons"
    ]["get"]
    assert {"200", "404", "422"} <= set(operation["responses"])
    assert operation["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApplicationErrorResponse"
    }


def test_seasons_endpoint_does_not_change_existing_player_resource_contract(tmp_path, monkeypatch):
    """Backwards-compatibility guard for #180/#181: adding the seasons
    sub-resource must not alter GET /api/v1/players/{id} or its search
    sibling's existing response shape or behaviour."""

    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, display_name="Nick Daicos")
        _seed_provider_id(conn, PLAYER_ID, "afl", "5501")
        _seed_provider_id(conn, PLAYER_ID, "champion_data", "CD_I1023261")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    player_response = _get(client)
    assert player_response.status_code == 200
    assert player_response.json() == {
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

    search_response = _search(client, params={"search": "daicos"})
    assert search_response.status_code == 200
    assert search_response.json()["players"][0]["canonical_player_id"] == PLAYER_ID


def test_navigation_from_player_seasons_to_season_rounds(tmp_path, monkeypatch):
    """End-to-end navigation exercise for Issue #182's target workflow:
    canonical player -> season memberships -> team for each season -> the
    season's own resources (rounds, and onward to matches/player-stats)."""

    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, display_name="Nick Daicos")
        _seed_provider_id(conn, PLAYER_ID, "champion_data", "CD_I1023261")
        _seed_membership(conn, PLAYER_ID, OTHER_SEASON_ID, TEAM_ID)
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)
        conn.execute(
            "INSERT INTO rounds(round_id,round_label,season_id,competition_id,provider_id,round_number) "
            "VALUES(201,'Round 1',?,1,'CD_R_HIST',1)",
            (OTHER_SEASON_ID,),
        )

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    seasons_response = _get_seasons(client)
    assert seasons_response.status_code == 200
    historical_season = next(
        s for s in seasons_response.json()["seasons"] if s["year"] == 2025
    )
    assert historical_season["team"] == {"team_id": TEAM_ID, "name": "Collingwood"}

    rounds_response = client.get(
        f"/api/v1/seasons/{historical_season['season_id']}/rounds",
        headers={"x-api-key": API_KEY},
    )
    assert rounds_response.status_code == 200
    assert [r["round_id"] for r in rounds_response.json()["rounds"]] == [201]

    identifiers_response = _get(client)
    champion_data_id = identifiers_response.json()["player"]["identifiers"]["champion_data_player_id"]
    assert champion_data_id == "CD_I1023261"
