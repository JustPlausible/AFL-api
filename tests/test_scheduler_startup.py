import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_STOPPED
from fastapi.testclient import TestClient

import config
from db.migration_runner import migrate_database
from scheduler import registry


def noop(value=None):
    return value


def _db(tmp_path, monkeypatch):
    path = tmp_path / "afl.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    migrate_database(path)
    return path


def test_expired_date_job_is_skipped_during_registration(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    sched = BackgroundScheduler(timezone=timezone.utc)
    run_at = datetime.now(timezone.utc) - timedelta(hours=1)

    added = registry.add_registered_job(
        sched,
        noop,
        job_id="expired_once",
        job_type="general_refresh",
        run_date=run_at,
        args=[],
    )

    assert added is None
    assert sched.get_job("expired_once") is None
    row = registry.registry_rows()[0]
    assert row["job_id"] == "expired_once"
    assert row["status"] == registry.SKIPPED
    assert "scheduled run time is in the past" in row["last_error_summary"]


def test_future_date_job_remains_registered(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    sched = BackgroundScheduler(timezone=timezone.utc)
    run_at = datetime.now(timezone.utc) + timedelta(hours=1)

    added = registry.add_registered_job(
        sched,
        noop,
        job_id="future_once",
        job_type="general_refresh",
        run_date=run_at,
        args=[],
    )

    assert added is not None
    assert sched.get_job("future_once") is not None
    assert registry.registry_rows()[0]["status"] == registry.PENDING


def test_app_lifespan_starts_and_shuts_down_scheduler_once(monkeypatch):
    import scheduler.start as start

    calls = {"register": 0, "migrate": 0}

    class FakeScheduler:
        running = False
        state = STATE_STOPPED

        def start(self):
            time.sleep(0.05)

    monkeypatch.setattr(start, "_jobs_registered", False)
    monkeypatch.setattr(start, "_scheduler_thread", None)
    monkeypatch.setattr(start, "scheduler", FakeScheduler())
    monkeypatch.setattr(start, "migrate_database", lambda: calls.__setitem__("migrate", calls["migrate"] + 1))
    monkeypatch.setattr(start, "establish_instance", lambda: "test-instance")
    monkeypatch.setattr(start, "_recover_interrupted_attempts_startup", lambda: None)
    monkeypatch.setattr(start, "_reconcile_match_windows_startup", lambda: None)
    monkeypatch.setattr(start, "register_all_jobs", lambda: calls.__setitem__("register", calls["register"] + 1))
    monkeypatch.setattr(start, "shutdown_scheduler", lambda wait=True: None)

    with TestClient(start.app) as client:
        response = client.get("/health")
        assert response.status_code in {200, 404}
        start.start_scheduler_for_app()

    assert calls == {"register": 1, "migrate": 1}


def test_duplicate_scheduler_startup_is_ignored(monkeypatch):
    import scheduler.start as start

    calls = {"register": 0, "migrate": 0, "start": 0}

    class FakeScheduler:
        running = False

        def start(self):
            calls["start"] += 1
            time.sleep(0.2)

    monkeypatch.setattr(start, "_jobs_registered", False)
    monkeypatch.setattr(start, "_scheduler_thread", None)
    monkeypatch.setattr(start, "scheduler", FakeScheduler())
    monkeypatch.setattr(start, "migrate_database", lambda: calls.__setitem__("migrate", calls["migrate"] + 1))
    monkeypatch.setattr(start, "establish_instance", lambda: "test-instance")
    monkeypatch.setattr(start, "_recover_interrupted_attempts_startup", lambda: None)
    monkeypatch.setattr(start, "_reconcile_match_windows_startup", lambda: None)
    monkeypatch.setattr(start, "register_all_jobs", lambda: calls.__setitem__("register", calls["register"] + 1))

    start.start_scheduler_for_app()
    start.start_scheduler_for_app()

    assert calls == {"register": 1, "migrate": 1, "start": 1}


def test_direct_module_execution_blocks_and_terminates_cleanly(tmp_path):
    db_path = tmp_path / "afl.db"
    env = os.environ.copy()
    env["DB_PATH"] = str(db_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "scheduler.start"],
        cwd=os.getcwd(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(2)
    assert proc.poll() is None
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=10)

    assert proc.returncode == 0
    combined = stdout + stderr
    assert "cannot schedule new futures after interpreter shutdown" not in combined
    assert "RuntimeError" not in combined
