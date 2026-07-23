import importlib
import sqlite3

import pytest

import config
from db.migration_runner import migrate_database
from db.scrape_runs import STATUS_COMPLETED, STATUS_FAILED, TRIGGER_SCHEDULER


def _setup_db(tmp_path, monkeypatch):
    db = tmp_path / "scrapers.db"
    migrate_database(db)
    monkeypatch.setattr(config, "DB_PATH", str(db))
    return db


def _import_scraper_modules(tmp_path, monkeypatch):
    """Import scraper modules with any import-time data files isolated in tmp_path."""
    (tmp_path / "logs").mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "clubs.json").write_text("[]")
    monkeypatch.chdir(tmp_path)
    return {
        "fixture": importlib.import_module("scraper.scrape_afl_fixtures"),
        "injury": importlib.import_module("scraper.scrape_afl_injuries"),
        "lineup": importlib.import_module("scraper.scrape_afl_lineups"),
        "match": importlib.import_module("scraper.scrape_afl_matches"),
        "player_stats": importlib.import_module("scraper.scrape_afl_player_stats"),
    }


def _rows(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return c.execute("SELECT * FROM scrape_runs ORDER BY started_at").fetchall()
    finally:
        c.close()


def _assert_running_exists(db, scrape_type):
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["scrape_type"] == scrape_type
    assert rows[0]["status"] == "running"


@pytest.mark.parametrize("module_key,func,patch_name,args,scrape_type,return_value", [
    ("fixture", "update_fixture_cache", "_update_fixture_cache", (), "fixture", "ok"),
    ("lineup", "scrape_team_lineups", "_scrape_team_lineups", (9,), "lineup", [{"match_id": 1}]),
    ("lineup", "scrape_match_lineup", "_scrape_match_lineup", (7043,), "lineup", [{"match_id": 7043}]),
    ("match", "run", "_run", (9,), "match", None),
    ("player_stats", "run_scraper", "_run_scraper", (7043, True), "player_stats", None),
])
def test_scraper_entrypoint_success_audits_before_work(tmp_path, monkeypatch, module_key, func, patch_name, args, scrape_type, return_value):
    db = _setup_db(tmp_path, monkeypatch)
    module = _import_scraper_modules(tmp_path, monkeypatch)[module_key]

    def fake(*fake_args, **fake_kwargs):
        _assert_running_exists(db, scrape_type)
        return return_value

    monkeypatch.setattr(module, patch_name, fake)
    result = getattr(module, func)(*args, trigger_source=TRIGGER_SCHEDULER, correlation_id="job-1")
    assert result == return_value
    row = _rows(db)[0]
    assert row["status"] == STATUS_COMPLETED
    assert row["trigger_source"] == TRIGGER_SCHEDULER
    assert row["correlation_id"] == "job-1"


@pytest.mark.parametrize("module_key,func,patch_name,args,scrape_type", [
    ("fixture", "update_fixture_cache", "_update_fixture_cache", (), "fixture"),
    ("injury", "scrape_injury_list", "_scrape_injury_list", (None,), "injury"),
    ("lineup", "scrape_team_lineups", "_scrape_team_lineups", (9,), "lineup"),
    ("match", "run", "_run", (9,), "match"),
    ("player_stats", "run_scraper", "_run_scraper", (7043, True), "player_stats"),
])
def test_scraper_entrypoint_failure_audits_and_preserves_exception(tmp_path, monkeypatch, module_key, func, patch_name, args, scrape_type):
    db = _setup_db(tmp_path, monkeypatch)
    module = _import_scraper_modules(tmp_path, monkeypatch)[module_key]

    def boom(*fake_args, **fake_kwargs):
        _assert_running_exists(db, scrape_type)
        raise RuntimeError("boom token=secret")

    monkeypatch.setattr(module, patch_name, boom)
    with pytest.raises(RuntimeError, match="boom"):
        getattr(module, func)(*args, trigger_source=TRIGGER_SCHEDULER, correlation_id="job-2")
    row = _rows(db)[0]
    assert row["status"] == STATUS_FAILED
    assert row["error_class"] == "RuntimeError"
    assert "secret" not in row["error_summary"]
    assert row["correlation_id"] == "job-2"
