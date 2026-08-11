"""Offline contract tests for canonical v1 round navigation."""

from api.routes_v1 import Round, RoundsResponse
from tests.test_api_v1_player_stats import API_KEY, NOW, SEASON_ID, _client, _make_db


def _seed_rounds(conn):
    conn.execute(
        "UPDATE afl_teams SET name='Canonical home', abbreviation='HOME' WHERE afl_id=10"
    )
    values = [
        (103, "Round 2", SEASON_ID, 2, "R2", "2026-03-20T08:00:00Z", None, "[]"),
        (102, "Opening Round", SEASON_ID, 0, "OR", "2026-03-06T08:00:00Z", "2026-03-09T12:00:00Z",
         '[{"id":10,"providerId":"CD_T1","name":"untrusted provider name"},{"id":10}]'),
        (104, "Unknown round", SEASON_ID, None, None, None, None, None),
        (201, "Other season", 84, 1, "R1", None, None, "[]"),
        (105, "Round 4", SEASON_ID, 4, "R4", None, None,
         '[{"providerId":"CD_T1"},"provider-shaped",{"id":true}]'),
        (106, "Round 3", SEASON_ID, 3, "R3", None, None,
         '[{"id":10},{"providerId":"unresolvable"}]'),
    ]
    conn.executemany(
        "INSERT INTO rounds(round_id,round_label,season_id,competition_id,round_number,"
        "abbreviation,start_time,end_time,byes_json) VALUES(?,?,?,1,?,?,?,?,?)",
        values,
    )
    conn.execute(
        "INSERT INTO afl_seasons VALUES(83,'CD_S83',1,'2024','2024',2024,0,NULL,"
        "NULL,NULL,'{}','{}',?)",
        (NOW,),
    )


def _rounds_client(tmp_path, monkeypatch):
    return _client(_make_db(tmp_path, _seed_rounds), monkeypatch)


def test_season_rounds_require_authentication(tmp_path, monkeypatch):
    response = _rounds_client(tmp_path, monkeypatch).get(
        f"/api/v1/seasons/{SEASON_ID}/rounds"
    )
    assert response.status_code == 401


def test_season_rounds_are_scoped_ordered_typed_and_safe(tmp_path, monkeypatch):
    response = _rounds_client(tmp_path, monkeypatch).get(
        f"/api/v1/seasons/{SEASON_ID}/rounds", headers={"x-api-key": API_KEY}
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["round_id"] for item in body["rounds"]] == [102, 101, 103, 106, 105, 104]
    assert body["rounds"][0]["round_number"] == 0
    assert body["rounds"][0]["byes"] == [
        {"team_id": 10, "name": "Canonical home", "abbreviation": "HOME"}
    ]
    # The baseline fixture's round 101 has unavailable bye data.
    assert body["rounds"][1]["byes"] is None
    assert body["rounds"][2]["byes"] == []
    # A mixed valid/malformed array and an entirely malformed array are uncertain.
    assert body["rounds"][3]["byes"] is None
    assert body["rounds"][4]["byes"] is None
    assert body["rounds"][-1]["round_number"] is None
    assert all(item["season_id"] == SEASON_ID for item in body["rounds"])
    assert RoundsResponse.model_validate(body)
    for internal in ("metadata_json", "source_json", "byes_json", "providerId"):
        assert internal not in response.text


def test_round_detail_and_structured_not_found(tmp_path, monkeypatch):
    client = _rounds_client(tmp_path, monkeypatch)
    response = client.get("/api/v1/rounds/102", headers={"x-api-key": API_KEY})
    assert response.status_code == 200
    assert Round.model_validate(response.json()).round_id == 102
    assert response.json()["byes"][0]["name"] == "Canonical home"

    missing = client.get("/api/v1/rounds/999999", headers={"x-api-key": API_KEY})
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {"code": "round_not_found", "message": "Round not found."}
    }


def test_valid_empty_season_and_unknown_season_are_distinct(tmp_path, monkeypatch):
    client = _rounds_client(tmp_path, monkeypatch)

    empty = client.get("/api/v1/seasons/83/rounds", headers={"x-api-key": API_KEY})
    assert empty.status_code == 200
    assert empty.json() == {"rounds": []}

    missing = client.get("/api/v1/seasons/999999/rounds", headers={"x-api-key": API_KEY})
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {"code": "season_not_found", "message": "Season not found."}
    }


def test_openapi_documents_round_contracts(tmp_path, monkeypatch):
    paths = _rounds_client(tmp_path, monkeypatch).get("/openapi.json").json()["paths"]
    listing = paths["/api/v1/seasons/{season_id}/rounds"]["get"]
    detail = paths["/api/v1/rounds/{round_id}"]["get"]
    assert listing["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RoundsResponse"
    }
    assert detail["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Round"
    }
    assert detail["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApplicationErrorResponse"
    }
    assert listing["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApplicationErrorResponse"
    }
