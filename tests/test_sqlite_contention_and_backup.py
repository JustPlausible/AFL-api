"""Deterministic cross-process WAL and backup checks for issue #131."""

import multiprocessing
import sqlite3

import config
from db.connection import get_db_connection, get_read_only_db_connection, initialize_database_policy


def _hold_writer(path, ready, release):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO values_table VALUES (1, 'held')")
    ready.set()
    release.wait()
    conn.commit()
    conn.close()


def _waiting_writer(path, attempted, completed, results):
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.set_trace_callback(lambda sql: attempted.set() if sql.startswith("INSERT") else None)
    try:
        conn.execute("INSERT INTO values_table VALUES (2, 'waited')")
        conn.commit()
        results.put("committed")
    except Exception as exc:
        results.put(f"error:{type(exc).__name__}:{exc}")
    finally:
        conn.close()
        completed.set()


def _initialize_at_startup(path, start, results):
    start.wait()
    try:
        from db.connection import initialize_database_policy
        results.put(initialize_database_policy(path)["journal_mode"])
    except Exception as exc:
        results.put(f"error:{type(exc).__name__}:{exc}")


def _migrate_at_startup(path, start, results):
    start.wait()
    try:
        from db.migration_runner import migrate_database
        results.put(("ok", len(migrate_database(path))))
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def test_wal_readers_and_waiting_process_writer(tmp_path, monkeypatch):
    path = tmp_path / "contention.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    initialize_database_policy(path)
    with get_db_connection() as conn:
        conn.execute("CREATE TABLE values_table(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO values_table VALUES (0, 'committed')")

    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    attempted, completed, results = ctx.Event(), ctx.Event(), ctx.Queue()
    holder = ctx.Process(target=_hold_writer, args=(str(path), ready, release))
    holder.start()
    assert ready.wait(10)

    # WAL permits both normal API-style and URI/query-only reporting reads.
    with get_db_connection() as reader:
        assert reader.execute("SELECT value FROM values_table WHERE id=0").fetchone()[0] == "committed"
    with get_read_only_db_connection() as report:
        assert report.execute("SELECT value FROM values_table WHERE id=0").fetchone()[0] == "committed"

    waiter = ctx.Process(target=_waiting_writer,
                         args=(str(path), attempted, completed, results))
    waiter.start()
    # The trace callback proves sqlite3 has begun stepping the INSERT while the
    # first process still owns the write transaction.
    assert attempted.wait(10)
    assert not completed.is_set()
    release.set()
    assert results.get(timeout=10) == "committed"
    holder.join(10); waiter.join(10)
    assert holder.exitcode == waiter.exitcode == 0
    with get_db_connection() as conn:
        assert [tuple(row) for row in conn.execute("SELECT id,value FROM values_table ORDER BY id")] == [
            (0, "committed"), (1, "held"), (2, "waited")]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_sqlite_backup_api_restores_wal_database(tmp_path, monkeypatch):
    source, restored = tmp_path / "source.db", tmp_path / "restored.db"
    monkeypatch.setattr(config, "DB_PATH", str(source))
    initialize_database_policy(source)
    with get_db_connection() as conn:
        conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO sample VALUES (1, 'preserved')")
    with get_db_connection() as src, sqlite3.connect(restored) as destination:
        src.backup(destination)
    with sqlite3.connect(restored) as check:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("SELECT value FROM sample").fetchone()[0] == "preserved"
    assert initialize_database_policy(restored)["journal_mode"] == "wal"


def test_concurrent_startup_policy_initialisation_is_idempotent(tmp_path):
    path = tmp_path / "startup.db"
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_initialize_at_startup,
                             args=(str(path), start, results)) for _ in range(2)]
    for process in processes: process.start()
    start.set()
    values = [results.get(timeout=15) for _ in processes]
    for process in processes: process.join(15)
    assert values == ["wal", "wal"]
    assert all(process.exitcode == 0 for process in processes)


def test_concurrent_full_migration_has_one_owner_and_one_idempotent_observer(tmp_path):
    path = tmp_path / "concurrent-migration.db"
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_migrate_at_startup,
                             args=(str(path), start, results)) for _ in range(2)]
    for process in processes: process.start()
    start.set()
    values = sorted(results.get(timeout=30) for _ in processes)
    for process in processes: process.join(30)
    assert values == [("ok", 0), ("ok", 12)]
    assert all(process.exitcode == 0 for process in processes)
