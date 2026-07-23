from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from db.migration_runner import migrate_database
from db.scrape_runs import (
    STATUS_COMPLETED, STATUS_FAILED, STATUS_RUNNING, TRIGGER_CLI,
    complete_scrape_run, fail_scrape_run, recent_scrape_runs, recover_stale_running_runs,
    sanitize_error_summary, start_scrape_run,
)


def conn(tmp_path):
    db = tmp_path / "audit.db"
    migrate_database(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def test_lifecycle_filters_counts_and_correlation(tmp_path):
    c = conn(tmp_path)
    rid = start_scrape_run("fixture", target_type="round", target_identifier=1, trigger_source=TRIGGER_CLI, correlation_id="fixtures_daily", conn=c)
    row = c.execute("SELECT * FROM scrape_runs WHERE run_id=?", (rid,)).fetchone()
    assert row["status"] == STATUS_RUNNING
    assert row["correlation_id"] == "fixtures_daily"
    complete_scrape_run(rid, rows_read=2, rows_written=2, conn=c)
    row = c.execute("SELECT * FROM scrape_runs WHERE run_id=?", (rid,)).fetchone()
    assert row["status"] == STATUS_COMPLETED
    assert row["finished_at"] is not None
    assert row["duration_ms"] >= 0
    assert row["rows_read"] == 2
    assert recent_scrape_runs(scrape_type="fixture", status=STATUS_COMPLETED, conn=c)[0].run_id == rid
    with pytest.raises(ValueError):
        complete_scrape_run("missing", conn=c)


def test_fail_sanitizes_and_truncates(tmp_path):
    c = conn(tmp_path)
    rid = start_scrape_run("injury", trigger_source=TRIGGER_CLI, conn=c)
    exc = RuntimeError("Authorization: Bearer abc123 Cookie: sid=deadbeef https://x.test/a?api_key=secret&ok=1 password=hunter2 " + "x" * 600)
    fail_scrape_run(rid, exc, conn=c)
    row = c.execute("SELECT * FROM scrape_runs WHERE run_id=?", (rid,)).fetchone()
    assert row["status"] == STATUS_FAILED
    assert row["error_class"] == "RuntimeError"
    assert len(row["error_summary"]) <= 500
    assert "abc123" not in row["error_summary"]
    assert "deadbeef" not in row["error_summary"]
    assert "secret" not in row["error_summary"]
    assert "hunter2" not in row["error_summary"]


def test_recover_stale_running_runs_uses_cutoff(tmp_path):
    c = conn(tmp_path)
    old = start_scrape_run("match", trigger_source=TRIGGER_CLI, conn=c)
    recent = start_scrape_run("match", trigger_source=TRIGGER_CLI, conn=c)
    c.execute("UPDATE scrape_runs SET started_at=? WHERE run_id=?", ((datetime.now(timezone.utc)-timedelta(hours=3)).isoformat(), old))
    c.commit()
    recovered = recover_stale_running_runs(older_than=datetime.now(timezone.utc)-timedelta(hours=1), conn=c)
    assert recovered == 1
    rows = {r["run_id"]: r["status"] for r in c.execute("SELECT run_id,status FROM scrape_runs")}
    assert rows[old] == STATUS_FAILED
    assert rows[recent] == STATUS_RUNNING


def test_migration_is_additive_and_idempotent(tmp_path):
    db = tmp_path / "fresh.db"
    assert migrate_database(db) == ["0001", "0002", "0003", "0004", "0005"]
    assert migrate_database(db) == []
    c = sqlite3.connect(db)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"scrape_runs", "scrape_log", "scrape_summary"} <= tables
