import base64
import importlib

from fastapi.testclient import TestClient

import config


def _auth():
    token = base64.b64encode(b"admin:password").decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "admin.db"))
    import admin
    admin = importlib.reload(admin)
    return admin, TestClient(admin.app)


def test_primary_admin_navigation_is_consistent(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin.httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(admin.httpx.ConnectError("offline")))

    for path in ("/", "/schedule", "/tables", "/logs", "/setup"):
        response = client.get(path, headers=_auth())
        assert response.status_code == 200
        for label in ("Overview", "Scheduling", "Data", "Logs &amp; diagnostics", "System"):
            assert label in response.text


def test_missing_log_is_diagnostic_empty_state(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setitem(admin.LOG_FILES, "Player Stats", "definitely-missing-player-stats.log")

    response = client.get("/logs?log=Player%20Stats", headers=_auth())

    assert response.status_code == 200
    assert "No log entries are available yet." in response.text
    assert "logs/definitely-missing-player-stats.log" in response.text
    assert "admin interface has failed" in response.text


def test_unknown_log_keeps_admin_layout_and_returns_bad_request(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)

    response = client.get("/logs?log=not-a-log", headers=_auth())

    assert response.status_code == 400
    assert "Unknown log selection." in response.text
    assert "AFL API" in response.text
