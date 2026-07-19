import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

import db.connection
from health import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_healthz_returns_ok_without_sensitive_details():
    response = _client().get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_returns_ok_when_database_is_available(tmp_path, monkeypatch):
    db_path = tmp_path / "ready.db"
    conn = sqlite3.connect(db_path)
    conn.close()
    monkeypatch.setattr(db.connection, "DB_PATH", db_path)

    response = _client().get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_returns_503_when_database_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(db.connection, "DB_PATH", tmp_path / "missing.db")

    response = _client().get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
