"""Offline API tests for GET /api/v1/matches/{match_id}/commentary (Issue #201).

Fixture shape mirrors the DB-building conventions already used by
tests/test_api_v1_players.py: raw inserts against a fully migrated SQLite
database, hashed API-key fixtures against the real authenticate_api_key
dependency. No test contacts AFL/CFS; everything runs against an isolated
on-disk SQLite fixture.
"""

import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from afl_json.match_commentary import parse_commentary_feed, persist_commentary_feed
from api.routes import router as legacy_router
from api.routes_v1 import router as v1_router
from api_key_security import api_key_prefix, hash_api_key
from db.init_db import create_api_keys_table
from db.migration_runner import migrate_database

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc).isoformat()
API_KEY = "v1-commentary-test-key"

SEASON_ID = 85
MATCH_ID = 9101
MATCH_PROVIDER_ID = "CD_M20260142409"
ROUND_ID = 1
PLAYER_ID = 501
TEAM_ID = 80


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
        "start_time_utc, season_id, scraped_at) VALUES(?,?,?,'West Coast','Hawthorn','Optus Stadium','POSTGAME',?,?,?)",
        (MATCH_ID, MATCH_PROVIDER_ID, ROUND_ID, NOW, SEASON_ID, NOW),
    )
    conn.execute(
        "INSERT INTO canonical_players VALUES(?,?,?,?,?,?)",
        (PLAYER_ID, "Jack Gunston", "Jack", "Gunston", NOW, NOW),
    )
    conn.execute(
        "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) VALUES(?,?,?,?,?)",
        (PLAYER_ID, "champion_data", "CD_I291351", NOW, NOW),
    )
    conn.execute(
        "INSERT INTO afl_teams VALUES(?,'CD_T80','Hawthorn','HAW','Hawks','Hawthorn','Hawthorn','MEN','{}','{}','{}',?)",
        (TEAM_ID, NOW),
    )


def _seed_events(conn, events):
    payload = {"matchId": MATCH_PROVIDER_ID, "lastUpdated": "2026-08-23T12:15:40.217+0000", "commentaryEvent": events}
    feed = parse_commentary_feed(payload, match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID, observed_at=NOW)
    persist_commentary_feed(conn, feed)


def _seed_api_key(conn):
    create_api_keys_table(conn.cursor())
    conn.execute(
        "INSERT INTO api_keys (label, api_key, key_hash, key_prefix, is_active) VALUES (?, NULL, ?, ?, 1)",
        ("v1-commentary-tests", hash_api_key(API_KEY), api_key_prefix(API_KEY)),
    )


def _make_db(tmp_path, seed):
    path = tmp_path / "commentary.db"
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


def _get(client, match_id=MATCH_ID, params=None, headers=None):
    headers = {"x-api-key": API_KEY} if headers is None else headers
    return client.get(f"/api/v1/matches/{match_id}/commentary", params=params, headers=headers)


def test_unknown_match_returns_404(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_base(conn))
    client = _client(db_path, monkeypatch)
    response = _get(client, match_id=999999)
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "match_not_found", "message": "Match not found."}}


def test_valid_match_with_no_commentary_returns_empty_collection(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_base(conn))
    client = _client(db_path, monkeypatch)
    response = _get(client)
    assert response.status_code == 200
    body = response.json()
    assert body["match"]["match_id"] == MATCH_ID
    assert body["match"]["match_provider_id"] == MATCH_PROVIDER_ID
    assert body["events"] == []


def test_missing_api_key_is_rejected(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=lambda conn: _seed_base(conn))
    client = _client(db_path, monkeypatch)
    response = _get(client, headers={})
    assert response.status_code in (401, 403)


def test_events_are_returned_chronologically_oldest_first_with_resolved_identity(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_events(conn, [
            {"comment": "GOAL - Hawks (Jack Gunston)", "periodNumber": 1, "periodSeconds": 59,
             "playerId": "CD_I291351", "teamId": "CD_T80", "scoreEvent": True},
            {"comment": "Q1 is now underway.", "periodNumber": 1, "periodSeconds": 0,
             "playerId": None, "teamId": None, "scoreEvent": False},
        ])

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    response = _get(client)
    assert response.status_code == 200
    events = response.json()["events"]
    assert [e["comment"] for e in events] == ["Q1 is now underway.", "GOAL - Hawks (Jack Gunston)"]

    goal = events[1]
    assert goal["score_event"] is True
    assert goal["player"] == {"id": PLAYER_ID, "name": "Jack Gunston", "provider_id": "CD_I291351"}
    assert goal["team"] == {"id": TEAM_ID, "name": "Hawthorn", "provider_id": "CD_T80"}
    assert goal["possible_edit_of_event_id"] is None

    quarter_start = events[0]
    assert quarter_start["player"] is None
    assert quarter_start["team"] is None


def test_null_player_and_team_render_as_null_never_guessed(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_events(conn, [
            {"comment": "BEHIND - Eagles (Rushed)", "periodNumber": 2, "periodSeconds": 107,
             "playerId": None, "teamId": "CD_T150", "scoreEvent": True},
        ])

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    response = _get(client)
    event = response.json()["events"][0]
    assert event["player"] is None
    # teamId CD_T150 has no seeded crosswalk in this test -> unresolved, not guessed.
    assert event["team"] is None


def test_event_with_null_comment_does_not_break_the_response(tmp_path, monkeypatch):
    """A source event that omits comment persists with comment=null; the
    response model must render it as null rather than raising a validation
    error for every unfiltered request against this match."""
    def seed(conn):
        _seed_base(conn)
        _seed_events(conn, [
            {"periodNumber": 1, "periodSeconds": 5, "playerId": None, "teamId": None, "scoreEvent": False},
        ])

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    response = _get(client)
    assert response.status_code == 200
    assert response.json()["events"][0]["comment"] is None


def test_score_events_only_filter(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_events(conn, [
            {"comment": "GOAL - Hawks (Jack Gunston)", "periodNumber": 1, "periodSeconds": 59,
             "playerId": "CD_I291351", "teamId": "CD_T80", "scoreEvent": True},
            {"comment": "Some narrative.", "periodNumber": 1, "periodSeconds": 100,
             "playerId": None, "teamId": None, "scoreEvent": False},
        ])

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    response = _get(client, params={"score_events_only": "true"})
    events = response.json()["events"]
    assert len(events) == 1
    assert events[0]["score_event"] is True


def test_period_and_player_and_team_filters(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_events(conn, [
            {"comment": "GOAL - Hawks (Jack Gunston)", "periodNumber": 1, "periodSeconds": 59,
             "playerId": "CD_I291351", "teamId": "CD_T80", "scoreEvent": True},
            {"comment": "Q2 is now underway.", "periodNumber": 2, "periodSeconds": 0,
             "playerId": None, "teamId": None, "scoreEvent": False},
        ])

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)

    by_period = _get(client, params={"period": 2}).json()["events"]
    assert len(by_period) == 1 and by_period[0]["period_number"] == 2

    by_player = _get(client, params={"player_id": PLAYER_ID}).json()["events"]
    assert len(by_player) == 1 and by_player[0]["comment"] == "GOAL - Hawks (Jack Gunston)"

    by_team = _get(client, params={"team_id": TEAM_ID}).json()["events"]
    assert len(by_team) == 1 and by_team[0]["comment"] == "GOAL - Hawks (Jack Gunston)"


def test_same_slot_scoring_outcome_change_exposes_both_events_with_link(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_events(conn, [
            {"comment": "GOAL - Hawks (Jack Gunston)", "periodNumber": 3, "periodSeconds": 839,
             "playerId": "CD_I291351", "teamId": "CD_T80", "scoreEvent": True},
        ])
        # A second, distinct poll where the published outcome changes at the
        # identical match-clock/player/team/scoreEvent slot (the source feed
        # never states why -- see afl_json.match_commentary module docstring).
        _seed_events(conn, [
            {"comment": "BEHIND - Hawks (Jack Gunston)", "periodNumber": 3, "periodSeconds": 839,
             "playerId": "CD_I291351", "teamId": "CD_T80", "scoreEvent": True},
            {"comment": "GOAL - Hawks (Jack Gunston)", "periodNumber": 3, "periodSeconds": 839,
             "playerId": "CD_I291351", "teamId": "CD_T80", "scoreEvent": True},
        ])

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    events = _get(client).json()["events"]
    assert [e["comment"] for e in events] == [
        "GOAL - Hawks (Jack Gunston)", "BEHIND - Hawks (Jack Gunston)",
    ]
    assert events[0]["possible_edit_of_event_id"] is None
    assert events[1]["possible_edit_of_event_id"] == events[0]["id"]
