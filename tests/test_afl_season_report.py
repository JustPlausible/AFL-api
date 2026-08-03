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
             provider_id="CD_M1", membership_team=10, player_count=20):
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
    for player in range(1, player_count + 1):
        cd, afl = f"CD_I{player}", str(player)
        team = membership_team if player == 1 else (10 if player <= player_count // 2 else 11)
        conn.execute("INSERT INTO canonical_players VALUES(?,?,?,?,?,?)",
                     (player, f"Player {player}", "Player", str(player), now, now))
        for provider, value in (("champion_data", cd), ("afl", afl)):
            conn.execute("INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                         (player, provider, value, now, now))
        conn.execute("INSERT INTO competition_season_players(player_id,competition_season_id,team_id,source_provider,source_json,created_at,updated_at) VALUES(?,85,?,'champion_data','{}',?,?)",
                     (player, team, now, now))
    if stats:
        sides = tuple(
            (player, f"CD_I{player}", "CD_T1" if one_sided or player <= player_count // 2
             else "CD_T2", "home" if one_sided or player <= player_count // 2 else "away")
            for player in range(1, player_count + 1)
        )
        for player, cd, team, side in sides:
            conn.execute("INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,afl_match_id,team_provider_id,side,collected_at,source_endpoint,resolved_match_status,snapshot_authority,extra_stats_json,raw_player_json,canonical_player_id) VALUES(?,?,'8001',?,?,?,'match_player_stats','CONCLUDED',2,'{}','{}',?)",
                         (provider_id, cd, team, side, now, player))
    conn.commit()
    return path, conn


def report(conn):
    return SeasonCompletenessReporter(conn, clock=lambda: NOW, database="report.db").report(2026)


def codes(value):
    return {item.code for item in value.findings}


def set_match_participants(conn, *, status, home_id, away_id, placeholder,
                           start="2026-09-26T04:30:00+00:00"):
    def context(team_id, name):
        team = {"id": team_id, "providerId": f"CD_T{team_id}", "name": name,
                "abbreviation": "TBD" if placeholder else "OUT",
                "nickname": "TBD" if placeholder else "Outsider", "teamType": "MEN"}
        return json.dumps({"team": team}, sort_keys=True)

    conn.execute(
        "UPDATE matches SET status=?,start_time_utc=?,home_team_id=?,away_team_id=?,"
        "home_team=?,away_team=?,home_json=?,away_json=? WHERE match_id=8001",
        (status, start, home_id, away_id, "TBD" if placeholder else "Outsider A",
         "TBD" if placeholder else "Outsider B", context(home_id, "Winner of PF1"),
         context(away_id, "Winner of PF2")),
    )
    conn.commit()


def test_complete_finished_season_is_deterministic_and_legacy_is_not_authority(tmp_path):
    _, conn = database(tmp_path)
    first = report(conn)
    second = report(conn)
    assert first.to_dict() == second.to_dict()
    assert first.status is ReportStatus.COMPLETE
    assert first.metadata.generated_at == NOW.isoformat()
    assert first.aggregates["authoritative_stat_rows"] == 20
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


@pytest.mark.parametrize("status", ["PLACEHOLDER", "SCHEDULED"])
def test_future_tbd_participants_are_informational_for_any_nonconcluded_status(
        tmp_path, status):
    _, conn = database(tmp_path, status=status, stats=False)
    set_match_participants(conn, status=status, home_id=156, away_id=160, placeholder=True)

    value = report(conn)

    finding = next(item for item in value.findings
                   if item.code == "match.participants_unpublished")
    assert finding.severity.value == "info"
    assert finding.observed == {"placeholder_sides": ("home", "away"), "status": status}
    assert "match.missing_team" not in codes(value)
    assert value.status is ReportStatus.COMPLETE
    assert exit_code(value.status) == 0


def test_scheduled_match_with_participating_teams_has_no_participant_finding(tmp_path):
    _, conn = database(tmp_path, status="SCHEDULED", stats=False)

    value = report(conn)

    assert "match.participants_unpublished" not in codes(value)
    assert "match.missing_team" not in codes(value)
    assert value.status is ReportStatus.COMPLETE


def test_concluded_match_with_tbd_participants_is_invalid(tmp_path):
    _, conn = database(tmp_path)
    set_match_participants(conn, status="CONCLUDED", home_id=156, away_id=160,
                           placeholder=True, start="2026-07-01T00:00:00+00:00")

    value = report(conn)

    assert "match.participants_unpublished" not in codes(value)
    assert "match.missing_team" in codes(value)
    assert value.status is ReportStatus.INVALID


def test_nonplaceholder_teams_outside_season_are_invalid(tmp_path):
    _, conn = database(tmp_path, status="SCHEDULED", stats=False)
    set_match_participants(conn, status="SCHEDULED", home_id=901, away_id=902,
                           placeholder=False)

    value = report(conn)

    assert "match.participants_unpublished" not in codes(value)
    assert "match.missing_team" in codes(value)
    assert value.status is ReportStatus.INVALID


def test_partial_unresolved_and_conflicting_crosswalk_findings(tmp_path):
    _, conn = database(tmp_path, one_sided=True)
    conn.execute("UPDATE cfs_player_stats SET canonical_player_id=2 WHERE champion_data_player_id='CD_I1'")
    conn.execute("INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,afl_match_id,team_provider_id,side,collected_at,source_endpoint,resolved_match_status,snapshot_authority,extra_stats_json,raw_player_json,canonical_player_id) VALUES('CD_M1','CD_UNKNOWN','8001','CD_T1','home',?,'match_player_stats','CONCLUDED',2,'{}','{}',NULL)", (NOW.isoformat(),))
    conn.commit()
    value = report(conn)
    assert {"match.partial_authoritative_stats", "stats.unresolved_canonical_player",
            "player.provider_mapping_conflict"} <= codes(value)
    assert value.status is ReportStatus.INVALID
    assert exit_code(value.status) == 1


def test_two_sided_below_conservative_floor_is_incomplete(tmp_path):
    _, conn = database(tmp_path, player_count=2)
    value = report(conn)
    finding = next(item for item in value.findings
                   if item.code == "stats.suspicious_player_count")
    assert finding.observed == {"total": 2, "home": 1, "away": 1}
    assert finding.expected == {"minimum_total": 20}
    assert value.aggregates["concluded_matches_with_suspicious_player_count"] == 1
    assert value.status is ReportStatus.INCOMPLETE
    assert exit_code(value.status) == 1


def test_one_sided_authoritative_rows_are_incomplete(tmp_path):
    _, conn = database(tmp_path, one_sided=True)
    value = report(conn)
    assert "match.partial_authoritative_stats" in codes(value)
    assert "stats.suspicious_player_count" not in codes(value)
    assert value.aggregates["concluded_matches_with_partial_stats"] == 1
    assert value.status is ReportStatus.INCOMPLETE


def test_team_provider_context_mismatch_and_unavailable_are_distinct(tmp_path):
    _, conn = database(tmp_path)
    conn.execute("UPDATE cfs_player_stats SET team_provider_id=NULL "
                 "WHERE champion_data_player_id='CD_I2'")
    conn.commit()
    unavailable = report(conn)
    assert "stats.team_provider_unavailable" in codes(unavailable)
    assert sum(item.code == "stats.team_provider_unavailable"
               for item in unavailable.findings) == 1
    assert unavailable.aggregates[
        "authoritative_stat_rows_with_unavailable_team_context"] == 1
    assert unavailable.aggregates["matches_with_unavailable_team_context"] == 1
    assert "stats.team_participant_mismatch" not in codes(unavailable)
    assert unavailable.status is ReportStatus.COMPLETE
    conn.execute("UPDATE cfs_player_stats SET team_provider_id='CD_T2' "
                 "WHERE champion_data_player_id='CD_I1'")
    conn.commit()
    value = report(conn)
    assert "stats.team_participant_mismatch" in codes(value)
    assert "stats.team_provider_unavailable" in codes(value)
    assert value.status is ReportStatus.INVALID


def test_systematically_null_team_context_is_one_aggregate_finding_per_match(tmp_path):
    _, conn = database(tmp_path)
    conn.execute("UPDATE cfs_player_stats SET team_provider_id=NULL")
    conn.commit()
    value = report(conn)
    findings = [item for item in value.findings
                if item.code == "stats.team_provider_unavailable"]
    assert len(findings) == 1
    assert findings[0].observed == {"unavailable_rows": 20, "home": 10, "away": 10}
    assert value.aggregates["authoritative_stat_rows_with_unavailable_team_context"] == 20
    assert value.aggregates["matches_with_unavailable_team_context"] == 1
    assert value.status is ReportStatus.COMPLETE


def test_authoritative_player_without_season_membership_is_incomplete(tmp_path):
    _, conn = database(tmp_path)
    conn.execute("DELETE FROM competition_season_players WHERE player_id=1")
    conn.commit()
    value = report(conn)
    finding = next(item for item in value.findings
                   if item.code == "stats.player_missing_season_membership")
    assert (finding.player_id, finding.match_id) == (1, 8001)
    assert finding.severity.value == "warning"
    assert value.status is ReportStatus.INCOMPLETE


def test_historical_stats_for_continuing_player_are_not_a_2026_finding(tmp_path):
    _, conn = database(tmp_path)
    now = NOW.isoformat()
    conn.execute("INSERT INTO afl_seasons VALUES(84,'CD_S84',1,'2025','2025',2025,0,24,NULL,NULL,'{}','{}',?)", (now,))
    for team, provider in ((12, "CD_T2025A"), (13, "CD_T2025B")):
        conn.execute("INSERT INTO afl_teams VALUES(?,?,84,?,?,?,?,?,?, '{}','{}','{}',?)",
                     (team, provider, provider, provider, provider, provider, provider, "AFL", now))
        conn.execute("INSERT INTO afl_team_seasons VALUES(84,?,?,?)", (team, now, now))
    conn.execute("INSERT INTO rounds(round_id,round_label,season_id,competition_id,provider_id,round_number) VALUES(100,'Round 1',84,1,'CD_R2025',1)")
    conn.execute("INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,status,start_time_utc,season_id,home_team_id,away_team_id) VALUES(7001,'CD_M2025',100,'A','B','MCG','CONCLUDED','2025-03-01T00:00:00+00:00',84,12,13)")
    conn.execute("INSERT INTO competition_season_players(player_id,competition_season_id,team_id,source_provider,source_json,created_at,updated_at) VALUES(1,84,12,'champion_data','{}',?,?)", (now, now))
    conn.execute("INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,afl_match_id,team_provider_id,side,collected_at,source_endpoint,resolved_match_status,snapshot_authority,extra_stats_json,raw_player_json,canonical_player_id) VALUES('CD_M2025','CD_I1','7001','CD_T2025A','home',?,'match_player_stats','CONCLUDED',2,'{}','{}',1)", (now,))
    conn.commit()

    value = report(conn)

    assert "stats.match_outside_season" not in codes(value)
    assert "stats.player_missing_season_membership" not in codes(value)
    assert value.status is ReportStatus.COMPLETE


def test_duplicate_match_provider_identity_is_defensively_invalid(tmp_path):
    _, conn = database(tmp_path)
    conn.execute("DROP INDEX idx_matches_provider_id")
    conn.execute("INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,"
                 "venue,status,start_time_utc,season_id,home_team_id,away_team_id) "
                 "VALUES(8002,'CD_M1',101,'A','B','MCG','SCHEDULED',"
                 "'2026-03-02T00:00:00+00:00',85,10,11)")
    conn.commit()
    value = report(conn)
    finding = next(item for item in value.findings if item.code == "match.duplicate_provider_id")
    assert finding.severity.value == "error"
    assert finding.observed["occurrences"] == 2
    assert value.status is ReportStatus.INVALID


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
