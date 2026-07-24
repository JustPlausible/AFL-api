import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import config
from db.migration_runner import migrate_database
from scheduler import manual_triggers
from scheduler.api import app
from scheduler.scheduled_tasks import scheduler


def _db(tmp_path, monkeypatch):
    path = tmp_path / "afl.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO rounds(round_id, round_label) VALUES (1, 'Round 1')")
    conn.execute("INSERT INTO matches(match_id, round_id, home_team, away_team) VALUES (10, 1, 'A', 'B')")
    conn.commit(); conn.close()
    for job in list(scheduler.get_jobs()):
        if job.id.startswith("admin_manual_"):
            scheduler.remove_job(job.id)
    return path


def _registry_count(path):
    conn = sqlite3.connect(path)
    count = conn.execute("SELECT COUNT(*) FROM scheduler_job_registry WHERE trigger_type='admin_manual'").fetchone()[0]
    conn.close(); return count


def test_scheduler_valid_triggers_create_registry_with_admin_manual_and_correct_args(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    client = TestClient(app)
    calls = []
    def fake_add(sched, func, **kwargs):
        calls.append((func.__name__, kwargs))
        from scheduler.registry import upsert_job
        upsert_job(kwargs["job_id"], kwargs["job_type"], kwargs["run_date"], match_id=kwargs.get("match_id"), round_id=kwargs.get("round_id"), func_ref=f"scheduler.manual_triggers:{func.__name__}", args=kwargs.get("args"), trigger_type=kwargs.get("trigger_type"))
    monkeypatch.setattr(manual_triggers, "add_registered_job", fake_add)
    cases = [
        ("/scheduler/manual/injuries", {}, "manual_refresh_injuries", "injury"),
        ("/scheduler/manual/fixtures/round", {"round_id": 1}, "manual_refresh_fixtures_round", "fixture"),
        ("/scheduler/manual/lineups/round", {"round_id": 1}, "manual_refresh_lineups_round", "lineup"),
        ("/scheduler/manual/lineups/match", {"match_id": 10}, "manual_refresh_lineups_match", "lineup"),
        ("/scheduler/manual/player-stats/match", {"match_id": 10}, "manual_refresh_player_stats_match", "player_stats"),
    ]
    for url, payload, func_name, job_type in cases:
        resp = client.post(url, json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["trigger_source"] == "admin_manual"
        assert calls[-1][0] == func_name
        assert calls[-1][1]["job_type"] == job_type
        assert calls[-1][1]["trigger_type"] == "admin_manual"
    assert _registry_count(path) == 5


def test_scheduler_invalid_submissions_create_no_registry_or_audit_record(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    client = TestClient(app)
    for url, payload in [
        ("/scheduler/manual/fixtures/round", {}),
        ("/scheduler/manual/fixtures/round", {"round_id": 0}),
        ("/scheduler/manual/fixtures/round", {"round_id": -1}),
        ("/scheduler/manual/fixtures/round", {"round_id": 999}),
        ("/scheduler/manual/lineups/match", {}),
        ("/scheduler/manual/lineups/match", {"match_id": 0}),
        ("/scheduler/manual/lineups/match", {"match_id": -1}),
        ("/scheduler/manual/lineups/match", {"match_id": 999}),
    ]:
        assert client.post(url, json=payload).status_code == 422
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM scheduler_job_registry").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0] == 0
    conn.close()


def test_manual_wrappers_propagate_admin_manual_once_and_correlation(monkeypatch):
    calls = []
    import types, sys
    monkeypatch.setitem(sys.modules, "scraper.scrape_afl_matches", types.SimpleNamespace(run=lambda **kw: calls.append(("fixtures", kw))))
    monkeypatch.setitem(sys.modules, "scraper.scrape_afl_lineups", types.SimpleNamespace(
        scrape_team_lineups=lambda **kw: calls.append(("lineups_round", kw)),
        scrape_match_lineup=lambda **kw: calls.append(("lineups_match", kw)),
    ))
    monkeypatch.setitem(sys.modules, "scraper.scrape_afl_player_stats", types.SimpleNamespace(run_scraper=lambda **kw: calls.append(("stats", kw))))
    manual_triggers.manual_refresh_fixtures_round(1, "jid")
    manual_triggers.manual_refresh_lineups_round(1, "jid")
    manual_triggers.manual_refresh_lineups_match(10, "jid")
    manual_triggers.manual_refresh_player_stats_match(10, "jid")
    assert all(call[1]["trigger_source"] == "admin_manual" for call in calls)
    assert all(call[1]["correlation_id"] == "jid" for call in calls)
    assert calls[-1][1]["once"] is True


def test_duplicate_manual_trigger_returns_existing_job(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    from scheduler.registry import upsert_job
    upsert_job("admin_manual_lineup_round_1_existing", "lineup", datetime.now(timezone.utc), round_id=1, trigger_type="admin_manual")
    resp = TestClient(app).post("/scheduler/manual/lineups/round", json={"round_id": 1})
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_running"


def test_automatic_job_ids_are_unchanged():
    from scheduler.registry import fixture_job_id, injury_job_id, stats_match_job_id
    assert fixture_job_id() == "fixtures_daily"
    assert injury_job_id() == "injuries_daily"
    assert stats_match_job_id(10) == "stats_match_10"
