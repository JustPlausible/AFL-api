"""Consumer /api/v1 request telemetry middleware (Issue #205).

Covers the single shared instrumentation point in ``analytics/middleware.py``:
success and error status recording, the internal (non-secret) API-key ID
being attached when a route authenticates, analytics failure never breaking
an otherwise-valid response, and that no API-key secret or Authorization
header value is ever persisted -- only the small set of privacy-minimal
columns the contract defines.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import config
from analytics import record
from analytics.middleware import analytics_http_middleware
from api_key_security import api_key_prefix, hash_api_key
from auth import AuthenticatedCredential, authenticate_api_key
from db.init_db import create_api_keys_table
from db.migration_runner import migrate_database

API_KEY = "analytics-mw-test-key"
AUTH_HEADER_VALUE = "Bearer super-secret-token-should-never-be-stored"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(analytics_http_middleware)

    @app.get("/api/v1/seasons")
    def seasons():
        return {"seasons": []}

    @app.get("/api/v1/matches/{match_id}")
    def match_detail(match_id: int):
        if match_id == 999:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="not found")
        return {"match_id": match_id}

    @app.get("/api/v1/matches/{match_id}/player-stats")
    def player_stats(match_id: int, credential: AuthenticatedCredential = Depends(authenticate_api_key)):
        return {"match_id": match_id, "label": credential.label}

    @app.get("/not-versioned")
    def unversioned():
        return {"ok": True}

    return app


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "consumer.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    create_api_keys_table(conn.cursor())
    conn.execute(
        "INSERT INTO api_keys (label, api_key, key_hash, key_prefix, is_active) VALUES (?, NULL, ?, ?, 1)",
        ("analytics-mw-tests", hash_api_key(API_KEY), api_key_prefix(API_KEY)),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(path))
    monkeypatch.setattr(config, "AFL_ANALYTICS_CONSUMER_ENABLED", True)
    return path


def _requests(db_path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM analytics_consumer_requests ORDER BY id").fetchall()
    finally:
        conn.close()


def test_successful_request_records_route_status_and_duration(db_path):
    client = TestClient(_build_app())
    response = client.get("/api/v1/seasons")
    assert response.status_code == 200
    assert record.wait_until_idle()
    rows = _requests(db_path)
    assert len(rows) == 1
    assert rows[0]["route"] == "/api/v1/seasons"
    assert rows[0]["status_code"] == 200
    assert rows[0]["duration_ms"] >= 0


def test_error_response_is_recorded(db_path):
    client = TestClient(_build_app())
    response = client.get("/api/v1/matches/999")
    assert response.status_code == 404
    assert record.wait_until_idle()
    rows = _requests(db_path)
    assert rows[-1]["route"] == "/api/v1/matches/{match_id}"
    assert rows[-1]["status_code"] == 404


def test_unmatched_route_is_recorded_without_crashing(db_path):
    client = TestClient(_build_app())
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert record.wait_until_idle()
    rows = _requests(db_path)
    assert rows[-1]["route"] == "unmatched"


def test_routes_outside_api_v1_are_not_recorded(db_path):
    client = TestClient(_build_app())
    client.get("/not-versioned")
    assert record.wait_until_idle()
    assert _requests(db_path) == []


def test_authenticated_request_records_internal_api_key_id_not_secret(db_path):
    client = TestClient(_build_app())
    response = client.get("/api/v1/matches/5/player-stats", headers={
        "x-api-key": API_KEY, "authorization": AUTH_HEADER_VALUE,
    })
    assert response.status_code == 200
    assert record.wait_until_idle()
    rows = _requests(db_path)
    row = rows[-1]
    assert row["api_key_id"] == 1
    # Every column value across the row must never contain the raw API key
    # or the Authorization header -- only the internal integer ID above.
    for value in tuple(row):
        assert value != API_KEY
        if isinstance(value, str):
            assert API_KEY not in value
            assert AUTH_HEADER_VALUE not in value
            assert "Bearer" not in value


def test_analytics_failure_never_breaks_a_valid_response(db_path, monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("simulated analytics failure")

    monkeypatch.setattr("analytics.middleware.record_consumer_request", _boom)
    client = TestClient(_build_app())
    response = client.get("/api/v1/seasons")
    assert response.status_code == 200
    assert response.json() == {"seasons": []}
