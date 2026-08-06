from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from db.migration_runner import migrate_database
from scheduler.recovery import (
    RecoveryScope,
    RecoverySettings,
    reconcile_interrupted_attempts,
)

NOW = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
OLD = (NOW - timedelta(hours=2)).isoformat()


class Lane:
    def __init__(self, path):
        self.path = path

    def execute_immediate(self, operation, target, callback):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = callback(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    execute = execute_immediate


@pytest.fixture
def recovery_db(tmp_path):
    path = tmp_path / "recovery.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO rounds(round_id,round_label,season_id,competition_id) VALUES(1,'R1',1,1)"
    )
    conn.execute(
        """INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,status,start_time_utc)
                    VALUES(1,'CD_M1',1,'H','A','CONCLUDED',?)""",
        (OLD,),
    )
    conn.execute(
        """INSERT INTO match_stat_windows(window_id,match_id,match_provider_id,
        policy_version,lifecycle,collection_phase,status,next_due_at,cadence_profile,
        finality_state,lease_owner,lease_token,lease_generation,lease_claimed_at,
        lease_expires_at,reason_code,planner_version,updated_at)
        VALUES('w1',1,'CD_M1','v1','CONCLUDED','final_confirmation','leased',?,
        'final','unconfirmed','old-worker','token-1',1,?,?,'live','v1',?)""",
        (OLD, OLD, OLD, OLD),
    )
    conn.commit()
    conn.close()
    return path, Lane(path)


def settings():
    return RecoverySettings(
        timedelta(minutes=30),
        timedelta(minutes=30),
        timedelta(minutes=30),
        timedelta(minutes=2),
    )


def run(path_lane, **kwargs):
    path, lane = path_lane
    return reconcile_interrupted_attempts(
        trigger_source="test",
        now=NOW,
        settings=settings(),
        lane=lane,
        run_id="recovery-test",
        **kwargs,
    )


def fetch(path, sql):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_expired_lease_without_attempt_is_replanned_once(recovery_db):
    first = run(recovery_db)
    assert (
        first.stale_leases_found,
        first.stale_leases_expired,
        first.windows_replanned,
    ) == (1, 1, 1)
    row = fetch(
        recovery_db[0],
        "SELECT status,lease_token,recovery_reason FROM match_stat_windows",
    )[0]
    assert row["status"] == "backoff" and row["lease_token"] is None
    second = run(recovery_db)
    assert second.inspected_windows == second.windows_replanned == 0


@pytest.mark.parametrize(
    "with_scrape,response,expected",
    [
        (False, False, "registry_started_before_scrape_run"),
        (True, False, "scrape_started_no_completed_request"),
        (True, True, "response_received_persistence_unproven"),
    ],
)
def test_interruption_state_matrix(recovery_db, with_scrape, response, expected):
    path, _ = recovery_db
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO scheduler_job_registry(job_id,job_type,match_id,status,
        last_attempt_time,attempt_count,window_id,attempt_id,lease_generation,lease_token)
        VALUES('j1','cfs_player_stats_poll',1,'running',?,1,'w1','a1',1,'token-1')""",
        (OLD,),
    )
    if with_scrape:
        conn.execute(
            """INSERT INTO scrape_runs(run_id,scrape_type,target_type,target_identifier,
            trigger_source,status,started_at,correlation_id,window_id,attempt_id,scheduler_job_id,
            lease_generation,lease_token,response_received_at)
            VALUES('s1','cfs_player_stats_poll','match','1','scheduler','running',?,
            'a1','w1','a1','j1',1,'token-1',?)""",
            (OLD, OLD if response else None),
        )
    conn.commit()
    conn.close()
    report = run(recovery_db)
    assert report.decisions[0]["reason"] == expected
    assert (
        fetch(path, "SELECT status,job_id,attempt_id FROM scheduler_job_registry")[0][
            "status"
        ]
        == "interrupted"
    )
    if with_scrape:
        audit = fetch(
            path,
            "SELECT status,run_id,attempt_id,persistence_evidence FROM scrape_runs",
        )[0]
        assert tuple(audit) == ("interrupted", "s1", "a1", "unknown")


def test_final_authoritative_cfs_completes_without_collection(recovery_db):
    path, _ = recovery_db
    conn = sqlite3.connect(path)
    for index in range(20):
        conn.execute(
            """INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,
            side,collected_at,source_endpoint,snapshot_authority,extra_stats_json,raw_player_json)
            VALUES('CD_M1',?,?,?,?,2,'{}','{}')""",
            (f"p{index}", "home" if index < 10 else "away", OLD, "cfs"),
        )
    conn.commit()
    conn.close()
    report = run(recovery_db)
    assert report.windows_completed_from_existing_data == 1
    row = fetch(
        path, "SELECT status,finality_state,next_due_at FROM match_stat_windows"
    )[0]
    assert tuple(row) == ("complete", "authoritative_complete", None)


def test_valid_lease_and_partial_data_are_not_reclaimed(recovery_db):
    path, _ = recovery_db
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE match_stat_windows SET lease_expires_at=?",
        ((NOW + timedelta(minutes=5)).isoformat(),),
    )
    conn.execute(
        """INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,
        side,collected_at,source_endpoint,snapshot_authority,extra_stats_json,raw_player_json)
        VALUES('CD_M1','p1','home',?,'cfs',1,'{}','{}')""",
        (OLD,),
    )
    conn.commit()
    conn.close()
    report = run(recovery_db)
    assert report.stale_leases_found == report.windows_completed_from_existing_data == 0
    assert fetch(path, "SELECT status FROM match_stat_windows")[0][0] == "leased"


def test_dry_run_and_bounded_scope_do_not_mutate(recovery_db):
    report = run(recovery_db, dry_run=True, scope=RecoveryScope(window_id="w1"))
    assert report.windows_replanned == 1 and report.dry_run
    row = fetch(recovery_db[0], "SELECT status,lease_token FROM match_stat_windows")[0]
    assert tuple(row) == ("leased", "token-1")
    excluded = run(recovery_db, dry_run=True, scope=RecoveryScope(window_id="other"))
    assert excluded.inspected_windows == 0


def test_sensitive_failure_is_redacted_and_integrity_failure_stops(tmp_path):
    class BrokenLane:
        def execute_immediate(self, *args):
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            try:
                return args[-1](conn)
            finally:
                conn.close()

        execute = execute_immediate

    with pytest.raises(RuntimeError, match="migration readiness"):
        reconcile_interrupted_attempts(
            trigger_source="test", now=NOW, lane=BrokenLane()
        )
