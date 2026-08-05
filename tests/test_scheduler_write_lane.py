import logging
import sqlite3
import threading

import pytest

import config
from db.connection import initialize_database_policy
from scheduler.write_lane import NestedWriteLaneError, SchedulerWriteLane, WriteLaneClosed


def _database(tmp_path, monkeypatch):
    path = tmp_path / "lane.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    initialize_database_policy(path)
    return path


def test_failure_rolls_back_and_next_write_succeeds(tmp_path, monkeypatch):
    _database(tmp_path, monkeypatch)
    lane = SchedulerWriteLane()
    def fail(conn):
        conn.execute("CREATE TABLE rolled_back (id INTEGER)")
        conn.execute("INSERT INTO rolled_back VALUES (1)")
        raise ValueError("boom")
    with pytest.raises(ValueError):
        lane.execute("test.fail", 1, fail)
    lane.execute("test.success", 2, lambda conn: conn.execute("CREATE TABLE ok (id INTEGER)"))


def test_threads_never_overlap_and_lane_drains(tmp_path, monkeypatch):
    _database(tmp_path, monkeypatch)
    lane = SchedulerWriteLane()
    barrier = threading.Barrier(3)
    state = {"active": 0, "maximum": 0}
    state_lock = threading.Lock()
    def worker(number):
        barrier.wait()
        def persist(conn):
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            conn.execute("CREATE TABLE IF NOT EXISTS writes (id INTEGER)")
            conn.execute("INSERT INTO writes VALUES (?)", (number,))
            with state_lock:
                state["active"] -= 1
        lane.execute("test.thread", number, persist)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join()
    assert state["maximum"] == 1
    assert lane.drain(timeout=1)
    with pytest.raises(WriteLaneClosed):
        lane.execute("late", 3, lambda conn: None)


def test_nested_use_is_rejected(tmp_path, monkeypatch):
    _database(tmp_path, monkeypatch)
    lane = SchedulerWriteLane()
    with pytest.raises(NestedWriteLaneError):
        lane.execute("outer", 1, lambda conn: lane.execute("inner", 1, lambda nested: None))


def test_structured_success_and_rollback_diagnostics(tmp_path, monkeypatch, caplog):
    _database(tmp_path, monkeypatch)
    lane = SchedulerWriteLane()
    caplog.set_level(logging.INFO, logger="scheduler.write_lane")
    lane.execute("domain.persist", 7, lambda conn: conn.execute("CREATE TABLE event(id INTEGER)"))
    with pytest.raises(RuntimeError):
        lane.execute("domain.fail", 8, lambda conn: (_ for _ in ()).throw(RuntimeError("bad")))
    fields = [record.scheduler_write for record in caplog.records if hasattr(record, "scheduler_write")]
    assert fields[0].keys() == {"event", "operation", "target_id", "lane_wait_ms",
                               "transaction_ms", "result", "failure_class", "retry",
                               "queued_writers"}
    assert fields[0]["operation"] == "domain.persist" and fields[0]["result"] == "success"
    assert fields[1]["operation"] == "domain.fail" and fields[1]["result"] == "rollback"
    assert fields[1]["failure_class"] == "application" and fields[1]["retry"] is False


def test_drain_times_out_visibly_while_active_then_finishes(tmp_path, monkeypatch, caplog):
    _database(tmp_path, monkeypatch)
    lane = SchedulerWriteLane()
    entered, release = threading.Event(), threading.Event()
    thread = threading.Thread(target=lambda: lane.execute(
        "active", 1, lambda conn: (entered.set(), release.wait())[0]))
    thread.start(); assert entered.wait(1)
    assert lane.drain(timeout=0.01) is False
    with pytest.raises(WriteLaneClosed):
        lane.execute("late", 2, lambda conn: None)
    release.set(); thread.join(1)
    assert lane.drain(timeout=1) is True
    assert any(getattr(record, "scheduler_write", {}).get("result") == "timeout"
               for record in caplog.records)


def test_busy_timeout_is_one_bounded_failure_with_structured_classification(tmp_path, monkeypatch, caplog):
    _database(tmp_path, monkeypatch)
    blocker = sqlite3.connect(config.DB_PATH)
    blocker.execute("BEGIN IMMEDIATE")
    lane = SchedulerWriteLane()
    caplog.set_level(logging.INFO, logger="scheduler.write_lane")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        lane.execute("busy.persist", 9, lambda conn: conn.execute("CREATE TABLE blocked(id INTEGER)"))
    blocker.rollback(); blocker.close()
    lane.execute("recovered.persist", 9, lambda conn: conn.execute("CREATE TABLE recovered(id INTEGER)"))
    busy = next(record.scheduler_write for record in caplog.records
                if getattr(record, "scheduler_write", {}).get("operation") == "busy.persist")
    assert busy["failure_class"] == "sqlite_busy"
    assert busy["result"] == "rollback" and busy["retry"] is False


def test_diagnostic_failure_does_not_replace_callback_result_or_error(tmp_path, monkeypatch):
    _database(tmp_path, monkeypatch)
    lane = SchedulerWriteLane()
    monkeypatch.setattr("scheduler.write_lane.logger.info",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("logger failed")))
    assert lane.execute("logged.success", 1, lambda conn: "result") == "result"
    with pytest.raises(ValueError, match="original"):
        lane.execute("logged.failure", 2,
                     lambda conn: (_ for _ in ()).throw(ValueError("original")))
