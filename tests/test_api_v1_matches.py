"""Offline contract tests for canonical v1 match navigation."""

from api.routes_v1 import Match, MatchesResponse
from tests.test_api_v1_player_stats import (
    API_KEY,
    MATCH_ID,
    ROUND_ID,
    SEASON_ID,
    _client,
    _make_db,
    _seed_match,
    _seed_player,
    _seed_stat_row,
)


def _seed_matches(conn):
    _seed_match(conn)
    conn.execute("UPDATE afl_teams SET name='Canonical Home' WHERE afl_id=10")
    conn.execute("UPDATE afl_teams SET name='Canonical Away' WHERE afl_id=11")
    conn.execute(
        "UPDATE matches SET score_home=88, score_away=NULL, home_json='raw-home', "
        "away_json='raw-away', venue_json='raw-venue', metadata_json='raw-metadata', "
        "source_json='raw-source' WHERE match_id=?",
        (MATCH_ID,),
    )
    rows = [
        (8200, "CD_PRIVATE_0", ROUND_ID, "2026-08-01T01:00:00Z", 10, 11),
        (8201, "CD_PRIVATE_1", ROUND_ID, "2026-08-01T01:00:00Z", 10, 999),
        (8202, "CD_PRIVATE_2", ROUND_ID, None, None, 11),
        (7000, "CD_OTHER", 202, "2026-01-01T00:00:00Z", 10, 11),
    ]
    conn.execute(
        "INSERT INTO rounds(round_id,round_label,season_id,competition_id) "
        "VALUES(202,'Other',?,1)",
        (SEASON_ID,),
    )
    conn.execute(
        "INSERT INTO rounds(round_id,round_label,season_id,competition_id) "
        "VALUES(203,'Empty',?,1)",
        (SEASON_ID,),
    )
    conn.executemany(
        "INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,"
        "status,start_time_utc,score_home,score_away,season_id,home_team_id,away_team_id) "
        "VALUES(?,?,?,'untrusted home','untrusted away','untrusted venue','SCHEDULED',"
        "?,NULL,NULL,?,?,?)",
        [(a, b, c, d, SEASON_ID, e, f) for a, b, c, d, e, f in rows],
    )


def _matches_client(tmp_path, monkeypatch):
    return _client(_make_db(tmp_path, _seed_matches), monkeypatch)


def test_round_match_listing_requires_authentication(tmp_path, monkeypatch):
    response = _matches_client(tmp_path, monkeypatch).get(
        f"/api/v1/rounds/{ROUND_ID}/matches"
    )
    assert response.status_code == 401


def test_listing_is_scoped_ordered_typed_and_safe(tmp_path, monkeypatch):
    response = _matches_client(tmp_path, monkeypatch).get(
        f"/api/v1/rounds/{ROUND_ID}/matches", headers={"x-api-key": API_KEY}
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["match_id"] for item in body["matches"]] == [8200, 8201, MATCH_ID, 8202]
    assert all(item["round_id"] == ROUND_ID for item in body["matches"])
    assert body["matches"][-1]["start_time_utc"] is None
    assert MatchesResponse.model_validate(body)

    detail = next(item for item in body["matches"] if item["match_id"] == MATCH_ID)
    assert detail["home_team"] == {"team_id": 10, "name": "Canonical Home"}
    assert detail["away_team"] == {"team_id": 11, "name": "Canonical Away"}
    assert detail["score_home"] == 88
    assert detail["score_away"] is None
    unresolved = next(item for item in body["matches"] if item["match_id"] == 8201)
    assert unresolved["away_team"] is None
    for internal in (
        "match_provider_id", "providerId", "home_json", "away_json", "venue_json",
        "metadata_json", "source_json", "untrusted", "CD_PRIVATE",
    ):
        assert internal not in response.text


def test_unknown_and_empty_rounds_are_distinct(tmp_path, monkeypatch):
    client = _matches_client(tmp_path, monkeypatch)
    empty = client.get("/api/v1/rounds/203/matches", headers={"x-api-key": API_KEY})
    assert empty.status_code == 200
    assert empty.json() == {"matches": []}

    missing = client.get("/api/v1/rounds/999999/matches", headers={"x-api-key": API_KEY})
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {"code": "round_not_found", "message": "Round not found."}
    }


def test_match_detail_and_structured_not_found(tmp_path, monkeypatch):
    client = _matches_client(tmp_path, monkeypatch)
    response = client.get(f"/api/v1/matches/{MATCH_ID}", headers={"x-api-key": API_KEY})
    assert response.status_code == 200
    assert Match.model_validate(response.json()).match_id == MATCH_ID
    assert response.json()["home_team"]["name"] == "Canonical Home"

    missing = client.get("/api/v1/matches/999999", headers={"x-api-key": API_KEY})
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {"code": "match_not_found", "message": "Match not found."}
    }


def test_listed_match_id_navigates_to_existing_player_stats(tmp_path, monkeypatch):
    def seed(conn):
        _seed_matches(conn)
        _seed_player(conn, 1, champion_data_id="CD_P1", afl_id=101)
        _seed_stat_row(conn, champion_data_player_id="CD_P1", side="home", snapshot_authority=1,
                       canonical_player_id=1)

    client = _client(_make_db(tmp_path, seed), monkeypatch)
    listing = client.get(
        f"/api/v1/rounds/{ROUND_ID}/matches", headers={"x-api-key": API_KEY}
    ).json()
    match_id = next(item["match_id"] for item in listing["matches"] if item["match_id"] == MATCH_ID)
    stats = client.get(
        f"/api/v1/matches/{match_id}/player-stats", headers={"x-api-key": API_KEY}
    )
    assert stats.status_code == 200
    assert stats.json()["match"]["match_id"] == match_id


def test_openapi_documents_match_contracts(tmp_path, monkeypatch):
    paths = _matches_client(tmp_path, monkeypatch).get("/openapi.json").json()["paths"]
    listing = paths["/api/v1/rounds/{round_id}/matches"]["get"]
    detail = paths["/api/v1/matches/{match_id}"]["get"]
    assert listing["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/MatchesResponse"
    }
    assert detail["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Match"
    }
    for operation in (listing, detail):
        assert operation["responses"]["404"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ApplicationErrorResponse"
        }
