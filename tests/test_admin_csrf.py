import base64
import importlib
import re
import sqlite3

from fastapi.testclient import TestClient

import config
from api_key_security import hash_api_key, api_key_prefix
from db.init_db import create_api_keys_table


def _auth(username="admin", password="password"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _client(tmp_path, monkeypatch):
    db_path = tmp_path / "afl_players.db"
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    import admin
    admin = importlib.reload(admin)
    return TestClient(admin.app), db_path


def _token_from(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match, response.text
    return match.group(1)


def _seed_key(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    create_api_keys_table(cur)
    key = "seed-key"
    cur.execute(
        "INSERT INTO api_keys (label, api_key, key_hash, key_prefix, is_active) VALUES (?, NULL, ?, ?, 1)",
        ("seed", hash_api_key(key), api_key_prefix(key)),
    )
    conn.commit()
    conn.close()


def _active_value(db_path):
    conn = sqlite3.connect(db_path)
    value = conn.execute("SELECT is_active FROM api_keys WHERE id = 1").fetchone()[0]
    conn.close()
    return value


def test_admin_mutation_forms_include_csrf_tokens(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    _seed_key(db_path)

    api_keys = client.get("/setup/api-keys", headers=_auth())
    assert api_keys.status_code == 200
    assert api_keys.text.count('name="csrf_token"') >= 3
    assert "/setup/api-keys/new" in api_keys.text
    assert "/setup/api-keys/1/renew" in api_keys.text
    assert "/setup/api-keys/delete/1" in api_keys.text

    clubs = client.get("/setup/clubs-diff", headers=_auth())
    assert clubs.status_code == 200
    assert clubs.text.count('name="csrf_token"') >= 2

    schedule = client.get("/schedule", headers=_auth())
    assert schedule.status_code == 200
    assert 'action="/scheduler/refresh" method="post"' in schedule.text
    assert 'name="csrf_token"' in schedule.text


def test_valid_create_api_key_succeeds(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    token = _token_from(client.get("/setup/api-keys", headers=_auth()))

    response = client.post(
        "/setup/api-keys/new",
        headers=_auth(),
        data={"label": "created", "csrf_token": token},
        follow_redirects=True,
    )

    assert response.status_code == 200
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM api_keys WHERE label = 'created'").fetchone()[0]
    conn.close()
    assert count == 1


def test_missing_invalid_and_malformed_tokens_are_rejected_before_side_effect(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)

    for payload in ({"label": "missing"}, {"label": "invalid", "csrf_token": "invalid-token"}, {"label": "malformed", "csrf_token": "%%%"}):
        response = client.post("/setup/api-keys/new", headers=_auth(), data=payload)
        assert response.status_code in (403, 422)
        if payload.get("csrf_token"):
            assert payload["csrf_token"] not in response.text
        assert "password" not in response.text
        assert "Traceback" not in response.text

    conn = sqlite3.connect(db_path)
    create_api_keys_table(conn.cursor())
    count = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
    conn.close()
    assert count == 0


def test_token_cannot_be_reused_from_incompatible_browser_context(tmp_path, monkeypatch):
    client_a, db_path = _client(tmp_path, monkeypatch)
    token = _token_from(client_a.get("/setup/api-keys", headers=_auth()))
    client_b, _ = _client(tmp_path, monkeypatch)

    response = client_b.post(
        "/setup/api-keys/new",
        headers=_auth(),
        data={"label": "blocked", "csrf_token": token},
    )

    assert response.status_code == 403
    conn = sqlite3.connect(db_path)
    create_api_keys_table(conn.cursor())
    count = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
    conn.close()
    assert count == 0


def test_api_key_toggle_delete_renew_and_read_only_pages_with_valid_csrf(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    _seed_key(db_path)

    read_only = client.get("/setup/api-keys", headers=_auth())
    assert read_only.status_code == 200
    token = _token_from(read_only)

    assert client.post("/setup/api-keys/1/toggle-ajax", headers=_auth(), data={"csrf_token": token}).json() == {"success": True, "new_status": 0}
    assert _active_value(db_path) == 0

    token = _token_from(client.get("/setup/api-keys", headers=_auth()))
    assert client.post("/setup/api-keys/1/renew", headers=_auth(), data={"csrf_token": token}, follow_redirects=True).status_code == 200

    token = _token_from(client.get("/setup/api-keys", headers=_auth()))
    assert client.post("/setup/api-keys/delete/1", headers=_auth(), data={"csrf_token": token}, follow_redirects=True).status_code == 200
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
    conn.close()
    assert count == 0

    assert client.get("/").status_code == 401
    assert client.get("/", headers=_auth()).status_code == 200
    assert client.get("/tables", headers=_auth()).status_code == 200
