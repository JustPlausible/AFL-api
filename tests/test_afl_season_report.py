from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

import cli
import cli_runtime
from afl_json.season_report import (ReportStatus, SeasonCompletenessReporter,
                                    calculate_status, exit_code, render_human)
from db.migration_runner import migrate_database


NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def database(tmp_path, *, status="CONCLUDED", stats=True, one_sided=False,
             provider_id="CD_M1", membership_team=10):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "report.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    now = NOW.isoformat()
    conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}',?)", (now,))
    conn.execute("INSERT INTO afl_seasons VALUES(85,'CD_S85',1,'2026','2026',2026,1,1,NULL,NULL,'{}','{}',?)", (now,))
    for team, provider in ((10, "CD_T1"), (11, "CD_T2")):
        conn.execute("INSERT INTO afl_teams VALUES(?,?,85,?,?,?,?,?,?, '{}','{}','{}',?)",
                     (team, provider, provider, provider, provider, provider, provider, "AFL", now))
        conn.execute("INSERT INTO afl_team_seasons VALUES(85,?,?,?)", (team, now, now))
    conn.execute("INSERT INTO rounds(round_id,round_label,season_id,competition_id,provider_id,round_number) VALUES(101,'Round 1',85,1,'CD_R1',1)")
    conn.execute("INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,status,start_time_utc,season_id,home_team_id,away_team_id) VALUES(8001,?,101,'A','B','MCG',?,'2026-03-01T00:00:00+00:00',85,10,11)", (provider_id, status))
    for player, cd, afl, team in ((1, "CD_I1", "1", membership_team), (2, "CD_I2", "2", 11)):
        conn.execute("INSERT INTO canonical_players VALUES(?,?,?,?,?,?)",
                     (player, f"Player {player}", "Player", str(player), now, now))
        for provider, value in (("champion_data", cd), ("afl", afl)):
            conn.execute("INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                         (player, provider, value, now, now))
        conn.execute("INSERT INTO competition_season_players(player_id,competition_season_id,team_id,source_provider,source_json,created_at,updated_at) VALUES(?,85,?,'champion_data','{}',?,?)",
                     (player, team, now, now))
    if stats:
        sides = ((1, "CD_I1", "CD_T1", "home"),) if one_sided else (
            (1, "CD_I1", "CD_T1", "home"), (2, "CD_I2", "CD_T2", "away"))
        for player, cd, team, side in sides:
            conn.execute("INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,afl_match_id,team_provider_id,side,collected_at,source_endpoint,resolved_match_status,snapshot_authority,extra_stats_json,raw_player_json,canonical_player_id) VALUES(?,?,'8001',?,?,?,'match_player_stats','CONCLUDED',2,'{}','{}',?)",
                         (provider_id, cd, team, side, now, player))
    conn.commit()
    return path, conn


def report(conn):
    return SeasonCompletenessReporter(conn, clock=lambda: NOW, database="report.db").report(2026)


def codes(value):
    return {item.code for item in value.findings}


def test_complete_finished_season_is_deterministic_and_legacy_is_not_authority(tmp_path):
    _, conn = database(tmp_path)
    first = report(conn)
    second = report(conn)
    assert first.to_dict() == second.to_dict()
    assert first.status is ReportStatus.COMPLETE
    assert first.metadata.generated_at == NOW.isoformat()
    assert first.aggregates["authoritative_stat_rows"] == 2
    assert exit_code(first.status) == 0
    conn.execute("DELETE FROM cfs_player_stats")
    conn.execute("INSERT INTO player_stats(match_id,player_name,team_code,status,scraped_at) VALUES(8001,'Legacy','A','COMPLETED',?)", (NOW.isoformat(),))
    conn.commit()
    legacy = report(conn)
    assert legacy.aggregates["legacy_stat_rows"] == 1
    assert "match.final_without_authoritative_stats" in codes(legacy)
    assert legacy.status is ReportStatus.INCOMPLETE


def test_future_match_nullable_membership_and_missing_provider_are_classified(tmp_path):
    _, conn = database(tmp_path, status="SCHEDULED", stats=False, provider_id=None,
                       membership_team=None)
    value = report(conn)
    by_code = {item.code: item for item in value.findings}
    assert by_code["membership.missing_team"].severity.value == "info"
    assert by_code["match.missing_provider_id"].severity.value == "info"
    assert "match.final_without_authoritative_stats" not in by_code
    assert value.status is ReportStatus.COMPLETE


def test_partial_unresolved_conflict_and_outside_season_findings(tmp_path):
    _, conn = database(tmp_path, one_sided=True)
    conn.execute("UPDATE cfs_player_stats SET canonical_player_id=2 WHERE champion_data_player_id='CD_I1'")
    conn.execute("INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,afl_match_id,team_provider_id,side,collected_at,source_endpoint,resolved_match_status,snapshot_authority,extra_stats_json,raw_player_json,canonical_player_id) VALUES('CD_M1','CD_UNKNOWN','8001','CD_T1','home',?,'match_player_stats','CONCLUDED',2,'{}','{}',NULL)", (NOW.isoformat(),))
    conn.execute("INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,afl_match_id,team_provider_id,side,collected_at,source_endpoint,resolved_match_status,snapshot_authority,extra_stats_json,raw_player_json,canonical_player_id) VALUES('CD_OUT','CD_I1','9001','CD_T1','home',?,'match_player_stats','CONCLUDED',2,'{}','{}',1)", (NOW.isoformat(),))
    conn.commit()
    value = report(conn)
    assert {"match.partial_authoritative_stats", "stats.unresolved_canonical_player",
            "player.provider_mapping_conflict", "stats.match_outside_season"} <= codes(value)
    assert value.status is ReportStatus.INVALID
    assert exit_code(value.status) == 1


def test_missing_provider_and_failed_audit_statuses(tmp_path):
    _, conn = database(tmp_path, stats=False, provider_id=None)
    value = report(conn)
    assert value.status is ReportStatus.INCOMPLETE
    assert "match.missing_provider_id" in codes(value)
    _, conn2 = database(tmp_path / "other")
    conn2.execute("INSERT INTO scrape_runs(run_id,scrape_type,target_type,target_identifier,trigger_source,status,started_at) VALUES('r','season_match_player_stats','match','CD_M1','cli','failed',?)", (NOW.isoformat(),))
    conn2.commit()
    warning = report(conn2)
    assert warning.status is ReportStatus.USABLE_WITH_WARNINGS
    assert "audit.latest_run_failed_or_partial" in codes(warning)
    assert exit_code(warning.status) == 0


def test_report_is_read_only_and_human_json_share_findings(tmp_path):
    _, conn = database(tmp_path)
    before = conn.serialize()
    value = report(conn)
    payload = value.to_dict()
    human = render_human(value)
    assert conn.serialize() == before
    assert all(item["code"] in human for item in payload["findings"])
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("PRAGMA query_only=ON")
        conn.execute("DELETE FROM matches")


def test_cli_modes_and_exit_codes_use_same_report(monkeypatch, capsys, tmp_path):
    path, conn = database(tmp_path, stats=False)
    conn.close()
    monkeypatch.setattr("db.connection.get_db_path", lambda: path)
    args = cli.handle_args(["--report-afl-season", "2026", "--print-json"])
    with pytest.raises(SystemExit, match="1"):
        cli_runtime.handle_report_afl_season(args)
    payload = json.loads(capsys.readouterr().out)
    json_codes = {item["code"] for item in payload["findings"]}
    args = cli.handle_args(["--report-afl-season", "2026"])
    with pytest.raises(SystemExit, match="1"):
        cli_runtime.handle_report_afl_season(args)
    human = capsys.readouterr().out
    assert all(code in human for code in json_codes)


def test_status_decision_table_uses_codes_not_only_severity(tmp_path):
    _, conn = database(tmp_path)
    value = report(conn)
    assert calculate_status([]) is ReportStatus.COMPLETE
    assert value.status is ReportStatus.COMPLETE
