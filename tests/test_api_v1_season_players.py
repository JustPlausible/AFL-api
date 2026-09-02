"""Offline API tests for GET /api/v1/seasons/{season_id}/players.

Fixture conventions mirror ``tests/test_api_v1_players.py``: raw inserts
against a fully migrated SQLite database, hashed API-key fixtures against the
real ``authenticate_api_key`` dependency. No test contacts AFL/CFS.

Issue #247's population authority is ``competition_season_players`` only --
these tests exist specifically to prove the endpoint neither reads nor
requires StatsPro, derived Home & Away summaries, match player-stat
appearances, match rosters, or the legacy ``players`` table.
"""

import sqlite3

from tests.test_api_v1_players import (
    API_KEY,
    CURRENT_SEASON_ID,
    FUTURE_SEASON_ID,
    NOW,
    OTHER_SEASON_ID,
    PLAYER_ID,
    TEAM_B_ID,
    TEAM_ID,
    _client,
    _make_db,
    _seed_future_season,
    _seed_membership,
    _seed_player,
    _seed_provider_id,
    _seed_seasons,
)

JOSH_ID = 396


def _get(client, season_id=CURRENT_SEASON_ID, params=None, headers=None):
    headers = headers or {"x-api-key": API_KEY}
    return client.get(f"/api/v1/seasons/{season_id}/players", params=params, headers=headers)


def _seed_bulk_membership(conn, season_id, team_id, count, start_id):
    rows_players = [
        (start_id + i, f"Bulk Player {start_id + i}", None, None, NOW, NOW) for i in range(count)
    ]
    conn.executemany(
        "INSERT INTO canonical_players(id, display_name, given_name, family_name, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?)",
        rows_players,
    )
    rows_membership = [
        (start_id + i, season_id, team_id, NOW, NOW) for i in range(count)
    ]
    conn.executemany(
        "INSERT INTO competition_season_players(player_id,competition_season_id,team_id,"
        "source_provider,source_json,created_at,updated_at) "
        "VALUES(?,?,?,'champion_data','{}',?,?)",
        rows_membership,
    )


# --- 1. known season returns its canonical membership population ----------


def test_known_season_returns_its_membership_population(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        _seed_provider_id(conn, PLAYER_ID, "afl", "5501")
        _seed_provider_id(conn, PLAYER_ID, "champion_data", "CD_I1023261")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert response.json() == {
        "players": [
            {
                "canonical_player_id": PLAYER_ID,
                "display_name": "Nick Daicos",
                "team": {"team_id": TEAM_ID, "name": "Collingwood"},
                "identifiers": {
                    "afl_player_id": 5501,
                    "champion_data_player_id": "CD_I1023261",
                },
            }
        ],
        "limit": 250,
        "offset": 0,
    }


# --- 2/3/16. sourced only from competition_season_players ------------------


def test_membership_excludes_players_only_present_in_match_stats(tmp_path, monkeypatch):
    """A player with a cfs_player_stats row for this season, but no
    competition_season_players row, must not appear -- membership is never
    derived from match appearances."""

    APPEARANCE_ONLY_ID = 700

    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)
        _seed_player(conn, APPEARANCE_ONLY_ID, display_name="Appearance Only")
        conn.execute(
            "INSERT INTO rounds(round_id,round_label,season_id,competition_id,provider_id,round_number) "
            "VALUES(9101,'Round 1',?,1,'CD_R9101',1)",
            (CURRENT_SEASON_ID,),
        )
        conn.execute(
            "INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,"
            "status,start_time_utc,season_id,home_team_id,away_team_id) "
            "VALUES(9001,'CD_M9001',9101,'A','B','MCG','CONCLUDED','2026-04-01T00:00:00+00:00',"
            "?,?,?)",
            (CURRENT_SEASON_ID, TEAM_ID, TEAM_ID),
        )
        conn.execute(
            "INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,side,"
            "collected_at,source_endpoint,snapshot_authority,canonical_player_id,extra_stats_json,"
            "raw_player_json) VALUES('CD_M9001','CD_I700','home',?,?,2,?,'{}','{}')",
            (NOW, "test", APPEARANCE_ONLY_ID),
        )

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    ids = [p["canonical_player_id"] for p in response.json()["players"]]
    assert ids == [PLAYER_ID]
    assert APPEARANCE_ONLY_ID not in ids


def test_membership_excludes_players_only_in_statspro_or_derived_summaries(tmp_path, monkeypatch):
    STATSPRO_ONLY_ID = 701
    DERIVED_ONLY_ID = 702

    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)
        _seed_player(conn, STATSPRO_ONLY_ID, display_name="StatsPro Only")
        _seed_player(conn, DERIVED_ONLY_ID, display_name="Derived Only")
        conn.execute(
            "INSERT INTO statspro_player_season_summaries(canonical_player_id,player_provider_id,"
            "season_id,season_provider_id,team_id,team_provider_id,source,source_context,scope,"
            "games_played,published_totals,published_averages,source_updated_at,collected_at) "
            "VALUES(?,'CD_I701',?,'CD_S85',?,'CD_T1','AFL_STATSPRO','SEASON_TOTAL','full_season',"
            "5,'{}','{}',NULL,?)",
            (STATSPRO_ONLY_ID, CURRENT_SEASON_ID, TEAM_ID, NOW),
        )
        conn.execute(
            "INSERT INTO derived_player_season_summaries(season_id,canonical_player_id,team_id,"
            "scope,source,population_source,games_played,totals,derived_rates,built_at,"
            "source_max_updated_at,finalized) VALUES(?,?,?,'home_and_away','DERIVED_MATCH_STATS',"
            "'competition_season_players',5,'{}','{}',?,?,1)",
            (CURRENT_SEASON_ID, DERIVED_ONLY_ID, TEAM_ID, NOW, NOW),
        )

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    ids = {p["canonical_player_id"] for p in response.json()["players"]}
    assert ids == {PLAYER_ID}


def test_endpoint_works_with_no_statspro_or_ha_summary_rows_at_all(tmp_path, monkeypatch):
    """No statspro_player_season_summaries/derived_player_season_summaries
    rows exist anywhere in the database, and the endpoint still serves the
    persisted membership -- the pre-season scenario Issue #247 targets."""

    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert [p["canonical_player_id"] for p in response.json()["players"]] == [PLAYER_ID]


def test_membership_does_not_read_legacy_players_table(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)
        conn.execute(
            "INSERT INTO players(afl_id, first_name, last_name, club) VALUES(1,'J','Smith','COLL')"
        )

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    ids = [p["canonical_player_id"] for p in response.json()["players"]]
    assert ids == [PLAYER_ID]
    assert all(p["display_name"] != "J Smith" for p in response.json()["players"])


# --- 4/5. pagination across >250 rows, ~800+ population --------------------


def test_pagination_across_more_than_250_rows(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_bulk_membership(conn, CURRENT_SEASON_ID, TEAM_ID, count=300, start_id=1000)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    first_page = _get(client).json()
    assert len(first_page["players"]) == 250
    assert first_page["limit"] == 250
    assert first_page["offset"] == 0
    assert [p["canonical_player_id"] for p in first_page["players"]] == list(range(1000, 1250))

    second_page = _get(client, params={"limit": 250, "offset": 250}).json()
    assert len(second_page["players"]) == 50
    assert [p["canonical_player_id"] for p in second_page["players"]] == list(range(1250, 1300))


def test_representative_full_season_population(tmp_path, monkeypatch):
    """~800-900 players is a realistic AFL season population; four
    default/max-size requests should retrieve it all with no duplicates or
    gaps."""

    TOTAL = 860

    def seed(conn):
        _seed_seasons(conn)
        _seed_bulk_membership(conn, CURRENT_SEASON_ID, TEAM_ID, count=TOTAL, start_id=2000)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    seen = []
    offset = 0
    while True:
        page = _get(client, params={"limit": 250, "offset": offset}).json()
        seen.extend(p["canonical_player_id"] for p in page["players"])
        if len(page["players"]) < 250:
            break
        offset += 250

    assert len(seen) == TOTAL
    assert len(set(seen)) == TOTAL
    assert seen == sorted(seen)


# --- 6/7. deterministic ordering, final partial page ------------------------


def test_ordering_is_deterministic_by_canonical_player_id(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        # Seed in descending order relative to expected ascending output.
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        _seed_player(conn, JOSH_ID, display_name="Josh Daicos")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)
        _seed_membership(conn, JOSH_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    first = _get(client).json()["players"]
    second = _get(client).json()["players"]

    expected_order = [JOSH_ID, PLAYER_ID]
    assert [p["canonical_player_id"] for p in first] == expected_order
    assert [p["canonical_player_id"] for p in second] == expected_order


def test_final_partial_page(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_bulk_membership(conn, CURRENT_SEASON_ID, TEAM_ID, count=260, start_id=3000)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    last_page = _get(client, params={"limit": 250, "offset": 250}).json()

    assert len(last_page["players"]) == 10
    assert [p["canonical_player_id"] for p in last_page["players"]] == list(range(3250, 3260))


# --- 8. offset beyond population --------------------------------------------


def test_offset_beyond_population_returns_empty_collection(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client, params={"offset": 500})

    assert response.status_code == 200
    assert response.json() == {"players": [], "limit": 250, "offset": 500}


# --- 9/10. requested-season team semantics ----------------------------------


def test_requested_season_team_unaffected_by_newer_season_club_change(tmp_path, monkeypatch):
    """2026 membership -> Team A, 2027 membership -> Team B;
    GET /api/v1/seasons/{2026}/players must report Team A, never Team B."""

    def seed(conn):
        _seed_seasons(conn)
        _seed_future_season(conn)
        _seed_player(conn, PLAYER_ID, display_name="Journeyman Player")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)  # 2026 -> Team A
        _seed_membership(conn, PLAYER_ID, FUTURE_SEASON_ID, TEAM_B_ID)  # 2027 -> Team B

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    current_response = _get(client, season_id=CURRENT_SEASON_ID).json()
    future_response = _get(client, season_id=FUTURE_SEASON_ID).json()

    assert current_response["players"][0]["team"] == {"team_id": TEAM_ID, "name": "Collingwood"}
    assert future_response["players"][0]["team"] == {"team_id": TEAM_B_ID, "name": "Essendon"}


def test_unresolved_requested_season_team_is_null(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="No Team Player")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, None)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["players"][0]["team"] is None


def test_requested_season_team_never_borrows_from_current_season(tmp_path, monkeypatch):
    """The requested (historical) season has no resolved team; even though a
    later season marked is_current has a resolved team for the same player,
    team must stay null -- _current_team's projection must never leak in."""

    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Historical Player")
        _seed_membership(conn, PLAYER_ID, OTHER_SEASON_ID, None)
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client, season_id=OTHER_SEASON_ID)

    assert response.status_code == 200
    assert response.json()["players"][0]["team"] is None


# --- 11/12. display-name and identifier projection --------------------------


def test_display_name_falls_back_to_given_and_family_name(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, given_name="J", family_name="Smith")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["players"][0]["display_name"] == "J Smith"


def test_display_name_is_null_when_unresolved(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID)
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["players"][0]["display_name"] is None


def test_identifiers_resolve_independently_and_preserve_null(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        _seed_provider_id(conn, PLAYER_ID, "champion_data", "CD_I1023261")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

        _seed_player(conn, JOSH_ID, display_name="Josh Daicos")
        _seed_provider_id(conn, JOSH_ID, "afl", "1321")
        _seed_membership(conn, JOSH_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    by_id = {p["canonical_player_id"]: p["identifiers"] for p in response.json()["players"]}
    assert by_id[PLAYER_ID] == {"afl_player_id": None, "champion_data_player_id": "CD_I1023261"}
    assert by_id[JOSH_ID] == {"afl_player_id": 1321, "champion_data_player_id": None}


def test_identifiers_both_null_when_no_provider_crosswalk_exists(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="No Crosswalk Player")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["players"][0]["identifiers"] == {
        "afl_player_id": None,
        "champion_data_player_id": None,
    }


# --- 13/14. unknown season / empty membership -------------------------------


def test_unknown_season_returns_season_not_found(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = _get(client, season_id=999999)

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "season_not_found", "message": "Season not found."}}


def test_valid_season_with_no_memberships_returns_empty_collection(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert response.json() == {"players": [], "limit": 250, "offset": 0}


# --- 15. authentication matches existing v1 consumer API -------------------


def test_missing_api_key_returns_401(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = client.get(f"/api/v1/seasons/{CURRENT_SEASON_ID}/players")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


def test_invalid_api_key_returns_401(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = _get(client, headers={"x-api-key": "wrong-key"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


def test_ordinary_api_key_is_sufficient_without_advanced_read(tmp_path, monkeypatch):
    """No dependency on advanced-read: ordinary v1 consumer access suffices,
    matching GET /api/v1/players/{canonical_player_id}. The API key fixture
    carries no api_key_capabilities rows at all, so this proves the route
    never gates on a capability the way GET .../player-stats?advanced=true
    does."""

    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    with sqlite3.connect(db_path) as conn:
        capability_rows = conn.execute("SELECT COUNT(*) FROM api_key_capabilities").fetchone()[0]
    assert capability_rows == 0
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    assert [p["canonical_player_id"] for p in response.json()["players"]] == [PLAYER_ID]


# --- Query-parameter validation ---------------------------------------------


def test_limit_above_maximum_returns_422(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = _get(client, params={"limit": 251})

    assert response.status_code == 422


def test_limit_below_minimum_returns_422(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = _get(client, params={"limit": 0})

    assert response.status_code == 422


def test_negative_offset_returns_422(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_seasons(conn))
    client = _client(db_path, monkeypatch)

    response = _get(client, params={"offset": -1})

    assert response.status_code == 422


# --- Response shape / OpenAPI documentation ---------------------------------


def test_response_shape_and_openapi_documentation(tmp_path, monkeypatch):
    def seed(conn):
        _seed_seasons(conn)
        _seed_player(conn, PLAYER_ID, display_name="Nick Daicos")
        _seed_provider_id(conn, PLAYER_ID, "afl", "5501")
        _seed_membership(conn, PLAYER_ID, CURRENT_SEASON_ID, TEAM_ID)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    response = _get(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"players", "limit", "offset"}
    player = body["players"][0]
    assert set(player.keys()) == {"canonical_player_id", "display_name", "team", "identifiers"}
    assert set(player["team"].keys()) == {"team_id", "name"}
    assert set(player["identifiers"].keys()) == {"afl_player_id", "champion_data_player_id"}

    operation = client.get("/openapi.json").json()["paths"]["/api/v1/seasons/{season_id}/players"]["get"]
    assert {"200", "404", "422"} <= set(operation["responses"])
    assert operation["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApplicationErrorResponse"
    }
