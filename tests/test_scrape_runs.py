from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import pytest
import config
from db import scrape_runs

from db.migration_runner import migrate_database
from db.scrape_runs import (
    STATUS_COMPLETED, STATUS_FAILED, STATUS_PARTIAL, STATUS_RUNNING, TRIGGER_CLI,
    complete_scrape_run, fail_scrape_run, recent_scrape_runs, recover_stale_running_runs,
    record_scrape_decision, sanitize_error_summary, start_scrape_run,
    audited_scrape_run, scheduler_job_context,
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


def test_concurrent_scheduler_audit_context_is_thread_local(tmp_path, monkeypatch):
    db = tmp_path / "concurrent-audit.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    migrate_database(db)
    barrier = threading.Barrier(2)

    def audit(job_id):
        with scheduler_job_context(job_id):
            with audited_scrape_run("threaded", target_identifier=job_id):
                barrier.wait()

    threads = [threading.Thread(target=audit, args=(job_id,))
               for job_id in ("job-a", "job-b")]
    for thread in threads: thread.start()
    for thread in threads: thread.join(5)
    assert all(not thread.is_alive() for thread in threads)
    with sqlite3.connect(db) as check:
        assert check.execute(
            "SELECT correlation_id,status FROM scrape_runs ORDER BY correlation_id"
        ).fetchall() == [("job-a", STATUS_COMPLETED), ("job-b", STATUS_COMPLETED)]


def test_audit_finalisation_failure_does_not_hide_collector_error(tmp_path, monkeypatch):
    c = conn(tmp_path)
    monkeypatch.setattr(scrape_runs, "fail_scrape_run",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit unavailable")))
    with pytest.raises(ValueError, match="collector failed"):
        with audited_scrape_run("failure", conn=c):
            raise ValueError("collector failed")


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


def test_partial_success_is_a_distinct_audit_outcome(tmp_path):
    c = conn(tmp_path)
    rid = start_scrape_run("injury", trigger_source=TRIGGER_CLI, conn=c)

    complete_scrape_run(rid, rows_read=3, rows_written=1, partial=True, conn=c)

    row = c.execute("SELECT status,rows_read,rows_written FROM scrape_runs WHERE run_id=?", (rid,)).fetchone()
    assert tuple(row) == (STATUS_PARTIAL, 3, 1)


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
    assert migrate_database(db) == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0011", "0012", "0013", "0014", "0015", "0016", "0017", "0018"]
    assert migrate_database(db) == []
    c = sqlite3.connect(db)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"scrape_runs", "scrape_log", "scrape_summary"} <= tables


def test_decision_records_are_redacted_and_queryable(tmp_path):
    c = conn(tmp_path)
    run_id = record_scrape_decision(
        "afl_season_sync_decision", target_type="match", target_identifier=8001,
        reason_code="scheduled", decision_class="safe", correlation_id="season-1",
        canonical_match_id=8001, provider_match_id="CD_M1", round_identifier=1,
        diagnostic_summary="token=secret Authorization: Bearer hidden",
        trigger_source=TRIGGER_CLI, conn=c,
    )

    row = c.execute("SELECT * FROM scrape_runs WHERE run_id=?", (run_id,)).fetchone()
    assert (row["status"], row["rows_read"], row["rows_written"]) == ("completed", 0, 0)
    assert "secret" not in row["diagnostic_summary"]
    assert "hidden" not in row["diagnostic_summary"]
    assert len(recent_scrape_runs(correlation_id="season-1", conn=c)) == 1
    assert len(recent_scrape_runs(target_identifier=8001, conn=c)) == 1
    assert len(recent_scrape_runs(reason_code="scheduled", conn=c)) == 1
    assert recent_scrape_runs(reason_code="missing_provider_identity", conn=c) == []
