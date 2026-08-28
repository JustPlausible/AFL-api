"""Offline API tests for GET /api/v1/matches/{match_id}/interchanges and
/interchanges/events (Issue #204).

Fixture shape mirrors tests/test_api_v1_commentary.py's conventions: raw
inserts against a fully migrated SQLite database, hashed API-key fixtures
against the real authenticate_api_key dependency. No test contacts AFL/CFS.
"""

import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from afl_json.match_interchange import parse_match_interchange, persist_match_interchange
from api.routes import router as legacy_router
from api.routes_v1 import router as v1_router
from api_key_security import api_key_prefix, hash_api_key
from db.init_db import create_api_keys_table
from db.migration_runner import migrate_database

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc).isoformat()
API_KEY = "v1-interchange-test-key"

SEASON_ID = 85
MATCH_ID = 9201
MATCH_PROVIDER_ID = "CD_M20260142001"
ROUND_ID = 1
HOME_PLAYER_ID = 501
HOME_TEAM_ID = 10


def _seed_base(conn):
    conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}',?)", (NOW,))
    conn.execute(
        "INSERT INTO afl_seasons VALUES(?,'CD_S85',1,'2026','2026',2026,1,1,NULL,NULL,'{}','{}',?)",
        (SEASON_ID, NOW),
    )
    conn.execute(
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, scraped_at) VALUES(?, 'R24', ?, 1, ?)",
        (ROUND_ID, SEASON_ID, NOW),
    )
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, "
        "start_time_utc, season_id, scraped_at) VALUES(?,?,?,'Home Team','Away Team','Venue','LIVE',?,?,?)",
        (MATCH_ID, MATCH_PROVIDER_ID, ROUND_ID, NOW, SEASON_ID, NOW),
    )
    conn.execute(
        "INSERT INTO canonical_players VALUES(?,?,?,?,?,?)",
        (HOME_PLAYER_ID, "Alex Player", "Alex", "Player", NOW, NOW),
    )
    conn.execute(
        "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) VALUES(?,?,?,?,?)",
        (HOME_PLAYER_ID, "champion_data", "CD_I1", NOW, NOW),
    )
    conn.execute(
        "INSERT INTO afl_teams VALUES(?,'CD_T10','Home Team','HT','Home','Home','Home','MEN','{}','{}','{}',?)",
        (HOME_TEAM_ID, NOW),
    )


def _entry(player_id, *, team_id="CD_T10", count=1, bench_reason="ROTATION", tog=100, tob=10, power=3):
    return {
        "teamId": team_id,
        "player": {"playerId": player_id, "playerName": {"givenName": "Alex", "surname": "Player"},
                   "captain": False, "playerJumperNumber": 1},
        "interchangeCount": count, "benchReason": bench_reason,
        "timeOnGround": tog, "timeOnBench": tob, "powerRating": power,
    }


def _seed_interchange(conn, *, home=None, away=None, observed_at=NOW):
    payload = {
        "matchId": MATCH_PROVIDER_ID,
        "homeInterchange": home if home is not None else [],
        "awayInterchange": away if away is not None else [],
        "homeInterchangeCounts": {}, "awayInterchangeCounts": {},
    }
    parsed = parse_match_interchange(
        payload, match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=observed_at,
    )
    persist_match_interchange(conn, parsed)


def _seed_api_key(conn):
    create_api_keys_table(conn.cursor())
    conn.execute(
        "INSERT INTO api_keys (label, api_key, key_hash, key_prefix, is_active) VALUES (?, NULL, ?, ?, 1)",
        ("v1-interchange-tests", hash_api_key(API_KEY), api_key_prefix(API_KEY)),
    )


def _make_db(tmp_path, seed):
    path = tmp_path / "interchange.db"
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


def _get_interchanges(client, match_id=MATCH_ID, params=None, headers=None):
    headers = {"x-api-key": API_KEY} if headers is None else headers
    return client.get(f"/api/v1/matches/{match_id}/interchanges", params=params, headers=headers)


def _get_events(client, match_id=MATCH_ID, params=None, headers=None):
    headers = {"x-api-key": API_KEY} if headers is None else headers
    return client.get(f"/api/v1/matches/{match_id}/interchanges/events", params=params, headers=headers)


def test_unknown_match_returns_404(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_base(conn))
    client = _client(db_path, monkeypatch)
    response = _get_interchanges(client, match_id=999999)
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "match_not_found", "message": "Match not found."}}


def test_events_unknown_match_returns_404(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_base(conn))
    client = _client(db_path, monkeypatch)
    response = _get_events(client, match_id=999999)
    assert response.status_code == 404


def test_valid_match_with_no_interchange_data_returns_empty_collection(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_base(conn))
    client = _client(db_path, monkeypatch)
    response = _get_interchanges(client)
    assert response.status_code == 200
    body = response.json()
    assert body["match"]["match_id"] == MATCH_ID
    assert body["match"]["match_provider_id"] == MATCH_PROVIDER_ID
    assert body["interchanges"] == []


def test_missing_api_key_is_rejected(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_base(conn))
    client = _client(db_path, monkeypatch)
    response = _get_interchanges(client, headers={})
    assert response.status_code in (401, 403)


def test_current_state_response_shape_and_resolved_identity(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_interchange(conn, home=[_entry("CD_I1", count=8, bench_reason="ROTATION", tog=4697, tob=568, power=5)])

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    response = _get_interchanges(client)
    assert response.status_code == 200
    interchanges = response.json()["interchanges"]
    assert len(interchanges) == 1
    entry = interchanges[0]
    assert entry["champion_data_player_id"] == "CD_I1"
    assert entry["canonical_player_id"] == HOME_PLAYER_ID
    assert entry["display_name"] == "Alex Player"
    assert entry["side"] == "home"
    assert entry["team_id"] == HOME_TEAM_ID
    assert entry["champion_data_team_id"] == "CD_T10"
    assert entry["on_bench"] is True
    assert entry["interchange_count"] == 8
    assert entry["bench_reason"] == "ROTATION"
    assert entry["time_on_ground_seconds"] == 4697
    assert entry["time_on_bench_seconds"] == 568
    assert entry["power_rating"] == 5
    assert entry["first_observed_at"]
    assert entry["observed_at"]


def test_player_who_left_the_list_still_returned_with_flag_false(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_interchange(conn, home=[_entry("CD_I1")], observed_at=NOW)
        _seed_interchange(conn, home=[], observed_at="2026-08-24T12:01:00+00:00")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    interchanges = _get_interchanges(client).json()["interchanges"]
    assert len(interchanges) == 1
    assert interchanges[0]["on_bench"] is False


def test_on_bench_only_filter(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_interchange(conn, home=[_entry("CD_I1")], observed_at=NOW)
        _seed_interchange(conn, home=[], observed_at="2026-08-24T12:01:00+00:00")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    filtered = _get_interchanges(client, params={"on_bench_only": "true"}).json()["interchanges"]
    assert filtered == []


def test_side_and_player_id_filters(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_interchange(conn, home=[_entry("CD_I1")], away=[_entry("CD_I2", team_id="CD_T99")])

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    by_side = _get_interchanges(client, params={"side": "away"}).json()["interchanges"]
    assert len(by_side) == 1 and by_side[0]["champion_data_player_id"] == "CD_I2"

    by_player = _get_interchanges(client, params={"player_id": HOME_PLAYER_ID}).json()["interchanges"]
    assert len(by_player) == 1 and by_player[0]["champion_data_player_id"] == "CD_I1"


def test_unresolved_provider_ids_render_null_never_guessed(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_interchange(conn, home=[_entry("CD_I_UNKNOWN", team_id="CD_T_UNKNOWN")])

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    entry = _get_interchanges(client).json()["interchanges"][0]
    assert entry["champion_data_player_id"] == "CD_I_UNKNOWN"
    assert entry["canonical_player_id"] is None
    assert entry["display_name"] is None
    assert entry["team_id"] is None
    assert entry["champion_data_team_id"] == "CD_T_UNKNOWN"


def test_events_returned_chronologically_with_before_after_values(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_interchange(conn, home=[_entry("CD_I1", count=3)], observed_at=NOW)
        _seed_interchange(conn, home=[_entry("CD_I1", count=4)], observed_at="2026-08-24T12:01:00+00:00")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    events = _get_events(client).json()["events"]
    assert [e["event_type"] for e in events] == ["appeared", "interchange_count_changed"]
    changed = events[1]
    assert changed["previous_interchange_count"] == 3
    assert changed["interchange_count"] == 4
    assert changed["canonical_player_id"] == HOME_PLAYER_ID
    assert changed["display_name"] == "Alex Player"


def test_events_valid_match_with_no_transitions_returns_empty_collection(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_base(conn))
    client = _client(db_path, monkeypatch)
    response = _get_events(client)
    assert response.status_code == 200
    assert response.json()["events"] == []


def test_events_filters_by_player_and_event_type(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_interchange(conn, home=[_entry("CD_I1"), _entry("CD_I3")], observed_at=NOW)
        _seed_interchange(conn, home=[_entry("CD_I1", count=2)], observed_at="2026-08-24T12:01:00+00:00")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    by_player = _get_events(client, params={"player_id": HOME_PLAYER_ID}).json()["events"]
    assert all(e["champion_data_player_id"] == "CD_I1" for e in by_player)

    disappeared = _get_events(client, params={"event_type": "disappeared"}).json()["events"]
    assert len(disappeared) == 1
    assert disappeared[0]["champion_data_player_id"] == "CD_I3"
