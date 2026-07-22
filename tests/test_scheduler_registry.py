from datetime import datetime, timedelta, timezone
import subprocess

from apscheduler.schedulers.background import BackgroundScheduler

from db.migration_runner import migrate_database
import config
from scheduler import registry


def _db(tmp_path, monkeypatch):
    path = tmp_path / "afl.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    migrate_database(path)
    return path


def noop(value=None):
    return value


def boom():
    raise RuntimeError("failed token=supersecret")


def test_deterministic_job_ids_for_supported_categories():
    assert registry.stats_match_job_id(42) == "stats_match_42"
    assert registry.lineup_match_job_id(42) == "lineups_match_42"
    assert registry.lineup_round_job_id(7, "day before 5pm") == "lineups_round_7_day_before_5pm"
    assert registry.match_refresh_job_id(42) == "match_refresh_42"
    assert registry.fixture_job_id() == "fixtures_daily"
    assert registry.injury_job_id() == "injuries_daily"
    assert registry.refresh_job_id("Players") == "refresh_players"


def test_upsert_preserves_history(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    run_at = datetime.now(timezone.utc) + timedelta(hours=1)
    registry.upsert_job("stats_match_1", "player_stats", run_at, match_id=1, func_ref="tests.test_scheduler_registry:noop", args=[1])
    registry.mark_running("stats_match_1")
    registry.mark_succeeded("stats_match_1")
    before = registry.registry_rows()[0]
    registry.upsert_job("stats_match_1", "player_stats", run_at + timedelta(hours=1), match_id=1, func_ref="tests.test_scheduler_registry:noop", args=[1])
    after = registry.registry_rows()[0]
    assert after["status"] == registry.SUCCEEDED
    assert after["attempt_count"] == before["attempt_count"] == 1
    assert after["last_success_time"] == before["last_success_time"]


def test_status_transitions_attempts_and_concise_error(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    registry.upsert_job("refresh_bad", "general_refresh", datetime.now(timezone.utc), func_ref="tests.test_scheduler_registry:boom")
    try:
        registry.execute_registered_job("refresh_bad", boom)
    except RuntimeError:
        pass
    row = registry.registry_rows()[0]
    assert row["status"] == registry.FAILED
    assert row["attempt_count"] == 1
    assert row["last_attempt_time"]
    assert "<redacted>" in row["last_error_summary"]
    registry.mark_skipped("refresh_bad", "expired")
    assert registry.registry_rows()[0]["status"] == registry.SKIPPED


def test_reconcile_pending_future_idempotent_and_existing(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    sched = BackgroundScheduler(timezone=timezone.utc)
    run_at = datetime.now(timezone.utc) + timedelta(hours=1)
    registry.upsert_job("refresh_future", "general_refresh", run_at, func_ref="tests.test_scheduler_registry:noop", args=[])
    assert registry.reconcile_scheduler(sched)["re_registered"] == 1
    assert sched.get_job("refresh_future")
    assert registry.reconcile_scheduler(sched)["already_present"] == 1
    assert len([j for j in sched.get_jobs() if j.id == "refresh_future"]) == 1


def test_reconcile_skips_succeeded_failed_and_expired(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    sched = BackgroundScheduler(timezone=timezone.utc)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    registry.upsert_job("done", "general_refresh", future, status=registry.SUCCEEDED, func_ref="tests.test_scheduler_registry:noop")
    registry.upsert_job("bad", "general_refresh", future, status=registry.FAILED, func_ref="tests.test_scheduler_registry:noop")
    registry.upsert_job("old", "general_refresh", past, func_ref="tests.test_scheduler_registry:noop")
    result = registry.reconcile_scheduler(sched)
    assert result["ignored"] == 2
    assert result["skipped"] == 1
    assert not sched.get_jobs()
    rows = {r["job_id"]: r for r in registry.registry_rows()}
    assert rows["old"]["status"] == registry.SKIPPED


def test_startup_succeeds_with_no_registry_rows(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    sched = BackgroundScheduler(timezone=timezone.utc)
    assert registry.reconcile_scheduler(sched)["re_registered"] == 0
