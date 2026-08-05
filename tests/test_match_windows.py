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
        conn = sqlite3.connect(self.path, isolation_level="DEFERRED", timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
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
    ("WEIRD", "failed_terminal", ReasonCode.UNKNOWN_LIFECYCLE.value),
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
    assert one(conn)["status"] == "failed_terminal"
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
