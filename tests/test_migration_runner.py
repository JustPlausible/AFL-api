import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from db.migration_runner import BASELINE_TABLES, MigrationError, classify_existing_database, discover_migrations, migrate_database


def make_v030_db(path: Path, plaintext_key: str | None = None):
    """Build representative old DB independently of new migrations."""
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute("CREATE TABLE api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, api_key TEXT UNIQUE, key_hash TEXT UNIQUE, key_prefix TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, is_active INTEGER DEFAULT 1)")
    if plaintext_key:
        c.execute("INSERT INTO api_keys(label, api_key) VALUES('legacy', ?)", (plaintext_key,))
    c.execute("CREATE TABLE clubs (code TEXT PRIMARY KEY, name TEXT, slug TEXT, website TEXT, squad_url TEXT, aliases TEXT)")
    c.execute("INSERT INTO clubs(code, name) VALUES('ADE', 'Adelaide')")
    c.execute("CREATE TABLE players (afl_id INTEGER PRIMARY KEY, full_name TEXT, first_name TEXT, last_name TEXT, nickname TEXT, formatted_nickname TEXT, formatted_last_name TEXT, club TEXT, guernsey INTEGER, position TEXT, club_profile_url TEXT, image_url TEXT, club_player_id INTEGER, afl_url TEXT, champion_data_id TEXT, last_updated TEXT)")
    c.execute("INSERT INTO players(afl_id, full_name) VALUES(1, 'One Player')")
    c.execute("CREATE TABLE rounds (round_id INTEGER PRIMARY KEY, round_label TEXT, season_id INTEGER, competition_id INTEGER, scraped_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE matches (match_id INTEGER PRIMARY KEY, match_provider_id TEXT, round_id INTEGER NOT NULL, home_team TEXT, away_team TEXT, venue TEXT, status TEXT, start_time_utc TEXT, score_home INTEGER, score_away INTEGER, scraped_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE injuries (afl_id INTEGER NOT NULL, club TEXT NOT NULL, player_name TEXT NOT NULL, injury TEXT, return_info TEXT, updated TEXT, first_updated TEXT, source TEXT, scraped_at TEXT DEFAULT CURRENT_TIMESTAMP, current INTEGER DEFAULT 1, UNIQUE(afl_id, updated))")
    c.execute("CREATE TABLE lineups (round_number INTEGER NOT NULL, match_id TEXT NOT NULL, afl_id INTEGER NOT NULL, first_name TEXT, surname TEXT, team TEXT, position_group TEXT, champion_id TEXT, scraped_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (match_id, afl_id))")
    c.execute("CREATE TABLE player_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER NOT NULL, round_id INTEGER, afl_id INTEGER, champion_id TEXT, player_name TEXT NOT NULL, jumper_number INTEGER, team_code TEXT NOT NULL, af_score INTEGER, goals INTEGER, behinds INTEGER, disposals INTEGER, kicks INTEGER, handballs INTEGER, marks INTEGER, tackles INTEGER, hitouts INTEGER, clearances INTEGER, metres_gained INTEGER, goal_assists INTEGER, time_on_ground_pct REAL, status TEXT CHECK(status IN ('LIVE', 'COMPLETED')) NOT NULL, scraped_at TEXT NOT NULL, UNIQUE(match_id, afl_id))")
    c.execute("CREATE TABLE scrape_log (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER NOT NULL, round_id INTEGER, status TEXT, scraped_at TEXT NOT NULL)")
    c.execute("CREATE TABLE scrape_summary (match_id INTEGER PRIMARY KEY, round_id INTEGER, total_scrapes INTEGER, first_scraped TEXT, last_scraped TEXT, completed_scrape BOOLEAN DEFAULT 0, notes TEXT)")
    conn.commit(); conn.close()


def cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_discovery_order_and_bad_files(tmp_path):
    d = tmp_path / "m"; d.mkdir()
    (d / "0002_b.py").write_text('MIGRATION_ID="0002"\nDESCRIPTION="b"\ndef migrate(conn): pass\n')
    (d / "0001_a.py").write_text('MIGRATION_ID="0001"\nDESCRIPTION="a"\ndef migrate(conn): pass\n')
    assert [m.identifier for m in discover_migrations(d)] == ["0001", "0002"]
    (d / "bad.py").write_text('')
    with pytest.raises(MigrationError, match="Malformed"):
        discover_migrations(d)


def test_duplicate_identifier_rejected(tmp_path):
    d = tmp_path / "m"; d.mkdir()
    (d / "0001_a.py").write_text('MIGRATION_ID="0001"\nDESCRIPTION="a"\ndef migrate(conn): pass\n')
    (d / "0001_b.py").write_text('MIGRATION_ID="0001"\nDESCRIPTION="b"\ndef migrate(conn): pass\n')
    with pytest.raises(MigrationError, match="Duplicate"):
        discover_migrations(d)


def test_fresh_creation_idempotency_records_and_schema(tmp_path):
    db = tmp_path / "fresh.db"
    assert migrate_database(db) == ["0001", "0002", "0003"]
    assert migrate_database(db) == []
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert set(BASELINE_TABLES) | {"schema_migrations"} <= tables
    assert {"source", "scraped_at", "resolved_at"} <= cols(conn, "players")
    assert "match_time_label" in cols(conn, "matches")
    assert any(r[2] for r in conn.execute("PRAGMA index_list(scrape_log)") if r[1] == "idx_scrape_log_match_scraped_at")
    rows = conn.execute("SELECT migration_id, description, checksum, applied_at FROM schema_migrations ORDER BY migration_id").fetchall()
    assert [r[0] for r in rows] == ["0001", "0002", "0003"]
    assert all(r[1] and r[2] and r[3] for r in rows)


def test_checksum_change_detection(tmp_path):
    d = tmp_path / "m"; d.mkdir()
    f = d / "0001_a.py"
    f.write_text('MIGRATION_ID="0001"\nDESCRIPTION="a"\ndef migrate(conn):\n    conn.execute("CREATE TABLE t(id INTEGER)")\n')
    db = tmp_path / "x.db"
    migrate_database(db, d)
    f.write_text('MIGRATION_ID="0001"\nDESCRIPTION="a changed"\ndef migrate(conn):\n    conn.execute("CREATE TABLE t(id INTEGER)")\n')
    with pytest.raises(MigrationError, match="has changed"):
        migrate_database(db, d)


def test_failed_migration_rolls_back_table_and_record_but_keeps_prior(tmp_path):
    d = tmp_path / "m"; d.mkdir()
    (d / "0001_ok.py").write_text('MIGRATION_ID="0001"\nDESCRIPTION="ok"\ndef migrate(conn):\n    conn.execute("CREATE TABLE kept(id INTEGER)")\n')
    (d / "0002_fail.py").write_text('MIGRATION_ID="0002"\nDESCRIPTION="fail"\ndef migrate(conn):\n    conn.execute("CREATE TABLE gone(id INTEGER)")\n    raise RuntimeError("boom")\n')
    db = tmp_path / "x.db"
    with pytest.raises(MigrationError, match="0002"):
        migrate_database(db, d)
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "kept" in tables and "gone" not in tables
    assert conn.execute("SELECT migration_id FROM schema_migrations").fetchall() == [("0001",)]


def test_incompatible_or_partial_database_fails(tmp_path):
    db = tmp_path / "partial.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE clubs (code TEXT PRIMARY KEY)"); conn.commit()
    with pytest.raises(MigrationError, match="Unexpected pre-migration"):
        migrate_database(db)


def test_v030_baseline_preserves_rows_and_upgrades_api_keys(tmp_path):
    db = tmp_path / "old.db"
    make_v030_db(db, "afl_test_plaintext")
    assert migrate_database(db) == ["0002", "0003"]
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT name FROM clubs WHERE code='ADE'").fetchone()[0] == "Adelaide"
    assert conn.execute("SELECT full_name FROM players WHERE afl_id=1").fetchone()[0] == "One Player"
    key = conn.execute("SELECT api_key, key_hash, key_prefix FROM api_keys").fetchone()
    assert key[0] is None and key[1].startswith("sha256:") and key[2] == "afl_test"
    assert migrate_database(db) == []


def test_init_db_and_migrate_cli_from_other_cwd(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    db = tmp_path / "cli.db"
    env = {**os.environ, "DB_PATH": str(db), "PYTHONPATH": str(repo)}
    for module in ["db.init_db", "db.migrate", "db.init_db", "db.migrate"]:
        result = subprocess.run([sys.executable, "-m", module], cwd=tmp_path, env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr + result.stdout
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 3


def test_baseline_classifier_rejects_incomplete_table(tmp_path):
    db = tmp_path / "bad.db"
    make_v030_db(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE players RENAME TO players_old")
    conn.execute("CREATE TABLE players (afl_id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO players SELECT afl_id FROM players_old")
    conn.execute("DROP TABLE players_old")
    conn.commit()
    with pytest.raises(MigrationError, match="players"):
        classify_existing_database(conn)
