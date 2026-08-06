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
from scheduler.match_windows import MatchWindowSettings

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
        VALUES('mw_cfs_stats_1_v1',1,'CD_M1','v1','CONCLUDED','final_confirmation','leased',?,
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
        window_settings=MatchWindowSettings(policy_version="v1"),
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
    assert row["status"] == "awaiting_final" and row["lease_token"] is None
    second = run(recovery_db)
    assert (
        second.windows_replanned
        == second.registry_rows_repaired
        == second.scrape_runs_repaired
        == 0
    )


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
        VALUES('j1','cfs_player_stats_poll',1,'running',?,1,'mw_cfs_stats_1_v1','a1',1,'token-1')""",
        (OLD,),
    )
    if with_scrape:
        conn.execute(
            """INSERT INTO scrape_runs(run_id,scrape_type,target_type,target_identifier,
            trigger_source,status,started_at,correlation_id,window_id,attempt_id,scheduler_job_id,
            lease_generation,lease_token,response_received_at)
            VALUES('s1','cfs_player_stats_poll','match','1','scheduler','running',?,
            'a1','mw_cfs_stats_1_v1','a1','j1',1,'token-1',?)""",
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
            "SELECT status,run_id,attempt_id,attempt_persistence_evidence FROM scrape_runs",
        )[0]
        assert tuple(audit) == ("interrupted", "s1", "a1", "unknown")


def test_final_authoritative_cfs_completes_without_collection(recovery_db):
    path, _ = recovery_db
    add_running_attempt(path)
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
    registry = fetch(
        path,
        "SELECT attempt_persistence_evidence,match_authoritative_evidence FROM scheduler_job_registry",
    )[0]
    audit = fetch(
        path,
        "SELECT attempt_persistence_evidence,match_authoritative_evidence FROM scrape_runs",
    )[0]
    assert registry[:] == audit[:] == ("unknown", "authoritative_final")


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
    report = run(
        recovery_db, dry_run=True, scope=RecoveryScope(window_id="mw_cfs_stats_1_v1")
    )
    assert report.would_replan_windows == 1 and report.dry_run
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


def add_running_attempt(
    path,
    *,
    attempt="a1",
    job="j1",
    run_id="s1",
    generation=1,
    instance=None,
    with_scrape=True,
    with_registry=True,
    started=OLD,
):
    conn = sqlite3.connect(path)
    wid = "mw_cfs_stats_1_v1"
    if with_registry:
        conn.execute(
            """INSERT INTO scheduler_job_registry(job_id,job_type,match_id,status,
        last_attempt_time,attempt_count,window_id,attempt_id,scrape_run_id,
        lease_generation,lease_token,scheduler_instance_id,created_at,updated_at)
        VALUES(?,'cfs_player_stats_poll',1,'running',?,1,?,?,?,?,?,?,?,?)""",
            (
                job,
                started,
                wid,
                attempt,
                run_id if with_scrape else None,
                generation,
                "token-1",
                instance,
                started,
                started,
            ),
        )
    if with_scrape:
        conn.execute(
            """INSERT INTO scrape_runs(run_id,scrape_type,target_type,target_identifier,
            trigger_source,status,started_at,correlation_id,window_id,attempt_id,
            scheduler_job_id,lease_generation,lease_token,scheduler_instance_id)
            VALUES(?,'cfs_player_stats_poll','match','1','scheduler','running',?,?,?,?,?,?,?,?)""",
            (run_id, started, attempt, wid, attempt, job, generation, "token-1", instance),
        )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("kind", ["registry", "scrape", "expired_lease"])
def test_recent_owner_heartbeat_prevents_age_or_expiry_reclamation(recovery_db, kind):
    path, _ = recovery_db
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO scheduler_runtime_instances(instance_id,started_at,last_heartbeat_at) VALUES('active',?,?)",
        (OLD, NOW.isoformat()),
    )
    conn.execute(
        "UPDATE match_stat_windows SET lease_owner='active:worker',lease_expires_at=?",
        (OLD,),
    )
    conn.commit()
    conn.close()
    add_running_attempt(
        path,
        instance="active",
        with_scrape=kind != "registry",
        with_registry=kind != "scrape",
        started=(NOW - timedelta(minutes=10)).isoformat(),
    )
    report = run(recovery_db)
    assert report.registry_rows_repaired == report.scrape_runs_repaired == 0
    assert (
        fetch(path, "SELECT status,lease_token FROM match_stat_windows")[0][1]
        == "token-1"
    )
    assert report.decisions[-1]["action"] == "active_preserved"


def test_active_heartbeat_cannot_protect_expired_overlong_attempt(recovery_db):
    path, _ = recovery_db
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO scheduler_runtime_instances(instance_id,started_at,last_heartbeat_at) VALUES('active',?,?)",
        (OLD, NOW.isoformat()),
    )
    conn.execute(
        "UPDATE match_stat_windows SET lease_owner='active:worker',lease_expires_at=?",
        (OLD,),
    )
    conn.commit()
    conn.close()
    add_running_attempt(path, instance="active", started=OLD)
    report = run(recovery_db)
    assert report.registry_rows_repaired == report.scrape_runs_repaired == 1
    assert fetch(path, "SELECT status FROM scheduler_job_registry")[0][0] == "interrupted"
    assert fetch(path, "SELECT lease_token FROM match_stat_windows")[0][0] is None


@pytest.mark.parametrize(
    "shutdown,expected",
    [
        ("graceful", "graceful_shutdown_interruption"),
        ("unclean", "unclean_process_interruption"),
    ],
)
def test_stopped_runtime_classification_and_recovery(recovery_db, shutdown, expected):
    path, _ = recovery_db
    conn = sqlite3.connect(path)
    stopped = OLD if shutdown == "graceful" else None
    conn.execute(
        """INSERT INTO scheduler_runtime_instances(instance_id,started_at,last_heartbeat_at,stopped_at,shutdown_kind)
                    VALUES('dead',?,?,?,?)""",
        (OLD, OLD, stopped, shutdown),
    )
    conn.commit()
    conn.close()
    add_running_attempt(path, instance="dead")
    report = run(recovery_db)
    decision = next(item for item in report.decisions if item.get("attempt_id") == "a1")
    assert decision["reason"] == expected
    assert fetch(path, "SELECT status,recovery_reason FROM scheduler_job_registry")[0][
        :
    ] == ("interrupted", expected)


def test_supersession_requires_provably_later_success(recovery_db):
    path, _ = recovery_db
    add_running_attempt(path, attempt="stale", generation=2)
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO scheduler_job_registry(job_id,job_type,match_id,status,
        window_id,attempt_id,lease_generation,last_success_time,created_at,updated_at)
        VALUES('earlier','cfs_player_stats_poll',1,'succeeded','mw_cfs_stats_1_v1','earlier',1,?,?,?)""",
        (OLD, OLD, OLD),
    )
    later = (NOW - timedelta(minutes=10)).isoformat()
    for job, attempt, generation, stamp in [
        ("later1", "later1", 3, later),
        ("later2", "later2", 4, later),
    ]:
        conn.execute(
            """INSERT INTO scheduler_job_registry(job_id,job_type,match_id,status,
            window_id,attempt_id,lease_generation,last_success_time,created_at,updated_at)
            VALUES(?,'cfs_player_stats_poll',1,'succeeded','mw_cfs_stats_1_v1',?,?,?,?,?)""",
            (job, attempt, generation, stamp, stamp, stamp),
        )
    conn.commit()
    conn.close()
    report = run(recovery_db)
    decision = next(
        item for item in report.decisions if item.get("attempt_id") == "stale"
    )
    assert decision["superseded_by_attempt_id"] == "later2"
    assert decision["reason"] == "later_attempt_superseded_stale_attempt"


def test_earlier_or_unordered_success_does_not_supersede(recovery_db):
    path, _ = recovery_db
    add_running_attempt(path, attempt="stale", generation=2)
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO scheduler_job_registry(job_id,job_type,match_id,status,
        window_id,attempt_id,lease_generation,last_success_time,created_at,updated_at)
        VALUES('old','cfs_player_stats_poll',1,'succeeded','mw_cfs_stats_1_v1','old',1,?,?,?)""",
        (OLD, OLD, OLD),
    )
    conn.commit()
    conn.close()
    decision = next(
        item for item in run(recovery_db).decisions if item.get("attempt_id") == "stale"
    )
    assert decision["superseded_by_attempt_id"] is None


def test_multiple_attempt_rows_produce_one_window_plan_and_idempotent_history(
    recovery_db,
):
    path, _ = recovery_db
    add_running_attempt(path, attempt="a1", job="j1", run_id="s1", generation=1)
    add_running_attempt(path, attempt="a2", job="j2", run_id="s2", generation=2)
    first = run(recovery_db)
    assert first.registry_rows_repaired == first.scrape_runs_repaired == 2
    assert first.windows_replanned == 1
    assert [
        r[0]
        for r in fetch(
            path, "SELECT status FROM scheduler_job_registry ORDER BY job_id"
        )
    ] == ["interrupted", "interrupted"]
    second = run(recovery_db)
    assert (
        second.registry_rows_repaired
        == second.scrape_runs_repaired
        == second.windows_replanned
        == 0
    )


def test_uncorrelated_compatibility_records_are_report_only(recovery_db):
    path, _ = recovery_db
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO scheduler_job_registry(job_id,job_type,status,last_attempt_time,updated_at) VALUES('legacy','stats','running',?,?)",
        (OLD, OLD),
    )
    conn.execute(
        "INSERT INTO scrape_runs(run_id,scrape_type,trigger_source,status,started_at) VALUES('legacy-run','stats','scheduler','running',?)",
        (OLD,),
    )
    conn.commit()
    conn.close()
    report = run(recovery_db)
    assert report.compatibility_records == 2
    assert (
        fetch(path, "SELECT status FROM scheduler_job_registry WHERE job_id='legacy'")[
            0
        ][0]
        == "running"
    )
    assert (
        fetch(path, "SELECT status FROM scrape_runs WHERE run_id='legacy-run'")[0][0]
        == "running"
    )


def test_dry_run_database_dump_is_byte_for_byte_logically_unchanged(recovery_db):
    path, _ = recovery_db
    add_running_attempt(path)

    def dump():
        conn = sqlite3.connect(path)
        try:
            return "\n".join(conn.iterdump())
        finally:
            conn.close()

    before = dump()
    report = run(recovery_db, dry_run=True)
    after = dump()
    assert before == after
    assert report.registry_rows_repaired == report.scrape_runs_repaired == 0
    assert report.would_repair_registry_rows == report.would_repair_scrape_runs == 1


def test_planner_owns_horizon_and_feature_constraint_decisions(recovery_db):
    path, lane = recovery_db
    constrained = MatchWindowSettings(
        policy_version="v1", supported_competitions=("other",)
    )
    report = reconcile_interrupted_attempts(
        trigger_source="test",
        now=NOW,
        settings=settings(),
        window_settings=constrained,
        lane=lane,
        run_id="constraints",
    )
    assert report.windows_replanned == 1
    row = fetch(path, "SELECT status,next_due_at FROM match_stat_windows")[0]
    assert row[:] == ("not_applicable", None)


def test_planner_stops_at_horizon_and_recovery_never_reopens_terminal(recovery_db):
    path, lane = recovery_db
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE matches SET start_time_utc=?,status='CONCLUDED'",
        ((NOW - timedelta(hours=10)).isoformat(),),
    )
    conn.execute(
        "UPDATE match_stat_windows SET lifecycle_observed_at=?",
        ((NOW - timedelta(hours=5)).isoformat(),),
    )
    conn.commit()
    conn.close()
    bounded = MatchWindowSettings(
        policy_version="v1",
        post_match_horizon=timedelta(hours=1),
        expected_match_duration=timedelta(hours=3),
    )
    reconcile_interrupted_attempts(
        trigger_source="test",
        now=NOW,
        settings=settings(),
        window_settings=bounded,
        lane=lane,
        run_id="horizon",
    )
    assert fetch(path, "SELECT status,next_due_at FROM match_stat_windows")[0][:] == (
        "failed_terminal",
        None,
    )
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE match_stat_windows SET lease_token='old',lease_expires_at=?", (OLD,)
    )
    conn.commit()
    conn.close()
    second = reconcile_interrupted_attempts(
        trigger_source="test",
        now=NOW,
        settings=settings(),
        window_settings=bounded,
        lane=lane,
        run_id="terminal",
    )
    assert second.windows_replanned == 0
    assert fetch(path, "SELECT status,next_due_at FROM match_stat_windows")[0][:] == (
        "failed_terminal",
        None,
    )


def test_attempt_savepoint_isolates_one_mutation_failure(recovery_db):
    path, _ = recovery_db
    add_running_attempt(path, attempt="bad", job="bad", run_id="bad-run", generation=1)
    conn = sqlite3.connect(path)
    conn.execute("""INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,status,start_time_utc)
                    VALUES(2,'CD_M2',1,'C','D','CONCLUDED',?)""", (OLD,))
    conn.execute("""INSERT INTO match_stat_windows(window_id,match_id,match_provider_id,
        policy_version,lifecycle,collection_phase,status,next_due_at,cadence_profile,
        finality_state,lease_owner,lease_token,lease_generation,lease_claimed_at,
        lease_expires_at,reason_code,planner_version,updated_at)
        VALUES('mw_cfs_stats_2_v1',2,'CD_M2','v1','CONCLUDED','final_confirmation','leased',?,
        'final','unconfirmed','old-worker','token-2',1,?,?,'live','v1',?)""",
        (OLD, OLD, OLD, OLD))
    conn.execute("""INSERT INTO scheduler_job_registry(job_id,job_type,match_id,status,
        last_attempt_time,attempt_count,window_id,attempt_id,scrape_run_id,
        lease_generation,lease_token,created_at,updated_at)
        VALUES('good','cfs_player_stats_poll',2,'running',?,1,'mw_cfs_stats_2_v1',
        'good','good-run',1,'token-2',?,?)""", (OLD, OLD, OLD))
    conn.execute("""INSERT INTO scrape_runs(run_id,scrape_type,target_type,target_identifier,
        trigger_source,status,started_at,correlation_id,window_id,attempt_id,
        scheduler_job_id,lease_generation,lease_token)
        VALUES('good-run','cfs_player_stats_poll','match','2','scheduler','running',?,
        'good','mw_cfs_stats_2_v1','good','good',1,'token-2')""", (OLD,))
    conn.execute("""CREATE TRIGGER fail_bad BEFORE UPDATE ON scheduler_job_registry
                    WHEN OLD.job_id='bad' BEGIN SELECT RAISE(FAIL,'token=secret'); END""")
    conn.commit()
    conn.close()
    report = run(recovery_db)
    states = {
        r[0]: r[1]
        for r in fetch(path, "SELECT job_id,status FROM scheduler_job_registry")
    }
    assert states == {"bad": "running", "good": "interrupted"}
    leases = {r[0]: r[1] for r in fetch(path, "SELECT window_id,lease_token FROM match_stat_windows")}
    assert leases["mw_cfs_stats_1_v1"] == "token-1"
    assert leases["mw_cfs_stats_2_v1"] is None
    assert fetch(path, "SELECT status FROM scrape_runs WHERE run_id='bad-run'")[0][0] == "running"
    assert (
        report.per_item_failures
        and "secret" not in report.per_item_failures[0]["error"]
    )


@pytest.mark.parametrize(
    "replacement",
    [
        {"maximum_attempt_duration": timedelta(seconds=0)},
        {"registry_running_staleness": timedelta(minutes=5)},
        {"scrape_run_running_staleness": timedelta(minutes=5)},
        {"shutdown_grace_period": timedelta(seconds=15)},
        {"startup_candidate_limit": 0},
    ],
)
def test_unsafe_threshold_relationships_are_rejected(replacement):
    values = dict(
        maximum_attempt_duration=timedelta(minutes=30),
        registry_running_staleness=timedelta(minutes=30),
        scrape_run_running_staleness=timedelta(minutes=30),
        shutdown_grace_period=timedelta(minutes=2),
        heartbeat_interval=timedelta(seconds=15),
    )
    values.update(replacement)
    with pytest.raises(ValueError):
        RecoverySettings(**values).validate(lease_duration=timedelta(minutes=15))


def test_maximum_attempt_must_cover_lease_duration():
    with pytest.raises(ValueError, match="lease"):
        settings().validate(lease_duration=timedelta(hours=1))


def test_startup_reconciliation_bounds_uncorrelated_candidates(recovery_db):
    path, lane = recovery_db
    conn = sqlite3.connect(path)
    for index in range(3):
        conn.execute(
            """INSERT INTO scheduler_job_registry(
            job_id,job_type,status,last_attempt_time,created_at,updated_at)
            VALUES(?, 'legacy', 'running', ?, ?, ?)""",
            (f"legacy-{index}", OLD, OLD, OLD),
        )
    conn.commit()
    conn.close()
    bounded = RecoverySettings(
        maximum_attempt_duration=timedelta(minutes=30),
        registry_running_staleness=timedelta(minutes=30),
        scrape_run_running_staleness=timedelta(minutes=30),
        shutdown_grace_period=timedelta(minutes=2),
        heartbeat_interval=timedelta(seconds=15),
        startup_candidate_limit=1,
    )
    report = reconcile_interrupted_attempts(
        trigger_source="startup",
        now=NOW,
        settings=bounded,
        window_settings=MatchWindowSettings(policy_version="v1"),
        lane=lane,
        run_id="bounded-startup",
    )
    assert report.compatibility_records == 1
    assert report.inspected_attempts == 1
    assert len(fetch(path, "SELECT job_id FROM scheduler_job_registry WHERE status='running'")) == 3
