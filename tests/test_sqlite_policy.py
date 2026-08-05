import sqlite3

import pytest

import config
from db import connection as connection_module
from db.connection import (
    SQLITE_POLICY,
    get_db_connection,
    get_read_only_db_connection,
    initialize_database_policy,
    inspect_database_policy,
)


def test_policy_is_idempotent_and_connections_are_configured(tmp_path, monkeypatch):
    path = tmp_path / "policy.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    first = initialize_database_policy(path)
    second = initialize_database_policy(path)
    assert first["journal_mode"] == second["journal_mode"] == "wal"

    with get_db_connection() as conn:
        policy = inspect_database_policy(conn)
        assert policy["busy_timeout_ms"] == SQLITE_POLICY.busy_timeout_ms
        assert policy["foreign_keys"] is True
        assert policy["synchronous"] == 1  # SQLite's numeric value for NORMAL
        assert conn.row_factory is sqlite3.Row
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")

    ro = get_read_only_db_connection()
    try:
        assert inspect_database_policy(ro)["query_only"] is True
        assert ro.row_factory is sqlite3.Row
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO sample DEFAULT VALUES")
    finally:
        ro.close()


def test_read_only_open_does_not_create_database(tmp_path, monkeypatch):
    path = tmp_path / "absent.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    with pytest.raises(FileNotFoundError):
        get_read_only_db_connection()
    assert not path.exists()


def test_ordinary_connection_does_not_establish_persistent_journal_mode(tmp_path, monkeypatch):
    path = tmp_path / "ordinary.db"
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    with get_db_connection() as conn:
        assert inspect_database_policy(conn)["journal_mode"] == "delete"


def test_mandatory_wal_mismatch_fails_visibly(tmp_path, monkeypatch):
    class FakeConnection:
        def execute(self, sql):
            return self
        def fetchone(self):
            return ("delete",)
        def close(self):
            pass
    monkeypatch.setattr(connection_module.sqlite3, "connect", lambda *a, **k: FakeConnection())
    with pytest.raises(connection_module.SQLitePolicyError, match="required journal_mode=wal"):
        initialize_database_policy(tmp_path / "unsupported.db")


def _busy_error(message="database is locked"):
    exc = sqlite3.OperationalError(message)
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return exc


def test_wal_initialisation_retries_transient_lock(tmp_path, monkeypatch):
    path = tmp_path / "transient.db"
    real_connect = connection_module.sqlite3.connect
    attempts = {"count": 0}
    closed = {"count": 0}

    class LockedConnection:
        def execute(self, sql):
            if "journal_mode" in sql:
                raise _busy_error()
            return self
        def close(self):
            closed["count"] += 1

    def connect(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return LockedConnection()
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(connection_module.sqlite3, "connect", connect)

    assert initialize_database_policy(path)["journal_mode"] == "wal"
    assert attempts["count"] == 2
    assert closed["count"] == 1


def test_wal_initialisation_lock_timeout_remains_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(
        connection_module,
        "SQLITE_POLICY",
        connection_module.SQLitePolicy(connect_timeout_seconds=0.01, busy_timeout_ms=10),
    )
    closed = {"count": 0}

    class LockedConnection:
        def execute(self, sql):
            if "journal_mode" in sql:
                raise _busy_error()
            return self
        def close(self):
            closed["count"] += 1

    monkeypatch.setattr(connection_module.sqlite3, "connect", lambda *a, **k: LockedConnection())

    with pytest.raises(connection_module.SQLitePolicyError, match="database remained locked"):
        initialize_database_policy(tmp_path / "still-locked.db")
    assert closed["count"] >= 1


def test_wal_initialisation_does_not_retry_non_lock_errors(tmp_path, monkeypatch):
    attempts = {"count": 0}

    class BrokenConnection:
        def execute(self, sql):
            attempts["count"] += 1
            if "journal_mode" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return self
        def close(self):
            pass

    monkeypatch.setattr(connection_module.sqlite3, "connect", lambda *a, **k: BrokenConnection())

    with pytest.raises(connection_module.SQLitePolicyError, match="disk I/O error"):
        initialize_database_policy(tmp_path / "broken.db")
    assert attempts["count"] == 2
