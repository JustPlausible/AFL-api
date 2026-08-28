"""Admin route/rendering tests for the operations/data-health dashboard (Issue #225)."""
from __future__ import annotations

import base64
import importlib
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import config
from db.migration_runner import migrate_database


NOW = datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)


def _auth():
    return {"Authorization": "Basic " + base64.b64encode(b"admin:password").decode()}


def _client(tmp_path, monkeypatch, *, seed=None):
    db_path = tmp_path / "afl.db"
    migrate_database(db_path)
    if seed is not None:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        seed(conn)
        conn.close()
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    import admin
    admin = importlib.reload(admin)
    return admin, TestClient(admin.app)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _healthy_scheduler_get(url, *a, **k):
    if url.endswith("/scheduler/health"):
        return _FakeResponse({
            "state": "healthy", "scheduler_running": True, "database_accessible": True,
            "registry_accessible": True, "job_count": 2, "diagnostics": [], "version": "0.7.1",
        })
    return _FakeResponse([])


def _seed_season(conn, *, is_current=1, current_round_number=1):
    now = NOW.isoformat()
    conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}',?)", (now,))
    conn.execute(
        "INSERT INTO afl_seasons VALUES(85,'CD_S85',1,'2026','2026',2026,?,?,NULL,NULL,'{}','{}',?)",
        (is_current, current_round_number, now),
    )
    for team, provider in ((10, "CD_T1"), (11, "CD_T2")):
        conn.execute(
            "INSERT INTO afl_teams VALUES(?,?,85,?,?,?,?,?,?, '{}','{}','{}',?)",
            (team, provider, provider, provider, provider, provider, provider, "AFL", now),
        )
        conn.execute("INSERT INTO afl_team_seasons VALUES(85,?,?,?)", (team, now, now))
    conn.execute(
        "INSERT INTO rounds(round_id,round_label,season_id,competition_id,provider_id,round_number) "
        "VALUES(101,'Round 1',85,1,'CD_R1',1)"
    )
    conn.commit()


def test_operations_route_requires_auth(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    response = client.get("/operations")
    assert response.status_code == 401


def test_operations_appears_in_primary_navigation(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin.httpx, "get", _healthy_scheduler_get)

    for path in ("/", "/operations", "/schedule"):
        response = client.get(path, headers=_auth())
        assert response.status_code == 200
        assert "Operations" in response.text


def test_operations_dashboard_renders_healthy_state(tmp_path, monkeypatch):
    def seed(conn):
        _seed_season(conn)
        past = (NOW - timedelta(days=2)).isoformat()
        conn.execute(
            "INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,status,"
            "start_time_utc,season_id,home_team_id,away_team_id) "
            "VALUES(8001,'CD_M1',101,'A','B','MCG','CONCLUDED',?,85,10,11)",
            (past,),
        )
        conn.commit()

    admin, client = _client(tmp_path, monkeypatch, seed=seed)
    monkeypatch.setattr(admin.httpx, "get", _healthy_scheduler_get)

    response = client.get("/operations", headers=_auth())

    assert response.status_code == 200
    assert "System overview" in response.text
    assert "Dataset health" in response.text
    assert "2026" in response.text


def test_operations_dashboard_renders_attention_for_failed_scheduler_job(tmp_path, monkeypatch):
    def seed(conn):
        _seed_season(conn)
        conn.execute(
            "INSERT INTO scheduler_job_registry(job_id,job_type,status,last_error_summary,updated_at) "
            "VALUES('fixtures_daily','fixture','failed','upstream 500',?)",
            (NOW.isoformat(),),
        )
        conn.commit()

    admin, client = _client(tmp_path, monkeypatch, seed=seed)
    monkeypatch.setattr(admin.httpx, "get", _healthy_scheduler_get)

    response = client.get("/operations", headers=_auth())

    assert response.status_code == 200
    assert "scheduler.job_type_failed:fixture" in response.text
    assert "text-bg-danger" in response.text or "bg-danger" in response.text


def test_operations_dashboard_classifies_upcoming_round_distinctly(tmp_path, monkeypatch):
    def seed(conn):
        _seed_season(conn)
        future = (NOW + timedelta(days=5)).isoformat()
        conn.execute(
            "INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,status,"
            "start_time_utc,season_id,home_team_id,away_team_id) "
            "VALUES(8001,'CD_M1',101,'A','B','MCG','SCHEDULED',?,85,10,11)",
            (future,),
        )
        conn.commit()

    admin, client = _client(tmp_path, monkeypatch, seed=seed)
    monkeypatch.setattr(admin.httpx, "get", _healthy_scheduler_get)

    response = client.get("/operations", headers=_auth())

    assert response.status_code == 200
    assert "Upcoming" in response.text
    assert "not yet expected" in response.text


def test_operations_dashboard_renders_without_a_current_season(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin.httpx, "get", _healthy_scheduler_get)

    response = client.get("/operations", headers=_auth())

    assert response.status_code == 200
    assert "no season is marked current" in response.text.lower()


def test_operations_dashboard_shows_scheduler_unavailable(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)

    def fake_get(url, *a, **k):
        if url.endswith("/scheduler/health"):
            raise admin.httpx.ConnectError("connection refused")
        return _FakeResponse([])

    monkeypatch.setattr(admin.httpx, "get", fake_get)

    response = client.get("/operations", headers=_auth())

    assert response.status_code == 200
    assert "Unavailable" in response.text


def test_operations_dashboard_links_to_season_review(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch, seed=_seed_season)
    monkeypatch.setattr(admin.httpx, "get", _healthy_scheduler_get)

    response = client.get("/operations", headers=_auth())

    assert response.status_code == 200
    assert "/season-review?season=2026" in response.text


def test_operations_dashboard_renders_controlled_error_when_database_missing(tmp_path, monkeypatch):
    """A database that cannot be opened must render a controlled 503 state, not crash (Issue #225 review)."""
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "does-not-exist.db"))
    import admin
    admin = importlib.reload(admin)
    client = TestClient(admin.app)
    monkeypatch.setattr(admin.httpx, "get", _healthy_scheduler_get)

    response = client.get("/operations", headers=_auth())

    assert response.status_code == 503
    assert "Database unavailable" in response.text


def test_overview_page_links_to_operations(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)

    response = client.get("/", headers=_auth())

    assert response.status_code == 200
    assert '/operations' in response.text
