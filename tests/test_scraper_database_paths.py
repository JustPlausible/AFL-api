import sqlite3
import os
import subprocess
import sys

import pytest

import config
from db.migration_runner import migrate_database
from scraper import scrape_afl_fixtures, scrape_afl_matches


def _configured_database(tmp_path, monkeypatch):
    db_path = tmp_path / "configured" / "scraper.db"
    db_path.parent.mkdir()
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    migrate_database(db_path)
    return db_path


def test_fixture_scraper_writes_configured_database_and_closes_connection(tmp_path, monkeypatch):
    db_path = _configured_database(tmp_path, monkeypatch)
    unexpected = tmp_path / "working-directory" / "data" / "afl_players.db"
    unexpected.parent.mkdir(parents=True)
    monkeypatch.chdir(unexpected.parent.parent)
    monkeypatch.setattr(scrape_afl_fixtures, "load_page_with_playwright", lambda _url: "fixture html")
    monkeypatch.setattr(scrape_afl_fixtures, "parse_fixtures_metadata", lambda _html: {
        "season_id": 73, "competition_id": 1,
    })
    monkeypatch.setattr(scrape_afl_fixtures, "parse_round_list", lambda _html: [{
        "round_id": 1234, "round_label": "Round 1",
    }])

    opened = []
    real_get_connection = scrape_afl_fixtures.get_db_connection

    def tracked_connection():
        conn = real_get_connection()
        opened.append(conn)
        return conn

    monkeypatch.setattr(scrape_afl_fixtures, "get_db_connection", tracked_connection)
    scrape_afl_fixtures._update_fixture_cache()

    with sqlite3.connect(db_path) as verification:
        assert verification.execute(
            "SELECT round_label FROM rounds WHERE round_id = 1234"
        ).fetchone() == ("Round 1",)
    assert not unexpected.exists()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


def test_match_scraper_rolls_back_and_closes_on_failure(tmp_path, monkeypatch):
    db_path = _configured_database(tmp_path, monkeypatch)
    opened = []
    real_get_connection = scrape_afl_matches.get_db_connection

    def tracked_connection():
        conn = real_get_connection()
        opened.append(conn)
        return conn

    def failing_scrape(_round_id, conn):
        conn.execute(
            "INSERT INTO rounds (round_id, round_label, season_id, competition_id) VALUES (4321, 'pending', 73, 1)"
        )
        raise RuntimeError("scrape failed")

    monkeypatch.setattr(scrape_afl_matches, "get_db_connection", tracked_connection)
    monkeypatch.setattr(scrape_afl_matches, "scrape_round", failing_scrape)

    with pytest.raises(RuntimeError, match="scrape failed"):
        scrape_afl_matches._run(round_id=4321)

    with sqlite3.connect(db_path) as verification:
        assert verification.execute(
            "SELECT 1 FROM rounds WHERE round_id = 4321"
        ).fetchone() is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


def test_default_database_path_remains_repository_relative_default(tmp_path):
    env = os.environ.copy()
    env.pop("DB_PATH", None)
    env["PYTHONPATH"] = str(config.PROJECT_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.DB_PATH)"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == str((config.PROJECT_ROOT / "data/afl_players.db").resolve())
