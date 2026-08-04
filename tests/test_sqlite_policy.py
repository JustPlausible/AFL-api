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
