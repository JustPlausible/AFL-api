import os
import sqlite3
import subprocess
import sys
import shutil
from pathlib import Path

import pytest

from db.migration_runner import BASELINE_TABLES, MIGRATIONS_DIR, MigrationError, classify_existing_database, discover_migrations, migrate_database


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
    assert migrate_database(db) == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0011", "0012", "0013", "0014", "0015"]
    assert migrate_database(db) == []
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert set(BASELINE_TABLES) | {"schema_migrations"} <= tables
    assert {"source", "scraped_at", "resolved_at"} <= cols(conn, "players")
    assert "match_time_label" in cols(conn, "matches")
    assert any(r[2] for r in conn.execute("PRAGMA index_list(scrape_log)") if r[1] == "idx_scrape_log_match_scraped_at")
    rows = conn.execute("SELECT migration_id, description, checksum, applied_at FROM schema_migrations ORDER BY migration_id").fetchall()
    assert [r[0] for r in rows] == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0011", "0012", "0013", "0014", "0015"]
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
    assert migrate_database(db) == ["0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0011", "0012", "0013", "0014", "0015"]
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT name FROM clubs WHERE code='ADE'").fetchone()[0] == "Adelaide Crows"
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
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 15


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


def test_0014_realistic_upgrade_preserves_rows_schema_and_dependent_objects(tmp_path):
    before = tmp_path / "before"
    before.mkdir()
    for source in MIGRATIONS_DIR.glob("*.py"):
        if source.name == "__init__.py" or source.name.startswith("0014_"):
            continue
        shutil.copy2(source, before / source.name)
    db = tmp_path / "legacy-0013.db"
    migrate_database(db, before)
    conn = sqlite3.connect(db)
    registry_statuses = ("pending", "running", "succeeded", "failed", "skipped")
    for index, status in enumerate(registry_statuses):
        conn.execute(
            """INSERT INTO scheduler_job_registry(job_id,job_type,status,
            attempt_count,last_error_summary,args_json,trigger_type,created_at,updated_at)
            VALUES(?, 'legacy', ?, ?, ?, '[]','date',?,?)""",
            (
                f"job-{status}",
                status,
                index,
                None if index % 2 else f"error-{index}",
                "2026-08-06T00:00:00+00:00",
                "2026-08-06T00:00:00+00:00",
            ),
        )
    for status in ("running", "completed", "partial", "failed"):
        conn.execute(
            """INSERT INTO scrape_runs(run_id,scrape_type,trigger_source,status,
            started_at,rows_read,rows_written,correlation_id,reason_code)
            VALUES(?, 'legacy','scheduler',?,?,NULL,?, ?, NULL)""",
            (
                f"run-{status}",
                status,
                "2026-08-06T00:00:00+00:00",
                None if status == "running" else 1,
                f"corr-{status}",
            ),
        )
    conn.execute(
        "CREATE INDEX custom_registry_type ON scheduler_job_registry(job_type,status)"
    )
    conn.execute("CREATE TABLE recovery_trigger_log(job_id TEXT)")
    conn.execute("""CREATE TRIGGER registry_update_log AFTER UPDATE ON scheduler_job_registry
                    BEGIN INSERT INTO recovery_trigger_log VALUES(NEW.job_id); END""")
    conn.execute(
        "CREATE VIEW running_registry_view AS SELECT job_id FROM scheduler_job_registry WHERE status='running'"
    )
    old_registry_cols = [
        tuple(row) for row in conn.execute("PRAGMA table_info(scheduler_job_registry)")
    ]
    old_scrape_cols = [
        tuple(row) for row in conn.execute("PRAGMA table_info(scrape_runs)")
    ]
    registry_names = ",".join(row[1] for row in old_registry_cols)
    scrape_names = ",".join(row[1] for row in old_scrape_cols)
    registry_before = conn.execute(f"SELECT {registry_names} FROM scheduler_job_registry ORDER BY job_id").fetchall()
    scrapes_before = conn.execute(f"SELECT {scrape_names} FROM scrape_runs ORDER BY run_id").fetchall()
    registry_fks = conn.execute("PRAGMA foreign_key_list(scheduler_job_registry)").fetchall()
    scrape_fks = conn.execute("PRAGMA foreign_key_list(scrape_runs)").fetchall()
    conn.commit()
    conn.close()

    assert migrate_database(db) == ["0014"]
    assert migrate_database(db) == []
    conn = sqlite3.connect(db)
    assert conn.execute(f"SELECT {registry_names} FROM scheduler_job_registry ORDER BY job_id").fetchall() == registry_before
    assert conn.execute(f"SELECT {scrape_names} FROM scrape_runs ORDER BY run_id").fetchall() == scrapes_before
    assert conn.execute("PRAGMA foreign_key_list(scheduler_job_registry)").fetchall() == registry_fks
    assert conn.execute("PRAGMA foreign_key_list(scrape_runs)").fetchall() == scrape_fks
    assert {
        r[0] for r in conn.execute("SELECT job_id FROM scheduler_job_registry")
    } == {f"job-{s}" for s in registry_statuses}
    assert {r[0] for r in conn.execute("SELECT run_id FROM scrape_runs")} == {
        f"run-{s}" for s in ("running", "completed", "partial", "failed")
    }
    new_registry = {
        r[1]: tuple(r)
        for r in conn.execute("PRAGMA table_info(scheduler_job_registry)")
    }
    new_scrape = {
        r[1]: tuple(r) for r in conn.execute("PRAGMA table_info(scrape_runs)")
    }
    assert all(
        row[1] in new_registry and new_registry[row[1]][2:6] == row[2:6]
        for row in old_registry_cols
    )
    assert all(
        row[1] in new_scrape and new_scrape[row[1]][2:6] == row[2:6]
        for row in old_scrape_cols
    )
    objects = {
        (r[0], r[1]) for r in conn.execute("SELECT type,name FROM sqlite_master")
    }
    assert {
        ("index", "custom_registry_type"),
        ("trigger", "registry_update_log"),
        ("view", "running_registry_view"),
    } <= objects
    repository_indexes = {
        "idx_scheduler_registry_status_time",
        "idx_scheduler_registry_match",
        "idx_scheduler_registry_round",
        "idx_scrape_runs_started_at",
        "idx_scrape_runs_type_status_started",
        "idx_scrape_runs_status_started",
        "idx_scrape_runs_correlation_id",
        "idx_scrape_runs_reason_started",
        "idx_scrape_runs_canonical_match_started",
    }
    assert repository_indexes <= {
        row[1]
        for row in conn.execute(
            "SELECT type,name FROM sqlite_master WHERE type='index'"
        )
    }
    assert conn.execute("SELECT job_id FROM running_registry_view").fetchall() == [
        ("job-running",)
    ]
    conn.execute(
        "UPDATE scheduler_job_registry SET updated_at=? WHERE job_id='job-pending'",
        ("2026-08-06T01:00:00+00:00",),
    )
    assert conn.execute("SELECT job_id FROM recovery_trigger_log").fetchall() == [
        ("job-pending",)
    ]
    plan = " ".join(
        str(x)
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM scheduler_job_registry WHERE job_type='legacy' AND status='running'"
        )
        for x in row
    )
    assert "custom_registry_type" in plan
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO scheduler_job_registry(job_id,job_type,status) VALUES('invalid','x','not-a-status')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO scrape_runs(run_id,scrape_type,trigger_source,status,started_at) VALUES('invalid','x','scheduler','not-a-status',?)", ("2026-08-06T00:00:00+00:00",))


def test_0015_existing_keys_get_only_standard_read_and_migration_is_idempotent(tmp_path):
    before = tmp_path / "before-0015"
    before.mkdir()
    for source in MIGRATIONS_DIR.glob("*.py"):
        if source.name == "__init__.py" or source.name.startswith("0015_"):
            continue
        shutil.copy2(source, before / source.name)
    db = tmp_path / "legacy-0014.db"
    migrate_database(db, before)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO api_keys(label, key_hash, key_prefix, is_active) VALUES(?,?,?,1)",
        ("existing", "sha256:existing", "existing"),
    )
    conn.commit()
    conn.close()

    assert migrate_database(db) == ["0015"]
    assert migrate_database(db) == []
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT capability FROM api_key_capabilities").fetchall() == [
        ("standard-read",)
    ]
    rate = {row[1]: row for row in conn.execute("PRAGMA table_info(api_keys)")}
    assert "rate_limit_per_minute" in rate
    assert conn.execute("SELECT rate_limit_per_minute FROM api_keys").fetchone()[0] is None
    conn.close()
