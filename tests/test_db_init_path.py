import os
import sqlite3
import subprocess
import sys

import config
from db.init_db import create_api_keys_table, init_db


def _create_legacy_api_keys_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            api_key TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )
        """
    )
    conn.execute("INSERT INTO api_keys (label, api_key, is_active) VALUES (?, ?, ?)", ("active", "legacy-active", 1))
    conn.execute("INSERT INTO api_keys (label, api_key, is_active) VALUES (?, ?, ?)", ("inactive", "legacy-inactive", 0))
    conn.commit()
    conn.close()


def _table_columns(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("PRAGMA table_info(api_keys)").fetchall()
    conn.close()
    return {row[1]: row for row in rows}


def test_python_module_init_uses_configured_database_path(tmp_path):
    configured_dir = tmp_path / "configured-data"
    configured_dir.mkdir()
    configured_db = configured_dir / "afl_players.db"
    other_cwd = tmp_path / "other-cwd"
    other_cwd.mkdir()

    env = os.environ.copy()
    env["DB_PATH"] = str(configured_db)
    env["PYTHONPATH"] = os.getcwd()
    result = subprocess.run(
        [sys.executable, "-m", "db.init_db"],
        cwd=other_cwd,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert str(configured_db) in (result.stdout + result.stderr)
    assert configured_db.exists()
    assert not (other_cwd / "data" / "afl_players.db").exists()
    assert "api_keys" in {row[0] for row in sqlite3.connect(configured_db).execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_changing_cwd_does_not_change_migrated_database(tmp_path, monkeypatch):
    configured_dir = tmp_path / "configured-data"
    configured_dir.mkdir()
    configured_db = configured_dir / "afl_players.db"
    _create_legacy_api_keys_db(configured_db)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr(config, "DB_PATH", str(configured_db))
    monkeypatch.chdir(cwd)

    init_db()

    conn = sqlite3.connect(configured_db)
    rows = conn.execute("SELECT api_key, key_hash, key_prefix, is_active FROM api_keys ORDER BY id").fetchall()
    conn.close()
    assert [row[0] for row in rows] == [None, None]
    assert all(str(row[1]).startswith("sha256:") for row in rows)
    assert [row[3] for row in rows] == [1, 0]
    assert not (cwd / "data" / "afl_players.db").exists()


def test_existing_legacy_api_keys_table_is_upgraded_at_configured_path(tmp_path, monkeypatch):
    configured_db = tmp_path / "afl_players.db"
    _create_legacy_api_keys_db(configured_db)
    monkeypatch.setattr(config, "DB_PATH", str(configured_db))

    init_db()

    columns = _table_columns(configured_db)
    assert columns["api_key"][3] == 0
    assert "key_hash" in columns
    assert "key_prefix" in columns


def test_api_key_migration_is_idempotent(tmp_path, monkeypatch):
    configured_db = tmp_path / "afl_players.db"
    _create_legacy_api_keys_db(configured_db)
    monkeypatch.setattr(config, "DB_PATH", str(configured_db))

    init_db()
    conn = sqlite3.connect(configured_db)
    first_rows = conn.execute("SELECT id, label, api_key, key_hash, key_prefix, is_active FROM api_keys ORDER BY id").fetchall()
    conn.close()

    init_db()
    conn = sqlite3.connect(configured_db)
    second_rows = conn.execute("SELECT id, label, api_key, key_hash, key_prefix, is_active FROM api_keys ORDER BY id").fetchall()
    conn.close()

    assert second_rows == first_rows


def test_init_fails_when_configured_parent_is_invalid(tmp_path, monkeypatch):
    missing_parent = tmp_path / "missing" / "data"
    monkeypatch.setattr(config, "DB_PATH", str(missing_parent / "afl_players.db"))

    try:
        init_db()
    except FileNotFoundError as exc:
        assert str(missing_parent) in str(exc)
    else:
        raise AssertionError("init_db should fail when the configured database parent is missing")
