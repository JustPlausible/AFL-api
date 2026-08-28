"""Service-layer tests for the Admin operations/data-health dashboard (Issue #225)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from db.migration_runner import migrate_database
from operations.dashboard import HealthState, OperationsDashboardReporter, Severity


NOW = datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    path = tmp_path / "ops.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_season(conn, *, is_current=1, current_round_number=1, year=2026, season_id=85):
    now = NOW.isoformat()
    conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}',?)", (now,))
    conn.execute(
        "INSERT INTO afl_seasons VALUES(?,?,1,?,?,?,?,?,NULL,NULL,'{}','{}',?)",
        (season_id, f"CD_S{season_id}", str(year), str(year), year, is_current, current_round_number, now),
    )
    for team, provider in ((10, "CD_T1"), (11, "CD_T2")):
        conn.execute(
            "INSERT INTO afl_teams VALUES(?,?,?,?,?,?,?,?,?, '{}','{}','{}',?)",
            (team, provider, season_id, provider, provider, provider, provider, provider, "AFL", now),
        )
        conn.execute("INSERT INTO afl_team_seasons VALUES(?,?,?,?)", (season_id, team, now, now))
    conn.commit()


def _seed_round(conn, *, round_id, round_number, season_id=85, label=None):
    conn.execute(
        "INSERT INTO rounds(round_id,round_label,season_id,competition_id,provider_id,round_number) "
        "VALUES(?,?,?,1,?,?)",
        (round_id, label or f"Round {round_number}", season_id, f"CD_R{round_id}", round_number),
    )
    conn.commit()


def _seed_match(conn, *, match_id, round_id, status, start_time, season_id=85, provider_id=None):
    conn.execute(
        "INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,status,"
        "start_time_utc,season_id,home_team_id,away_team_id) VALUES(?,?,?,'A','B','MCG',?,?,?,10,11)",
        (match_id, provider_id, round_id, status, start_time, season_id),
    )
    conn.commit()


def _registry_row(conn, *, job_id, job_type, status, round_id=None, match_id=None,
                   last_success_time=None, last_attempt_time=None, scheduled_run_time=None,
                   last_error_summary=None):
    conn.execute(
        "INSERT INTO scheduler_job_registry(job_id,job_type,match_id,round_id,scheduled_run_time,status,"
        "last_attempt_time,last_success_time,last_error_summary,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (job_id, job_type, match_id, None if round_id is None else str(round_id), scheduled_run_time, status,
         last_attempt_time, last_success_time, last_error_summary, NOW.isoformat()),
    )
    conn.commit()


def _report(conn, *, scheduler_health=None):
    return OperationsDashboardReporter(conn, clock=lambda: NOW, database="ops.db").report(scheduler_health=scheduler_health)


def _dataset(report_result, key):
    return next(d for d in report_result.datasets if d.key == key)


HEALTHY_SCHEDULER = {
    "available": True, "state": "healthy", "scheduler_running": True,
    "database_accessible": True, "registry_accessible": True, "job_count": 3,
    "diagnostics": [], "version": "0.7.1",
}


def test_no_current_season_is_unknown_not_failed(tmp_path):
    conn = _db(tmp_path)
    report = _report(conn, scheduler_health=HEALTHY_SCHEDULER)

    assert report.overview.season is None
    assert _dataset(report, "seasons").state is HealthState.UNKNOWN
    assert report.overview.overall_state is HealthState.ATTENTION
    assert any(item.code == "season.no_current_season" for item in report.attention)


def test_healthy_season_with_no_matches_yet_is_not_a_failure(tmp_path):
    conn = _db(tmp_path)
    _seed_season(conn, current_round_number=None)

    report = _report(conn, scheduler_health=HEALTHY_SCHEDULER)

    season_ds = _dataset(report, "seasons")
    assert season_ds.state is HealthState.HEALTHY
    stats_ds = _dataset(report, "player_statistics")
    assert stats_ds.state is HealthState.UPCOMING
    assert "not yet expected" in stats_ds.summary


def test_upcoming_round_rosters_and_commentary_are_upcoming_not_missing(tmp_path):
    conn = _db(tmp_path)
    _seed_season(conn, current_round_number=1)
    _seed_round(conn, round_id=101, round_number=1)
    future = (NOW + timedelta(days=5)).isoformat()
    _seed_match(conn, match_id=8001, round_id=101, status="SCHEDULED", start_time=future, provider_id="CD_M1")

    report = _report(conn, scheduler_health=HEALTHY_SCHEDULER)

    assert report.overview.season.round.window_state == "upcoming"
    rosters = _dataset(report, "rosters")
    assert rosters.state is HealthState.UPCOMING
    commentary = _dataset(report, "commentary")
    assert commentary.state is HealthState.UPCOMING
    assert "not yet expected" in commentary.summary
    interchange = _dataset(report, "interchange")
    assert interchange.state is HealthState.UPCOMING
    # Legitimately-not-yet-expected data must never appear as an attention item.
    assert not any(item.code.startswith("dataset.rosters") or item.code.startswith("dataset.commentary")
                   for item in report.attention)


def test_concluded_match_missing_commentary_is_attention(tmp_path):
    conn = _db(tmp_path)
    _seed_season(conn, current_round_number=1)
    _seed_round(conn, round_id=101, round_number=1)
    past = (NOW - timedelta(days=2)).isoformat()
    _seed_match(conn, match_id=8001, round_id=101, status="CONCLUDED", start_time=past, provider_id="CD_M1")

    report = _report(conn, scheduler_health=HEALTHY_SCHEDULER)

    commentary = _dataset(report, "commentary")
    assert commentary.state is HealthState.ATTENTION
    assert any(item.code == "dataset.commentary" for item in report.attention)


def test_concluded_match_with_commentary_is_healthy(tmp_path):
    conn = _db(tmp_path)
    _seed_season(conn, current_round_number=1)
    _seed_round(conn, round_id=101, round_number=1)
    past = (NOW - timedelta(days=2)).isoformat()
    _seed_match(conn, match_id=8001, round_id=101, status="CONCLUDED", start_time=past, provider_id="CD_M1")
    conn.execute(
        "INSERT INTO match_commentary_events(match_id,match_provider_id,event_fingerprint,slot_key,source_index,"
        "first_observed_at,last_observed_at,raw_event_json,collector_version) "
        "VALUES(8001,'CD_M1','fp1','slot1',0,?,?,'{}','v1')",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()

    report = _report(conn, scheduler_health=HEALTHY_SCHEDULER)

    commentary = _dataset(report, "commentary")
    assert commentary.state is HealthState.HEALTHY


def test_roster_job_success_is_healthy_and_failure_is_attention(tmp_path):
    conn = _db(tmp_path)
    _seed_season(conn, current_round_number=1)
    _seed_round(conn, round_id=101, round_number=1)
    near = (NOW + timedelta(hours=2)).isoformat()
    _seed_match(conn, match_id=8001, round_id=101, status="SCHEDULED", start_time=near, provider_id="CD_M1")
    _registry_row(conn, job_id="lineups_match_8001", job_type="lineup", status="succeeded",
                  match_id=8001, last_success_time=NOW.isoformat())

    healthy_report = _report(conn, scheduler_health=HEALTHY_SCHEDULER)
    assert _dataset(healthy_report, "rosters").state is HealthState.HEALTHY

    conn.execute("UPDATE scheduler_job_registry SET status='failed', last_error_summary='timeout' WHERE job_id='lineups_match_8001'")
    conn.commit()
    failed_report = _report(conn, scheduler_health=HEALTHY_SCHEDULER)
    rosters = _dataset(failed_report, "rosters")
    assert rosters.state is HealthState.FAILED
    assert any(item.code == "dataset.rosters" and item.severity is Severity.ERROR for item in failed_report.attention)


def test_injuries_missing_stale_and_fresh_are_distinguishable(tmp_path):
    conn = _db(tmp_path)
    conn.commit()
    missing_report = _report(conn)
    assert _dataset(missing_report, "injuries").state is HealthState.MISSING

    conn.execute(
        "INSERT INTO injuries(afl_id,club,player_name,injury,updated,scraped_at,current) "
        "VALUES(1,'ADE','Test Player','Hamstring','2026-01-01',?,1)",
        ((NOW - timedelta(hours=48)).isoformat(),),
    )
    conn.commit()
    stale_report = _report(conn)
    assert _dataset(stale_report, "injuries").state is HealthState.STALE

    conn.execute("UPDATE injuries SET scraped_at=?", ((NOW - timedelta(hours=2)).isoformat(),))
    conn.commit()
    fresh_report = _report(conn)
    assert _dataset(fresh_report, "injuries").state is HealthState.HEALTHY


def test_scheduler_job_type_activity_summary_reports_failure_state(tmp_path):
    conn = _db(tmp_path)
    _registry_row(conn, job_id="injuries_daily", job_type="injury", status="succeeded",
                  last_success_time=NOW.isoformat())
    _registry_row(conn, job_id="fixtures_daily", job_type="fixture", status="failed",
                  last_attempt_time=NOW.isoformat(), last_error_summary="upstream 500")

    report = _report(conn, scheduler_health=HEALTHY_SCHEDULER)

    by_type = {row.job_type: row for row in report.scheduler_activity}
    assert by_type["injury"].state is HealthState.HEALTHY
    assert by_type["fixture"].state is HealthState.FAILED
    assert report.overview.failing_job_types == 1
    assert any(item.code.startswith("scheduler.job_type_failed:fixture") for item in report.attention)


def test_scheduler_unavailable_maps_to_unknown_and_drives_attention(tmp_path):
    conn = _db(tmp_path)
    report = _report(conn, scheduler_health={"available": False})

    assert report.overview.scheduler_state is HealthState.UNKNOWN
    assert report.overview.scheduler_label == "Unavailable"


def test_scheduler_unhealthy_maps_to_failed_overall_state(tmp_path):
    conn = _db(tmp_path)
    _seed_season(conn, current_round_number=None)
    report = _report(conn, scheduler_health={
        "available": True, "state": "unhealthy", "scheduler_running": True,
        "database_accessible": False, "registry_accessible": True, "job_count": 0,
        "diagnostics": ["database_unavailable"], "version": "0.7.1",
    })

    assert report.overview.scheduler_state is HealthState.FAILED
    assert report.overview.overall_state is HealthState.FAILED


def test_attention_list_is_bounded(tmp_path):
    conn = _db(tmp_path)
    _seed_season(conn, current_round_number=1)
    _seed_round(conn, round_id=101, round_number=1)
    past = (NOW - timedelta(days=10)).isoformat()
    for i in range(40):
        match_id = 9000 + i
        _seed_match(conn, match_id=match_id, round_id=101, status="CONCLUDED", start_time=past,
                    provider_id=f"CD_M{match_id}")

    report = _report(conn, scheduler_health=HEALTHY_SCHEDULER)

    assert len(report.attention) <= 25
    assert report.overview.attention_count >= len(report.attention)


def test_drill_down_links_target_existing_admin_routes(tmp_path):
    conn = _db(tmp_path)
    _seed_season(conn, current_round_number=1)
    _seed_round(conn, round_id=101, round_number=1)
    past = (NOW - timedelta(days=2)).isoformat()
    _seed_match(conn, match_id=8001, round_id=101, status="CONCLUDED", start_time=past, provider_id="CD_M1")

    report = _report(conn, scheduler_health=HEALTHY_SCHEDULER)

    for dataset in report.datasets:
        if dataset.link is not None:
            assert dataset.link.startswith("/")
    for item in report.attention:
        if item.link is not None:
            assert item.link.startswith("/")
    assert _dataset(report, "seasons").link == "/season-review?season=2026"
