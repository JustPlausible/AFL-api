"""Admin interface integration with the scheduler health contract (Issue #178)."""
import base64
import importlib

from fastapi.testclient import TestClient

import config


def _auth():
    return {"Authorization": "Basic " + base64.b64encode(b"admin:password").decode()}


def _client(tmp_path, monkeypatch):
    db_path = tmp_path / "afl.db"
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


def _fake_get(jobs_payload=None, health_payload=None, health_status=200):
    def fake_get(url, *a, **k):
        if url.endswith("/scheduler/health"):
            return _FakeResponse(health_payload, status_code=health_status)
        return _FakeResponse(jobs_payload if jobs_payload is not None else [])
    return fake_get


def test_schedule_page_shows_healthy_scheduler(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr("admin.httpx.get", _fake_get(health_payload={
        "state": "healthy", "scheduler_running": True, "database_accessible": True,
        "registry_accessible": True, "job_count": 3, "diagnostics": [], "version": "0.6.0",
    }))

    response = client.get("/schedule", headers=_auth())

    assert response.status_code == 200
    assert "Healthy" in response.text
    assert "3 registered job" in response.text


def test_schedule_page_shows_healthy_with_zero_jobs_distinctly(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr("admin.httpx.get", _fake_get(health_payload={
        "state": "healthy", "scheduler_running": True, "database_accessible": True,
        "registry_accessible": True, "job_count": 0, "diagnostics": [], "version": "0.6.0",
    }))

    response = client.get("/schedule", headers=_auth())

    assert response.status_code == 200
    assert "Healthy — no jobs registered" in response.text
    assert "bg-success" in response.text


def test_schedule_page_shows_starting_state(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr("admin.httpx.get", _fake_get(health_payload={
        "state": "starting", "scheduler_running": False, "database_accessible": True,
        "registry_accessible": True, "job_count": 0, "diagnostics": ["scheduler_not_running"], "version": "0.6.0",
    }))

    response = client.get("/schedule", headers=_auth())

    assert response.status_code == 200
    assert "Starting / Not ready" in response.text
    assert "bg-warning" in response.text


def test_schedule_page_shows_unhealthy_dependency_failure(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr("admin.httpx.get", _fake_get(health_payload={
        "state": "unhealthy", "scheduler_running": True, "database_accessible": False,
        "registry_accessible": False, "job_count": 0,
        "diagnostics": ["database_unavailable", "registry_unreadable"], "version": "0.6.0",
    }, health_status=503))

    response = client.get("/schedule", headers=_auth())

    assert response.status_code == 200
    assert "Unhealthy" in response.text
    assert "database_unavailable" in response.text
    assert "bg-danger" in response.text


def test_schedule_page_shows_unavailable_on_transport_failure(tmp_path, monkeypatch):
    """Connection failure to the health endpoint must render cleanly as Unavailable,
    not be treated as a malformed health response, and must not break the page."""
    admin, client = _client(tmp_path, monkeypatch)

    def fake_get(url, *a, **k):
        if url.endswith("/scheduler/health"):
            raise admin.httpx.ConnectError("connection refused")
        return _FakeResponse([])

    monkeypatch.setattr("admin.httpx.get", fake_get)

    response = client.get("/schedule", headers=_auth())

    assert response.status_code == 200
    assert "Unavailable" in response.text
    assert "could not reach the scheduler health endpoint" in response.text


def test_schedule_page_shows_unavailable_on_malformed_health_body(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr("admin.httpx.get", _fake_get(health_payload={"unexpected": "shape"}))

    response = client.get("/schedule", headers=_auth())

    assert response.status_code == 200
    assert "Unavailable" in response.text


def test_schedule_page_shows_unavailable_when_healthy_state_missing_job_count(tmp_path, monkeypatch):
    """Regression: a recognised `state` alone must not be enough to mark the
    response available -- a missing/wrong-typed required field must fall
    back to Unavailable rather than rendering as a false "healthy"."""
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr("admin.httpx.get", _fake_get(health_payload={
        "state": "healthy", "scheduler_running": True,
        "database_accessible": True, "registry_accessible": True,
        "diagnostics": [], "version": "0.6.0",
        # job_count intentionally omitted
    }))

    response = client.get("/schedule", headers=_auth())

    assert response.status_code == 200
    assert "Unavailable" in response.text
    assert "None registered job" not in response.text


def test_schedule_page_shows_unavailable_when_healthy_state_contradicts_flags(tmp_path, monkeypatch):
    """Regression: `state: healthy` with an unreachable dependency is an
    internally-contradictory body and must not be trusted as healthy."""
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr("admin.httpx.get", _fake_get(health_payload={
        "state": "healthy", "scheduler_running": True,
        "database_accessible": False, "registry_accessible": True,
        "job_count": 0, "diagnostics": [], "version": "0.6.0",
    }))

    response = client.get("/schedule", headers=_auth())

    assert response.status_code == 200
    assert "Unavailable" in response.text


def test_schedule_page_shows_unavailable_on_unexpected_http_status(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr("admin.httpx.get", _fake_get(health_payload={
        "state": "healthy", "scheduler_running": True,
        "database_accessible": True, "registry_accessible": True,
        "job_count": 0, "diagnostics": [], "version": "0.6.0",
    }, health_status=500))

    response = client.get("/schedule", headers=_auth())

    assert response.status_code == 200
    assert "Unavailable" in response.text


def test_is_valid_scheduler_health_accepts_well_formed_contract():
    import admin as admin_module

    assert admin_module._is_valid_scheduler_health({
        "state": "unhealthy", "scheduler_running": True,
        "database_accessible": False, "registry_accessible": True,
        "job_count": 0, "diagnostics": ["database_unavailable"], "version": "0.6.0",
    })


def test_is_valid_scheduler_health_rejects_boolean_job_count():
    import admin as admin_module

    assert not admin_module._is_valid_scheduler_health({
        "state": "healthy", "scheduler_running": True,
        "database_accessible": True, "registry_accessible": True,
        "job_count": True, "diagnostics": [], "version": "0.6.0",
    })


def test_scheduler_health_display_is_independent_of_jobs_error():
    """The jobs-list failure path must not affect the health display mapping."""
    import admin as admin_module

    health_display = admin_module._scheduler_health_display({
        "available": True, "state": "healthy", "job_count": 5,
    })
    assert health_display["label"] == "Healthy"


def test_scheduler_health_display_never_leaks_raw_error_text():
    import admin as admin_module

    display = admin_module._scheduler_health_display({"available": False})
    assert "Traceback" not in display["detail"]
    assert "Exception" not in display["detail"]
