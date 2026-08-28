"""Offline API tests for GET /api/v1/matches/{match_id}/rosters (Issue #219).

Fixture shape mirrors tests/test_api_v1_interchange.py's conventions: raw
inserts against a fully migrated SQLite database, hashed API-key fixtures
against the real authenticate_api_key dependency, persistence exercised
through afl_json.rosters.persist_match_rosters (never hand-crafted SQL for
the tables under test). No test contacts AFL/CFS.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from afl_json.rosters import MatchRosterCollector, persist_match_rosters
from api.routes import router as legacy_router
from api.routes_v1 import router as v1_router
from api_key_security import api_key_prefix, hash_api_key
from db.init_db import create_api_keys_table
from db.migration_runner import migrate_database

NOW = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc).isoformat()
API_KEY = "v1-rosters-test-key"
FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"

SEASON_ID = 85
ROUND_ID = 1
ROUND_PROVIDER_ID = "CD_R18"
MATCH_ID = 100
MATCH_PROVIDER_ID = "CD_M100"
HOME_TEAM_ID = 1
HOME_PROVIDER_ID = "CD_T1"
AWAY_TEAM_ID = 2
AWAY_PROVIDER_ID = "CD_T2"
HOME_PLAYER_ID = 501


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def _seed_base(conn):
    conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}',?)", (NOW,))
    conn.execute(
        "INSERT INTO afl_seasons VALUES(?,'CD_S85',1,'2026','2026',2026,1,1,NULL,NULL,'{}','{}',?)",
        (SEASON_ID, NOW),
    )
    conn.execute(
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, provider_id) "
        "VALUES(?,'R18',?,1,?)", (ROUND_ID, SEASON_ID, ROUND_PROVIDER_ID),
    )
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, "
        "start_time_utc, season_id, scraped_at) VALUES(?,?,?,'Cats','Dogs','Venue','SCHEDULED',?,?,?)",
        (MATCH_ID, MATCH_PROVIDER_ID, ROUND_ID, NOW, SEASON_ID, NOW),
    )
    conn.executemany(
        "INSERT INTO afl_teams VALUES(?,?,?,?,?,?,?,'MEN','{}','{}','{}',?)",
        [
            (HOME_TEAM_ID, HOME_PROVIDER_ID, "Cats", "CAT", "Cats", "Cats", "Cats", NOW),
            (AWAY_TEAM_ID, AWAY_PROVIDER_ID, "Dogs", "DOG", "Dogs", "Dogs", "Dogs", NOW),
        ],
    )
    conn.execute(
        "INSERT INTO canonical_players VALUES(?,?,?,?,?,?)",
        (HOME_PLAYER_ID, "Ada Able", "Ada", "Able", NOW, NOW),
    )
    conn.execute(
        "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) "
        "VALUES(?,?,?,?,?)", (HOME_PLAYER_ID, "champion_data", "CD_I1", NOW, NOW),
    )


def _seed_roster(conn, fixture_name="match_rosters_available.json", observed_at=NOW):
    class _Response:
        def __init__(self, data):
            self.data = data

    class _Client:
        def __init__(self, payload):
            self.payload = payload

        def get(self, name, path_parameters=None):
            return _Response(self.payload)

    result = MatchRosterCollector(_Client(_fixture(fixture_name))).collect(ROUND_PROVIDER_ID)
    persist_match_rosters(conn, result, observed_at=observed_at)


def _seed_api_key(conn):
    create_api_keys_table(conn.cursor())
    conn.execute(
        "INSERT INTO api_keys (label, api_key, key_hash, key_prefix, is_active) VALUES (?, NULL, ?, ?, 1)",
        ("v1-rosters-tests", hash_api_key(API_KEY), api_key_prefix(API_KEY)),
    )


def _make_db(tmp_path, seed):
    path = tmp_path / "rosters.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
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


def _get_rosters(client, match_id=MATCH_ID, headers=None):
    headers = {"x-api-key": API_KEY} if headers is None else headers
    return client.get(f"/api/v1/matches/{match_id}/rosters", headers=headers)


def test_unknown_match_returns_404(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=_seed_base)
    client = _client(db_path, monkeypatch)
    response = _get_rosters(client, match_id=999999)
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "match_not_found", "message": "Match not found."}}


def test_missing_api_key_is_rejected(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=_seed_base)
    client = _client(db_path, monkeypatch)
    response = _get_rosters(client, headers={})
    assert response.status_code in (401, 403)


def test_valid_match_with_no_roster_yet_returns_null_sides(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, seed=_seed_base)
    client = _client(db_path, monkeypatch)
    response = _get_rosters(client)
    assert response.status_code == 200
    body = response.json()
    assert body["match"]["match_id"] == MATCH_ID
    assert body["match"]["match_provider_id"] == MATCH_PROVIDER_ID
    assert body["home_team"] is None
    assert body["away_team"] is None
    assert body["metadata"]["source_updated_at"] is None
    assert body["metadata"]["match_status_at_observation"] is None


def test_published_roster_response_shape_and_resolved_identity(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_roster(conn)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    body = _get_rosters(client).json()

    assert body["metadata"]["match_status_at_observation"] == "PUBLISHED"
    assert body["metadata"]["source_updated_at"] == "2026-07-25T08:30:00Z"

    home = body["home_team"]
    assert home["team"] == {"team_id": HOME_TEAM_ID, "name": "Cats"}
    assert home["champion_data_team_id"] == HOME_PROVIDER_ID
    assert home["team_status"] == "CONFIRMED"

    home_selections = {entry["player"]["champion_data_player_id"]: entry for entry in home["selections"]}
    assert set(home_selections) == {"CD_I1", "CD_I2"}
    ada = home_selections["CD_I1"]
    assert ada["player"]["canonical_player_id"] == HOME_PLAYER_ID
    assert ada["player"]["display_name"] == "Ada Able"
    assert ada["position"] == "FORWARDS"
    assert ada["jumper_number"] == 7
    assert ada["captain"] is True

    bea = home_selections["CD_I2"]
    assert bea["player"]["canonical_player_id"] is None  # no crosswalk seeded for CD_I2
    assert bea["position"] == "INTERCHANGE"


def test_selections_and_context_are_kept_as_separate_collections(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_roster(conn)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    home = _get_rosters(client).json()["home_team"]

    selection_ids = {entry["player"]["champion_data_player_id"] for entry in home["selections"]}
    ins_ids = {entry["player"]["champion_data_player_id"] for entry in home["context"]["ins"]}
    outs_ids = {entry["player"]["champion_data_player_id"] for entry in home["context"]["outs"]}

    assert ins_ids == {"CD_I1"}
    assert home["context"]["ins"][0]["reason"] == "Selected"
    assert outs_ids == {"CD_I4"}
    assert home["context"]["outs"][0]["reason"] == "Managed"
    assert home["context"]["late_changes"] == []
    assert home["context"]["club_debuts"] == []
    assert home["context"]["milestones"] == []
    # CD_I1 is a selection *and* a change/context record -- distinct
    # collections, never merged into one lineup-membership list.
    assert "CD_I1" in selection_ids
    assert "CD_I1" in ins_ids


def test_response_never_implies_selection_is_participation(tmp_path, monkeypatch):
    """The endpoint's own documented contract (not a runtime-checkable field)
    is that a selection is never claimed as participation evidence -- assert
    the response carries no participation/stats-shaped field at all (no
    'played', 'stats', 'disposals', etc.), keeping this resource distinct
    from GET /api/v1/matches/{match_id}/player-stats."""
    def seed(conn):
        _seed_base(conn)
        _seed_roster(conn)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    body = _get_rosters(client).json()
    home_selection = body["home_team"]["selections"][0]
    assert set(home_selection.keys()) == {"player", "position", "jumper_number", "captain"}


def test_unresolved_team_and_player_render_null_never_guessed(tmp_path, monkeypatch):
    def seed(conn):
        conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}',?)", (NOW,))
        conn.execute(
            "INSERT INTO afl_seasons VALUES(?,'CD_S85',1,'2026','2026',2026,1,1,NULL,NULL,'{}','{}',?)",
            (SEASON_ID, NOW),
        )
        conn.execute(
            "INSERT INTO rounds(round_id, round_label, season_id, competition_id, provider_id) "
            "VALUES(?,'R18',?,1,?)", (ROUND_ID, SEASON_ID, ROUND_PROVIDER_ID),
        )
        conn.execute(
            "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, status, "
            "season_id, scraped_at) VALUES(?,?,?,'Cats','Dogs','SCHEDULED',?,?)",
            (MATCH_ID, MATCH_PROVIDER_ID, ROUND_ID, SEASON_ID, NOW),
        )
        # Deliberately no afl_teams/player_provider_ids rows -- nothing resolves.
        _seed_roster(conn)

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    home = _get_rosters(client).json()["home_team"]
    assert home["team"] is None
    assert home["champion_data_team_id"] == HOME_PROVIDER_ID
    for entry in home["selections"]:
        assert entry["player"]["canonical_player_id"] is None
        assert entry["player"]["champion_data_player_id"]


def test_later_pre_match_update_is_reflected_and_prior_state_not_duplicated(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _seed_roster(conn, "match_rosters_available.json", observed_at=NOW)
        _seed_roster(conn, "match_rosters_changed.json", observed_at="2026-07-25T09:00:00+00:00")

    db_path = _make_db(tmp_path, seed=seed)
    client = _client(db_path, monkeypatch)
    body = _get_rosters(client).json()
    home_players = {entry["player"]["champion_data_player_id"] for entry in body["home_team"]["selections"]}
    # CD_I2 was selected before the later fixture but is absent from it.
    assert home_players == {"CD_I1", "CD_I6"}
    assert body["metadata"]["source_updated_at"] == "2026-07-25T09:00:00Z"
