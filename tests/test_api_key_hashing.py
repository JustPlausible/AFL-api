import base64
import importlib
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
import config
from api.routes import router
from api_key_security import hash_api_key, api_key_prefix
from db.init_db import create_api_keys_table


def _auth_header():
    token = base64.b64encode(b"admin:password").decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _csrf_token(client):
    response = client.get("/setup/api-keys", headers=_auth_header())
    return response.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]


def _make_db(path):
    conn = sqlite3.connect(path)
    create_api_keys_table(conn.cursor())
    conn.execute(
        "INSERT INTO api_keys (label, api_key, key_hash, key_prefix, is_active) VALUES (?, NULL, ?, ?, ?)",
        ("active", hash_api_key("active-key"), api_key_prefix("active-key"), 1),
    )
    conn.execute(
        "INSERT INTO api_keys (label, api_key, key_hash, key_prefix, is_active) VALUES (?, NULL, ?, ?, ?)",
        ("inactive", hash_api_key("inactive-key"), api_key_prefix("inactive-key"), 0),
    )
    conn.commit()
    conn.close()


def _api_client(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_valid_hashed_api_key_authenticates(tmp_path, monkeypatch):
    db_path = tmp_path / "afl_players.db"
    _make_db(db_path)

    response = _api_client(db_path, monkeypatch).get("/api/echo-headers", headers={"x-api-key": "active-key"})

    assert response.status_code == 200


def test_invalid_hashed_api_key_is_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "afl_players.db"
    _make_db(db_path)

    response = _api_client(db_path, monkeypatch).get("/api/echo-headers", headers={"x-api-key": "wrong-key"})

    assert response.status_code == 401


def test_inactive_hashed_api_key_is_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "afl_players.db"
    _make_db(db_path)

    response = _api_client(db_path, monkeypatch).get("/api/echo-headers", headers={"x-api-key": "inactive-key"})

    assert response.status_code == 401


def test_plaintext_migration_preserves_active_and_inactive_keys(tmp_path, monkeypatch):
    db_path = tmp_path / "afl_players.db"
    conn = sqlite3.connect(db_path)
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
    conn.execute("INSERT INTO api_keys (label, api_key, is_active) VALUES (?, ?, ?)", ("active", "legacy-active", 1))
    conn.execute("INSERT INTO api_keys (label, api_key, is_active) VALUES (?, ?, ?)", ("inactive", "legacy-inactive", 0))
    create_api_keys_table(conn.cursor())
    conn.commit()
    rows = conn.execute("SELECT label, api_key, key_hash, key_prefix, is_active FROM api_keys ORDER BY label").fetchall()
    conn.close()

    assert all(row[1] is None for row in rows)
    assert rows[0][4] == 1
    assert rows[1][4] == 0
    client = _api_client(db_path, monkeypatch)
    assert client.get("/api/echo-headers", headers={"x-api-key": "legacy-active"}).status_code == 200
    assert client.get("/api/echo-headers", headers={"x-api-key": "legacy-inactive"}).status_code == 401


def test_admin_created_key_is_shown_once_and_not_stored_in_sqlite(tmp_path, monkeypatch):
    db_path = tmp_path / "afl_players.db"
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    import admin
    admin = importlib.reload(admin)
    monkeypatch.setattr(config, "DB_PATH", str(db_path))

    client = TestClient(admin.app)
    response = client.post("/setup/api-keys/new", data={"label": "created", "csrf_token": _csrf_token(client)}, headers=_auth_header(), follow_redirects=True)

    assert response.status_code == 200
    marker = "Copy this API key now. It will not be shown again:"
    assert marker in response.text
    full_key = response.text.split("<code>", 1)[1].split("</code>", 1)[0]
    conn = sqlite3.connect(db_path)
    values = [str(row[0]) for row in conn.execute("SELECT COALESCE(api_key, ''), key_hash, key_prefix FROM api_keys")]
    conn.close()
    assert full_key not in "\n".join(values)
    assert full_key not in client.get("/setup/api-keys", headers=_auth_header()).text


def test_renewed_key_replaces_old_key(tmp_path, monkeypatch):
    db_path = tmp_path / "afl_players.db"
    _make_db(db_path)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    import admin
    admin = importlib.reload(admin)
    monkeypatch.setattr(config, "DB_PATH", str(db_path))

    client = TestClient(admin.app)
    response = client.post("/setup/api-keys/1/renew", data={"csrf_token": _csrf_token(client)}, headers=_auth_header(), follow_redirects=True)

    assert response.status_code == 200
    renewed_key = response.text.split("<code>", 1)[1].split("</code>", 1)[0]
    client = _api_client(db_path, monkeypatch)
    assert client.get("/api/echo-headers", headers={"x-api-key": renewed_key}).status_code == 200
    assert client.get("/api/echo-headers", headers={"x-api-key": "active-key"}).status_code == 401
