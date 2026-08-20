"""Tests for the read-only scheduler health/status contract (Issue #178)."""
from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING, STATE_STOPPED
from fastapi.testclient import TestClient

import config
from db.migration_runner import migrate_database
from scheduler import api
from scheduler.scheduled_tasks import scheduler


def _db(tmp_path, monkeypatch):
    path = tmp_path / "afl.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    migrate_database(path)
    return path


def _client() -> TestClient:
    return TestClient(api.app)


def test_healthy_scheduler_reports_healthy_with_no_diagnostics(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "state", STATE_RUNNING)

    response = _client().get("/scheduler/health")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "healthy"
    assert body["scheduler_running"] is True
    assert body["database_accessible"] is True
    assert body["registry_accessible"] is True
    assert body["diagnostics"] == []
    assert isinstance(body["job_count"], int)
    assert body["version"]


def test_healthy_scheduler_with_empty_job_registry_is_still_healthy(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "state", STATE_RUNNING)

    class _EmptyScheduler:
        running = True

        def get_jobs(self):
            return []

    monkeypatch.setattr(api, "scheduler", _EmptyScheduler())

    response = _client().get("/scheduler/health")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "healthy"
    assert body["job_count"] == 0
    assert body["diagnostics"] == []


def test_scheduler_not_yet_started_reports_starting(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "state", STATE_STOPPED)

    response = _client().get("/scheduler/health")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "starting"
    assert body["scheduler_running"] is False
    assert body["database_accessible"] is True
    assert body["registry_accessible"] is True
    assert "scheduler_not_running" in body["diagnostics"]


def test_paused_scheduler_still_counts_as_running(tmp_path, monkeypatch):
    """APScheduler considers PAUSED distinct from STOPPED; only STOPPED means not-yet-started here."""
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "state", STATE_PAUSED)

    response = _client().get("/scheduler/health")

    body = response.json()
    assert body["scheduler_running"] is True
    assert body["state"] == "healthy"


def test_missing_database_reports_unhealthy_with_sanitized_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "missing.db"))
    monkeypatch.setattr(scheduler, "state", STATE_RUNNING)

    response = _client().get("/scheduler/health")

    assert response.status_code == 503
    body = response.json()
    assert body["state"] == "unhealthy"
    assert body["database_accessible"] is False
    assert body["registry_accessible"] is False
    assert body["diagnostics"] == ["database_unavailable", "registry_unreadable"]


def test_missing_registry_table_reports_unhealthy_dependency_failure(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "state", STATE_RUNNING)

    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE scheduler_job_registry")
    conn.commit()
    conn.close()

    response = _client().get("/scheduler/health")

    assert response.status_code == 503
    body = response.json()
    assert body["state"] == "unhealthy"
    assert body["database_accessible"] is True
    assert body["registry_accessible"] is False
    assert body["diagnostics"] == ["registry_unreadable"]


def test_diagnostics_never_expose_raw_paths_credentials_or_exception_text(tmp_path, monkeypatch):
    secret_dir = tmp_path / "super-secret-operational-path"
    monkeypatch.setattr(config, "DB_PATH", str(secret_dir / "afl.db"))
    monkeypatch.setattr(scheduler, "state", STATE_RUNNING)

    response = _client().get("/scheduler/health")

    body = response.json()
    raw_text = response.text
    assert "super-secret-operational-path" not in raw_text
    assert "Traceback" not in raw_text
    assert "sqlite3" not in raw_text.lower()
    for code in body["diagnostics"]:
        assert code in {"database_unavailable", "registry_unreadable", "scheduler_not_running"}
    assert set(body.keys()) == {
        "state", "scheduler_running", "database_accessible",
        "registry_accessible", "job_count", "diagnostics", "version",
    }


def test_jobs_endpoint_still_behaves_as_before(tmp_path, monkeypatch):
    """Regression guard: adding /scheduler/health must not change /scheduler/jobs.

    Uses a minimal fake scheduler rather than the shared process-wide
    ``scheduler`` singleton: that singleton is never started in the test
    process, and APScheduler's own pending-job objects lack a computed
    ``next_run_time`` until ``.start()`` runs -- a pre-existing characteristic
    of ``list_jobs()`` unrelated to this change. The behavioural contract this
    guards is that ``/scheduler/jobs`` keeps returning a 200 JSON list.
    """
    _db(tmp_path, monkeypatch)

    class _NoJobsScheduler:
        def get_jobs(self):
            return []

    monkeypatch.setattr(api, "scheduler", _NoJobsScheduler())

    response = _client().get("/scheduler/jobs")

    assert response.status_code == 200
    assert response.json() == []
