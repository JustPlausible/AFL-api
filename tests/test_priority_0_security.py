import base64
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
from api.routes import router


def _make_api_key_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            api_key TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )
        """
    )
    conn.execute("INSERT INTO api_keys (label, api_key, is_active) VALUES (?, ?, ?)", ("active", "active-key", 1))
    conn.execute("INSERT INTO api_keys (label, api_key, is_active) VALUES (?, ?, ?)", ("inactive", "inactive-key", 0))
    conn.commit()
    conn.close()


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    db_path = tmp_path / "afl_players.db"
    _make_api_key_db(db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_echo_headers_requires_api_key(api_client):
    response = api_client.get("/api/echo-headers")
    assert response.status_code == 422


def test_echo_headers_redacts_sensitive_authenticated_headers(api_client):
    response = api_client.get(
        "/api/echo-headers",
        headers={
            "x-api-key": "active-key",
            "authorization": "Bearer secret",
            "cookie": "session=secret",
            "x-admin-key": "admin-secret",
            "x-visible": "visible-value",
        },
    )

    assert response.status_code == 200
    headers = response.json()["headers"]
    assert headers["x-api-key"] == "<redacted>"
    assert headers["authorization"] == "<redacted>"
    assert headers["cookie"] == "<redacted>"
    assert headers["x-admin-key"] == "<redacted>"
    assert headers["x-visible"] == "visible-value"


def test_inactive_api_key_is_rejected(api_client):
    response = api_client.get("/api/echo-headers", headers={"x-api-key": "inactive-key"})
    assert response.status_code == 401


def test_invalid_api_key_log_redacts_full_secret(api_client, monkeypatch):
    messages = []
    monkeypatch.setattr(auth, "log", lambda message, level="INFO": messages.append(message))

    response = api_client.get("/api/echo-headers", headers={"x-api-key": "very-secret-invalid-key"})

    assert response.status_code == 401
    assert messages
    assert "very-secret-invalid-key" not in messages[0]
    assert "very-s" in messages[0]
    assert "-key" in messages[0]


def test_admin_app_requires_basic_auth(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")

    import importlib
    import admin

    admin = importlib.reload(admin)
    client = TestClient(admin.app)

    unauthenticated = client.get("/")
    assert unauthenticated.status_code == 401

    token = base64.b64encode(b"admin:password").decode("ascii")
    authenticated = client.get("/", headers={"Authorization": f"Basic {token}"})
    assert authenticated.status_code == 200
