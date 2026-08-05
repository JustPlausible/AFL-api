from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from db.migration_runner import migrate_database
from scheduler.match_windows import (
    MatchWindowSettings, MatchWindowStatus, ReasonCode, claim_due_windows,
    complete_window, inspection_rows, reconcile, record_attempt_failure,
    record_attempt_success, window_id,
)

NOW = datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)


class DirectLane:
    def __init__(self, path): self.path = path
    def execute(self, op, target, cb):
        return self._run(cb, immediate=False)
    def execute_immediate(self, op, target, cb):
        return self._run(cb, immediate=True)
    def _run(self, cb, immediate=False):
        conn = sqlite3.connect(self.path, isolation_level="DEFERRED", timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            result = cb(conn); conn.commit(); return result
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "afl.db"
    monkeypatch.setenv("DB_PATH", str(path))
    import config
    monkeypatch.setattr(config, "DB_PATH", str(path), raising=False)
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn, path
    conn.close()


def add_match(conn, match_id=8001, provider="CD_M1", status="SCHEDULED", start=None, season=73, comp=1):
    start = start or (NOW + timedelta(hours=3)).isoformat()
    conn.execute("INSERT OR IGNORE INTO rounds(round_id, round_label, season_id, competition_id, scraped_at) VALUES(?,?,?,?,?)", (1, "R1", season, comp, NOW.isoformat()))
    conn.execute("INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, start_time_utc, season_id, scraped_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (match_id, provider, 1, "A", "B", "V", status, start, season, NOW.isoformat()))
    conn.commit()


def add_final_stats(conn, provider="CD_M1", count=40, sides=("home", "away")):
    i = 0
    for side in sides:
        for _ in range(count // len(sides)):
            i += 1
            conn.execute("INSERT INTO cfs_player_stats(match_provider_id, champion_data_player_id, afl_match_id, side, collected_at, source_endpoint, resolved_match_status, snapshot_authority, extra_stats_json, raw_player_json) VALUES(?,?,?,?,?,'match_player_statistics','CONCLUDED',2,'{}','{}')",
                         (provider, f"CD_I{i}", "8001", side, NOW.isoformat()))
    conn.commit()


def one(conn):
    return conn.execute("SELECT * FROM match_stat_windows").fetchone()


def settings(**kw):
    base = dict(pre_match_window=timedelta(hours=2), post_match_horizon=timedelta(hours=6), lease_duration=timedelta(minutes=5))
    base.update(kw)
    s = MatchWindowSettings(**base); s.validate(); return s


def test_fresh_database_migration(db):
    conn, _ = db
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='match_stat_windows'").fetchone()


def test_existing_populated_database_migration(tmp_path, monkeypatch):
    path = tmp_path / "existing.db"
    monkeypatch.setenv("DB_PATH", str(path))
    import config
    monkeypatch.setattr(config, "DB_PATH", str(path), raising=False)
    migrate_database(path)
    conn = sqlite3.connect(path); conn.execute("INSERT OR IGNORE INTO rounds(round_id,round_label,season_id,competition_id,scraped_at) VALUES(1,'R1',73,1,?)", (NOW.isoformat(),)); conn.execute("INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,status,start_time_utc,scraped_at) VALUES(1,'CD_M1',1,'A','B','V','SCHEDULED',?,?)", (NOW.isoformat(), NOW.isoformat())); conn.commit(); conn.close()
    assert migrate_database(path) == []


def test_repeated_planning_one_active_and_fixture_change(db):
    conn, _ = db; add_match(conn)
    reconcile(conn, now=NOW, settings=settings())
    reconcile(conn, now=NOW, settings=settings())
    assert conn.execute("SELECT COUNT(*) FROM match_stat_windows").fetchone()[0] == 1
    old_due = one(conn)["next_due_at"]
    conn.execute("UPDATE matches SET start_time_utc=? WHERE match_id=8001", ((NOW + timedelta(hours=4)).isoformat(),)); conn.commit()
    reconcile(conn, now=NOW, settings=settings())
    assert one(conn)["next_due_at"] != old_due


@pytest.mark.parametrize("status,expected,reason", [
    ("SCHEDULED", "due", ReasonCode.APPROACHING_START.value),
    ("LIVE", "due", ReasonCode.LIVE.value),
    ("POSTGAME", "awaiting_final", ReasonCode.AWAITING_FINAL.value),
    ("POSTPONED", "backoff", ReasonCode.POSTPONED.value),
    ("CANCELLED", "cancelled", ReasonCode.CANCELLED.value),
    ("WEIRD", "planning_error", ReasonCode.UNKNOWN_LIFECYCLE.value),
])
def test_lifecycle_handling(db, status, expected, reason):
    conn, _ = db; add_match(conn, status=status, start=(NOW - timedelta(minutes=1)).isoformat())
    reconcile(conn, now=NOW, settings=settings())
    assert one(conn)["status"] == expected
    assert one(conn)["reason_code"] == reason


def test_future_outside_window_boundary_and_due_boundary(db):
    conn, _ = db; add_match(conn, start=(NOW + timedelta(hours=2)).isoformat())
    reconcile(conn, now=NOW, settings=settings())
    assert one(conn)["status"] == "due"
    conn.execute("DELETE FROM match_stat_windows"); conn.execute("UPDATE matches SET start_time_utc=?", ((NOW + timedelta(hours=2, seconds=1)).isoformat(),)); conn.commit()
    reconcile(conn, now=NOW, settings=settings())
    assert one(conn)["status"] == "planned"


def test_authoritative_concluded_snapshot_completes_but_elapsed_time_does_not(db):
    conn, _ = db; add_match(conn, status="CONCLUDED", start=(NOW - timedelta(hours=1)).isoformat())
    reconcile(conn, now=NOW + timedelta(hours=2), settings=settings())
    assert one(conn)["status"] == "awaiting_final"
    add_final_stats(conn)
    reconcile(conn, now=NOW + timedelta(hours=2), settings=settings())
    assert one(conn)["status"] == "complete"


def test_partial_or_unavailable_final_remains_eligible_until_horizon(db):
    conn, _ = db; add_match(conn, status="CONCLUDED", start=(NOW - timedelta(hours=1)).isoformat())
    add_final_stats(conn, count=10, sides=("home",))
    reconcile(conn, now=NOW, settings=settings())
    assert one(conn)["status"] == "awaiting_final"
    reconcile(conn, now=NOW + timedelta(hours=7), settings=settings())
    assert one(conn)["reason_code"] == ReasonCode.POLLING_HORIZON_EXCEEDED.value


def test_missing_provider_identity_durable_failure(db):
    conn, _ = db; add_match(conn, provider=None)
    reconcile(conn, now=NOW, settings=settings())
    assert one(conn)["status"] == "planning_error"
    assert one(conn)["reason_code"] == ReasonCode.MISSING_PROVIDER_IDENTITY.value


def test_planner_disabled_and_allowlist(db):
    conn, _ = db; add_match(conn)
    reconcile(conn, now=NOW, settings=settings(enabled=False))
    assert one(conn)["status"] == "disabled"
    reconcile(conn, now=NOW, settings=settings(enabled=True, supported_competitions=("999",)))
    assert one(conn)["status"] == "not_applicable"


def test_atomic_claim_valid_lease_expired_reclaim_and_stale_owner(db):
    conn, path = db; add_match(conn, status="LIVE", start=(NOW - timedelta(minutes=1)).isoformat())
    reconcile(conn, now=NOW, settings=settings()); conn.commit()
    lane = DirectLane(path)
    c1 = claim_due_windows("owner1", now=NOW, settings=settings(), lane=lane)
    c2 = claim_due_windows("owner2", now=NOW, settings=settings(), lane=lane)
    assert len(c1) == 1 and c2 == []
    c3 = claim_due_windows("owner2", now=NOW + timedelta(minutes=5), settings=settings(), lane=lane)
    assert len(c3) == 1
    assert one(conn)["reason_code"] == ReasonCode.LEASE_EXPIRED_RECLAIMED.value
    assert complete_window(conn, c1[0]["window_id"], c1[0]["lease_token"], now=NOW) is False
    assert record_attempt_success(conn, c3[0]["window_id"], c3[0]["lease_token"], now=NOW, rows_written=0) is True


def test_failed_attempt_and_successful_non_final_update(db):
    conn, path = db; add_match(conn, status="LIVE", start=(NOW - timedelta(minutes=1)).isoformat())
    reconcile(conn, now=NOW, settings=settings()); conn.commit()
    lease = claim_due_windows("owner", now=NOW, settings=settings(), lane=DirectLane(path))[0]
    assert record_attempt_failure(conn, lease["window_id"], lease["lease_token"], now=NOW, reason="boom")
    row = one(conn); assert row["status"] == "backoff" and row["consecutive_failure_count"] == 1
    conn.commit()
    lease = claim_due_windows("owner", now=NOW + timedelta(minutes=11), settings=settings(), lane=DirectLane(path))[0]
    assert record_attempt_success(conn, lease["window_id"], lease["lease_token"], now=NOW, rows_written=3)
    row = one(conn); assert row["attempt_count"] == 2 and row["last_successful_write_at"]


def test_inspection_and_correlation_ids(db):
    conn, _ = db; add_match(conn)
    reconcile(conn, now=NOW, settings=settings())
    row = inspection_rows(conn)[0]
    assert row["window_id"] == window_id(8001)
    from scheduler.match_windows import attempt_id, scheduler_job_id
    assert attempt_id(row["window_id"], 1, 0).startswith(row["window_id"])
    assert scheduler_job_id(row["window_id"], 1, 0).startswith("mw_attempt_")


def test_one_malformed_match_does_not_abort(db):
    conn, _ = db; add_match(conn, match_id=1, provider="CD_M1"); add_match(conn, match_id=2, provider="CD_M2", start="not-a-time")
    result = reconcile(conn, now=NOW, settings=settings())
    # malformed timestamp is recorded as a per-window terminal reason, not a process abort
    assert result.degraded is False
    assert conn.execute("SELECT COUNT(*) FROM match_stat_windows").fetchone()[0] == 2


def test_reconciliation_preserves_valid_lease_and_blocks_second_claim(db):
    conn, path = db; add_match(conn, status="LIVE", start=(NOW - timedelta(minutes=1)).isoformat())
    reconcile(conn, now=NOW, settings=settings()); conn.commit()
    first = claim_due_windows("owner1", now=NOW, settings=settings(), lane=DirectLane(path))[0]
    conn.execute("UPDATE matches SET status='CONCLUDED' WHERE match_id=8001"); conn.commit()
    reconcile(conn, now=NOW + timedelta(minutes=1), settings=settings())
    row = one(conn)
    assert row["status"] == "leased"
    assert row["lease_owner"] == "owner1"
    assert row["lease_token"] == first["lease_token"]
    conn.commit()
    assert claim_due_windows("owner2", now=NOW + timedelta(minutes=1), settings=settings(), lane=DirectLane(path)) == []


def test_recoverable_planning_errors_reopen_without_duplicates(db):
    conn, _ = db; add_match(conn, provider=None, status="WEIRD", start=None)
    conn.execute("UPDATE matches SET start_time_utc=NULL WHERE match_id=8001"); conn.commit()
    reconcile(conn, now=NOW, settings=settings())
    assert one(conn)["status"] == "planning_error"
    conn.execute("UPDATE matches SET match_provider_id='CD_M1', status='SCHEDULED', start_time_utc=? WHERE match_id=8001", ((NOW + timedelta(hours=1)).isoformat(),)); conn.commit()
    reconcile(conn, now=NOW, settings=settings())
    assert one(conn)["status"] == "due"
    reconcile(conn, now=NOW, settings=settings())
    assert conn.execute("SELECT COUNT(*) FROM match_stat_windows").fetchone()[0] == 1


def test_completion_requires_persisted_authoritative_finality(db):
    conn, path = db; add_match(conn, status="CONCLUDED", start=(NOW - timedelta(hours=1)).isoformat())
    reconcile(conn, now=NOW, settings=settings()); conn.commit()
    lease = claim_due_windows("owner", now=NOW, settings=settings(), lane=DirectLane(path))[0]
    assert record_attempt_success(conn, lease["window_id"], lease["lease_token"], now=NOW, rows_written=0, final=True) is False
    add_final_stats(conn, count=10, sides=("home",))
    assert complete_window(conn, lease["window_id"], lease["lease_token"], now=NOW) is False
    conn.execute("DELETE FROM cfs_player_stats"); conn.commit(); add_final_stats(conn, count=40)
    assert complete_window(conn, lease["window_id"], lease["lease_token"], now=NOW) is True


def test_horizon_uses_lifecycle_observed_or_expected_end_not_start(db):
    conn, _ = db; add_match(conn, status="CONCLUDED", start=(NOW - timedelta(hours=20)).isoformat())
    reconcile(conn, now=NOW, settings=settings(post_match_horizon=timedelta(hours=6)))
    assert one(conn)["status"] == "awaiting_final"
    reconcile(conn, now=NOW + timedelta(hours=7), settings=settings(post_match_horizon=timedelta(hours=6)))
    assert one(conn)["reason_code"] == ReasonCode.POLLING_HORIZON_EXCEEDED.value


def test_planner_finality_matches_season_report_contract(db):
    from afl_json.season_report import authoritative_stats_finality_for_match
    conn, _ = db; add_match(conn, status="CONCLUDED", start=(NOW - timedelta(hours=1)).isoformat())
    for count, sides, expected in [(0, ("home", "away"), False), (10, ("home",), False), (10, ("home", "away"), False), (40, ("home", "away"), True)]:
        conn.execute("DELETE FROM cfs_player_stats"); conn.commit()
        if count:
            add_final_stats(conn, count=count, sides=sides)
        report_finality = authoritative_stats_finality_for_match(conn, "CD_M1")
        reconcile(conn, now=NOW, settings=settings())
        assert (one(conn)["status"] == "complete") is (report_finality.has_satisfactory_concluded_coverage is expected and expected)
        conn.execute("DELETE FROM match_stat_windows"); conn.commit()
    conn.execute("INSERT INTO cfs_player_stats(match_provider_id, champion_data_player_id, afl_match_id, side, collected_at, source_endpoint, resolved_match_status, snapshot_authority, extra_stats_json, raw_player_json) VALUES('CD_M1','CD_LIVE','8001','home',?,'match_player_statistics','LIVE',1,'{}','{}')", (NOW.isoformat(),)); conn.commit()
    assert not authoritative_stats_finality_for_match(conn, "CD_M1").has_satisfactory_concluded_coverage


def test_match_window_config_parsing(monkeypatch):
    import importlib, config as config_module
    monkeypatch.setenv("AFL_MATCH_WINDOW_PLANNER_ENABLED", "off")
    monkeypatch.setenv("AFL_MATCH_WINDOW_PRE_MATCH_SECONDS", "60")
    monkeypatch.setenv("AFL_MATCH_WINDOW_POST_HORIZON_SECONDS", "120")
    monkeypatch.setenv("AFL_MATCH_WINDOW_LEASE_SECONDS", "30")
    monkeypatch.setenv("AFL_MATCH_WINDOW_RECONCILE_SECONDS", "45")
    monkeypatch.setenv("AFL_MATCH_WINDOW_EXPECTED_MATCH_SECONDS", "3600")
    monkeypatch.setenv("AFL_MATCH_WINDOW_SUPPORTED_COMPETITIONS", "1, 2")
    monkeypatch.setenv("AFL_MATCH_WINDOW_SUPPORTED_SEASONS", "73")
    monkeypatch.setenv("AFL_MATCH_WINDOW_POLICY_VERSION", "test_policy")
    cfg = importlib.reload(config_module)
    import scheduler.match_windows as mw
    monkeypatch.setattr(mw, "config", cfg)
    parsed = MatchWindowSettings.from_config()
    assert parsed.enabled is False
    assert parsed.pre_match_window == timedelta(seconds=60)
    assert parsed.supported_competitions == ("1", "2")
    assert parsed.supported_seasons == ("73",)
    assert parsed.policy_version == "test_policy"


def test_match_window_config_validation_errors(monkeypatch):
    import importlib, config as config_module
    monkeypatch.setenv("AFL_MATCH_WINDOW_LEASE_SECONDS", "0")
    cfg = importlib.reload(config_module)
    import scheduler.match_windows as mw
    monkeypatch.setattr(mw, "config", cfg)
    with pytest.raises(ValueError, match="lease duration"):
        MatchWindowSettings.from_config()
    monkeypatch.setenv("AFL_MATCH_WINDOW_LEASE_SECONDS", "30")
    monkeypatch.setenv("AFL_MATCH_WINDOW_PRE_MATCH_SECONDS", "-1")
    cfg = importlib.reload(config_module); monkeypatch.setattr(mw, "config", cfg)
    with pytest.raises(ValueError, match="non-negative"):
        MatchWindowSettings.from_config()
    monkeypatch.setenv("AFL_MATCH_WINDOW_PRE_MATCH_SECONDS", "bad")
    with pytest.raises(ValueError, match="integer"):
        importlib.reload(config_module)
    monkeypatch.setenv("AFL_MATCH_WINDOW_PRE_MATCH_SECONDS", "7200")
    monkeypatch.setenv("AFL_MATCH_WINDOW_LEASE_SECONDS", "900")
    importlib.reload(config_module)


def test_claim_due_windows_uses_production_write_lane_without_nested_transaction(db, monkeypatch):
    conn, _ = db; add_match(conn, status="LIVE", start=(NOW - timedelta(minutes=1)).isoformat())
    reconcile(conn, now=NOW, settings=settings()); conn.commit()
    claimed = claim_due_windows("prod-owner", now=NOW, settings=settings())
    assert len(claimed) == 1
    assert one(conn)["status"] == "leased"


def test_claim_due_windows_rolls_back_on_callback_failure(db, monkeypatch):
    conn, _ = db; add_match(conn, status="LIVE", start=(NOW - timedelta(minutes=1)).isoformat())
    reconcile(conn, now=NOW, settings=settings()); conn.commit()
    import scheduler.match_windows as mw
    monkeypatch.setattr(mw, "attempt_id", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("forced")))
    with pytest.raises(RuntimeError, match="forced"):
        claim_due_windows("prod-owner", now=NOW, settings=settings())
    assert one(conn)["status"] == "due"
    assert one(conn)["lease_owner"] is None
