from __future__ import annotations

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from afl_json import (CollectionResult, PlayerPersistenceSummary,
                      PlayerStatsStatus, normalise_player_stats, persist_afl_metadata)
from afl_json.client import AflJsonResourceUnavailable
from afl_json.season_sync import (SeasonBootstrapResult, SeasonSynchronizer,
                                  SeasonSyncOptions)
from db.migration_runner import migrate_database
from db.scrape_runs import complete_scrape_run, start_scrape_run


def metadata(statuses=("CONCLUDED", "SCHEDULED", "CONCLUDED"), *, missing_provider=True,
             start_times=None):
    matches = []
    for index, status in enumerate(statuses, 1):
        matches.append({
            "afl_id": 8000 + index,
            "provider_id": None if missing_provider and index == 3 else f"CD_M{index}",
            "status": status, "round": {"id": 100 + index},
            "home": {}, "away": {}, "home_score": None, "away_score": None,
            "venue": None,
            "utc_start_time": start_times[index - 1] if start_times else None,
            "metadata": None, "source": {},
        })
    return CollectionResult(
        {"afl_id": 1, "provider_id": "CD_C1", "code": "AFL", "name": "AFL",
         "metadata": None, "source": {}},
        {"afl_id": 85, "provider_id": "CD_S85", "name": "2026", "short_name": "2026",
         "year": 2026, "current": True, "current_round_number": 3,
         "start_time": None, "end_time": None, "metadata": None, "source": {}},
        [{"afl_id": 100 + i, "provider_id": f"CD_R{i}", "name": f"Round {i}",
          "round_number": i, "abbreviation": f"R{i}", "start_time": None,
          "end_time": None, "byes": [], "metadata": None, "source": {}}
         for i in range(1, len(statuses) + 1)], [], matches,
    )


def concluded(provider="CD_M1", goals=1, collected_at="2026-01-01T00:00:00+00:00"):
    payload = {"status": "CONCLUDED", "homeTeamPlayerStats": [{
        "player": {"teamId": "CD_T1", "player": {"player": {"player": {
            "playerId": "CD_I1"}}}},
        "playerStats": {"stats": {"goals": goals}},
    }], "awayTeamPlayerStats": []}
    return normalise_player_stats(payload, provider, collected_at=collected_at,
                                  afl_match_id=8001,
                                  canonical_match_status="CONCLUDED")


class Collector:
    results = {}
    calls = []

    def __init__(self, _client, **_kwargs): pass

    def collect(self, provider, **_kwargs):
        self.calls.append(provider)
        value = self.results.get(provider, concluded(provider))
        if isinstance(value, Exception):
            raise value
        return value


def setup(tmp_path, statuses=("CONCLUDED", "SCHEDULED", "CONCLUDED"), *,
          missing_provider=True, client=None, start_times=None, sync_kwargs=None):
    path = tmp_path / "sync.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    collected = metadata(statuses, missing_provider=missing_provider, start_times=start_times)

    def bootstrap(_client, db, **_kwargs):
        summary = persist_afl_metadata(db, collected)
        players = PlayerPersistenceSummary("published", 0, 0, 0, 0, 0, 0, 0, 0)
        return SeasonBootstrapResult(1, "CD_C1", 85, "CD_S85", summary, players)

    Collector.calls = []
    return conn, SeasonSynchronizer(client or object(), conn, bootstrap=bootstrap,
                                    collector_factory=Collector, **(sync_kwargs or {}))


def test_first_run_rerun_and_refresh_are_idempotent(tmp_path):
    conn, sync = setup(tmp_path, ("CONCLUDED",))
    Collector.results = {"CD_M1": concluded()}
    first = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")
    second = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")
    Collector.results = {"CD_M1": concluded(goals=2, collected_at="2026-01-02T00:00:00+00:00")}
    refreshed = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1",
                         options=SeasonSyncOptions(refresh_complete=True))

    assert (first.outcome, first.statistic_rows_inserted) == ("success", 1)
    assert (second.already_complete_unchanged, second.statistic_rows_inserted) == (1, 0)
    assert refreshed.statistic_rows_updated == 1
    assert tuple(conn.execute("SELECT COUNT(*),goals FROM cfs_player_stats").fetchone()) == (1, 2)
    assert conn.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0] == 0


def test_selection_states_bounds_and_missing_provider_are_distinct(tmp_path):
    conn, sync = setup(tmp_path)
    result = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")
    bounded = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1",
                       options=SeasonSyncOptions(round_number=2))

    assert result.total_matches_discovered == 3
    assert result.skipped_not_concluded == 1
    assert result.skipped_missing_provider_identity == 1
    assert bounded.total_matches_discovered == 1
    assert bounded.matches[0].round_number == 2


def test_unavailable_empty_partial_unknown_and_failure_are_isolated(tmp_path):
    conn, sync = setup(tmp_path, ("CONCLUDED", "CONCLUDED", "CONCLUDED"),
                       missing_provider=False)
    base = concluded()
    Collector.results = {
        "CD_M1": replace(base, match_provider_id="CD_M1", status=PlayerStatsStatus.UNAVAILABLE,
                          records=[]),
        "CD_M2": RuntimeError("Authorization: Bearer super-secret token=abc"),
        "CD_M3": concluded("CD_M3"),
    }
    result = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")

    assert result.outcome == "partial"
    assert (result.unavailable_unpublished, result.failed, result.collected_successfully) == (1, 1, 1)
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 1
    failure = next(match for match in result.matches if match.outcome == "failed")
    assert "super-secret" not in failure.error and "<redacted>" in failure.error
    correlations = conn.execute(
        "SELECT COUNT(DISTINCT correlation_id) FROM scrape_runs WHERE correlation_id=?",
        (result.correlation_id,),
    ).fetchone()[0]
    assert correlations == 1


def test_incomplete_authority_two_subset_is_recollected(tmp_path):
    conn, sync = setup(tmp_path, ("CONCLUDED",))
    full = concluded()
    second = replace(full.records[0], champion_data_player_id="CD_I2", side="away")
    full = replace(full, records=[*full.records, second])
    conn.execute(
        "INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,afl_match_id,"
        "team_provider_id,side,collected_at,source_endpoint,endpoint_source_status,"
        "resolved_match_status,snapshot_authority,extra_stats_json,raw_player_json) "
        "VALUES ('CD_M1','CD_I1','8001','CD_T1','home','2025-01-01','source',"
        "'CONCLUDED','CONCLUDED',2,'{}','{}')"
    )
    conn.commit()
    historical_audit = start_scrape_run(
        "match_player_stats", target_type="match", target_identifier="CD_M1", conn=conn
    )
    complete_scrape_run(historical_audit, rows_read=1, rows_written=1, conn=conn)
    Collector.results = {"CD_M1": full}

    result = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")

    assert Collector.calls == ["CD_M1"]
    assert result.already_complete_unchanged == 0
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 2


def test_qualifying_complete_audit_allows_default_skip(tmp_path):
    conn, sync = setup(tmp_path, ("CONCLUDED",))
    Collector.results = {"CD_M1": concluded()}
    sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")
    Collector.calls = []

    result = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")

    assert Collector.calls == []
    assert result.already_complete_unchanged == 1
    assert result.statistic_rows_unchanged == 1


def test_rejected_concluded_result_is_materially_partial(tmp_path):
    conn, sync = setup(tmp_path, ("CONCLUDED",))
    Collector.results = {"CD_M1": replace(concluded(), rejected_records=1)}

    result = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")

    assert result.outcome == "partial"
    assert result.partial == 1
    assert result.collected_successfully == 0
    assert [tuple(row) for row in conn.execute(
        "SELECT DISTINCT snapshot_authority FROM cfs_player_stats"
    )] == [(1,)]


class ConcludedDetailClient:
    def get(self, endpoint, **kwargs):
        assert endpoint == "match_detail"
        match_id = kwargs["path_parameters"]["afl_match_id"]
        return SimpleNamespace(data={"matches": [{
            "id": match_id, "providerId": "CD_M1", "status": "CONCLUDED",
        }]})


def test_stored_postgame_advances_before_eligibility_classification(tmp_path):
    conn, sync = setup(tmp_path, ("POSTGAME",), client=ConcludedDetailClient())
    Collector.results = {"CD_M1": concluded()}

    result = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")

    assert result.collected_successfully == 1
    assert result.skipped_not_concluded == result.partial == 0
    assert conn.execute("SELECT status FROM matches WHERE match_id=8001").fetchone()[0] == "CONCLUDED"


class NoDetailClient:
    def get(self, *_args, **_kwargs):
        raise AflJsonResourceUnavailable("not published", endpoint="match_detail")


def test_expected_season_lifecycle_skips_remain_success(tmp_path):
    future = "2099-01-01T00:00:00+00:00"
    conn, sync = setup(
        tmp_path, ("SCHEDULED", "LIVE", "POSTGAME", "MYSTERY"), missing_provider=False,
        client=NoDetailClient(), start_times=(future, future, future, future),
    )

    result = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")

    assert result.outcome == "success"
    assert (result.skipped_scheduled, result.skipped_live_or_postgame,
            result.skipped_future_placeholder) == (1, 2, 1)
    assert result.partial == result.unknown == result.unresolved_lifecycle == 0


def test_past_unknown_lifecycle_is_materially_partial(tmp_path):
    past = "2020-01-01T00:00:00+00:00"
    conn, sync = setup(tmp_path, ("MYSTERY",), missing_provider=False,
                       client=NoDetailClient(), start_times=(past,))

    result = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")

    assert result.outcome == "partial"
    assert result.unresolved_lifecycle == 1
    assert result.matches[0].outcome == "unknown_lifecycle"


def test_each_material_cfs_outcome_is_partial(tmp_path):
    statuses = (
        PlayerStatsStatus.UNAVAILABLE, PlayerStatsStatus.EMPTY,
        PlayerStatsStatus.LIVE_PARTIAL, PlayerStatsStatus.UNKNOWN,
    )
    for index, status in enumerate(statuses):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        conn, sync = setup(case_path, ("CONCLUDED",), missing_provider=False)
        base = concluded()
        Collector.results = {"CD_M1": replace(
            base, status=status,
            records=[] if status in {PlayerStatsStatus.UNAVAILABLE, PlayerStatsStatus.EMPTY} else base.records,
        )}
        result = sync.run(season=2026, competition_code="AFL",
                          competition_provider_id="CD_C1")
        assert result.outcome == "partial"
        conn.close()


def test_explicit_absent_and_unsatisfied_matches_are_partial(tmp_path):
    conn, sync = setup(tmp_path, ("SCHEDULED",), missing_provider=False)

    result = sync.run(
        season=2026, competition_code="AFL", competition_provider_id="CD_C1",
        options=SeasonSyncOptions(match_ids=(8001, 9999)),
    )

    assert result.outcome == "partial"
    assert result.explicit_matches_requested == result.explicit_matches_unsatisfied == 2
    assert result.missing_requested_match_ids == [9999]
    assert {match.match_id for match in result.matches} == {8001, 9999}


def test_explicit_lifecycle_and_identity_skips_are_unsatisfied(tmp_path):
    cases = [
        (("SCHEDULED",), False, 8001, None),
        (("LIVE",), False, 8001, NoDetailClient()),
        (("MYSTERY",), False, 8001, NoDetailClient()),
        (("CONCLUDED", "CONCLUDED", "CONCLUDED"), True, 8003, None),
    ]
    for index, (statuses, missing_provider, match_id, client) in enumerate(cases):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        conn, sync = setup(case_path, statuses, missing_provider=missing_provider,
                           client=client)
        result = sync.run(
            season=2026, competition_code="AFL", competition_provider_id="CD_C1",
            options=SeasonSyncOptions(match_ids=(match_id,)),
        )
        assert result.outcome == "partial"
        assert result.explicit_matches_unsatisfied == 1
        assert result.matches[-1].match_id == match_id
        conn.close()


def test_explicit_collected_and_already_complete_matches_succeed(tmp_path):
    conn, sync = setup(tmp_path, ("CONCLUDED",), missing_provider=False)
    Collector.results = {"CD_M1": concluded()}
    collected_result = sync.run(
        season=2026, competition_code="AFL", competition_provider_id="CD_C1",
        options=SeasonSyncOptions(match_ids=(8001,)),
    )
    complete_result = sync.run(
        season=2026, competition_code="AFL", competition_provider_id="CD_C1",
        options=SeasonSyncOptions(match_ids=(8001,)),
    )

    assert collected_result.outcome == complete_result.outcome == "success"
    assert collected_result.explicit_matches_unsatisfied == 0
    assert complete_result.already_complete_unchanged == 1


def test_concluded_missing_provider_is_materially_partial(tmp_path):
    conn, sync = setup(tmp_path, ("CONCLUDED", "CONCLUDED", "CONCLUDED"),
                       missing_provider=True)

    result = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")

    assert result.outcome == "partial"
    assert result.skipped_missing_provider_identity == 1


def test_empty_selection_contract_distinguishes_bounded_and_unbounded(tmp_path):
    conn, sync = setup(tmp_path, (), missing_provider=False)
    unbounded = sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")
    bounded = sync.run(
        season=2026, competition_code="AFL", competition_provider_id="CD_C1",
        options=SeasonSyncOptions(round_number=99),
    )
    empty_range = sync.run(
        season=2026, competition_code="AFL", competition_provider_id="CD_C1",
        options=SeasonSyncOptions(round_from=98, round_to=99),
    )

    assert (unbounded.selection_status, unbounded.outcome) == ("empty_unbounded", "success")
    assert (bounded.selection_status, bounded.outcome) == ("empty_bounded", "partial")
    assert (empty_range.selection_status, empty_range.outcome) == ("empty_bounded", "partial")


def test_committed_match_survives_audit_failure_and_later_match_runs(tmp_path):
    calls = 0

    def flaky_complete(run_id, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError(
                "Authorization: Bearer child-secret Cookie: session=hidden "
                "postgres://admin:db-password@example.test/data"
            )
        complete_scrape_run(run_id, **kwargs)

    conn, sync = setup(
        tmp_path, ("CONCLUDED", "CONCLUDED"), missing_provider=False,
        sync_kwargs={"complete_audit": flaky_complete},
    )
    Collector.results = {"CD_M1": concluded("CD_M1"), "CD_M2": concluded("CD_M2")}

    result = sync.run(season=2026, competition_code="AFL",
                      competition_provider_id="CD_C1")

    first, second = result.matches
    assert result.outcome == "partial"
    assert (result.statistic_rows_inserted, result.statistic_rows_updated,
            result.statistic_rows_unchanged) == (2, 0, 0)
    assert tuple(conn.execute(
        "SELECT COUNT(*),COUNT(DISTINCT match_provider_id) FROM cfs_player_stats"
    ).fetchone()) == (2, 2)
    assert (first.outcome, first.collection_outcome, first.persistence_outcome,
            first.rows_inserted, first.rows_updated, first.rows_unchanged,
            first.rows_written) == ("collected", "concluded", "committed", 1, 0, 0, 1)
    assert first.audit_outcome == "failed" and first.processing_continued is True
    assert first.audit_id and first.correlation_id == result.correlation_id
    assert second.persistence_outcome == "committed" and second.audit_outcome == "completed"
    diagnostic = f"{first.audit_error_class} {first.audit_error_summary}"
    assert "OperationalError" in diagnostic
    assert all(secret not in diagnostic for secret in
               ("child-secret", "session=hidden", "db-password"))
    assert "<redacted>" in diagnostic


def test_fail_audit_failure_does_not_mask_original_collection_error(tmp_path):
    def broken_fail(_run_id, _exc, **_kwargs):
        raise sqlite3.OperationalError("token=audit-secret")

    conn, sync = setup(
        tmp_path, ("CONCLUDED",), missing_provider=False,
        sync_kwargs={"fail_audit": broken_fail},
    )
    Collector.results = {"CD_M1": RuntimeError("original collection failure")}

    result = sync.run(season=2026, competition_code="AFL",
                      competition_provider_id="CD_C1")

    match = result.matches[0]
    assert match.error == "original collection failure"
    assert match.audit_outcome == "failed"
    assert match.audit_error_class == "OperationalError"
    assert "audit-secret" not in match.audit_error_summary


def test_parent_audit_failure_preserves_completed_children(tmp_path):
    calls = 0

    def fail_parent(run_id, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("parent audit commit failed")
        complete_scrape_run(run_id, **kwargs)

    conn, sync = setup(
        tmp_path, ("CONCLUDED",), missing_provider=False,
        sync_kwargs={"complete_audit": fail_parent},
    )
    Collector.results = {"CD_M1": concluded()}

    result = sync.run(season=2026, competition_code="AFL",
                      competition_provider_id="CD_C1")

    assert result.outcome == "partial" and result.audit_outcome == "failed"
    assert result.audit_error_class == "OperationalError"
    assert result.matches[0].persistence_outcome == "committed"
    assert result.matches[0].audit_outcome == "completed"
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 1


def test_already_terminal_child_audit_is_an_audit_only_failure(tmp_path):
    calls = 0

    def terminal_child(run_id, **kwargs):
        nonlocal calls
        calls += 1
        complete_scrape_run(run_id, **kwargs)
        if calls == 1:
            complete_scrape_run(run_id, **kwargs)

    conn, sync = setup(
        tmp_path, ("CONCLUDED",), missing_provider=False,
        sync_kwargs={"complete_audit": terminal_child},
    )
    Collector.results = {"CD_M1": concluded()}

    result = sync.run(season=2026, competition_code="AFL",
                      competition_provider_id="CD_C1")

    assert result.matches[0].persistence_outcome == "committed"
    assert result.matches[0].audit_outcome == "failed"
    assert result.matches[0].audit_error_class == "ValueError"
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 1


def test_ambient_transaction_is_rejected_before_any_side_effect(tmp_path):
    conn, sync = setup(tmp_path, ("CONCLUDED",), missing_provider=False)
    before_audits = conn.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0]
    conn.execute("CREATE TEMP TABLE caller_work(value INTEGER)")
    conn.execute("INSERT INTO caller_work VALUES (1)")
    Collector.calls = []

    with pytest.raises(RuntimeError, match="without an active transaction"):
        sync.run(season=2026, competition_code="AFL", competition_provider_id="CD_C1")

    assert Collector.calls == []
    assert conn.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0] == before_audits
    assert conn.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0] == 1


def decision_rows(conn):
    return conn.execute(
        "SELECT * FROM scrape_runs WHERE scrape_type='afl_season_sync_decision' "
        "ORDER BY started_at,run_id"
    ).fetchall()


def test_safe_and_material_skips_persist_correlated_zero_write_audits(tmp_path):
    future = "2099-01-01T00:00:00+00:00"
    past = "2020-01-01T00:00:00+00:00"
    conn, sync = setup(
        tmp_path, ("SCHEDULED", "LIVE", "CONCLUDED", "MYSTERY", "MYSTERY"),
        missing_provider=True, client=NoDetailClient(),
        start_times=(future, future, future, future, past),
    )

    result = sync.run(season=2026, competition_code="AFL",
                      competition_provider_id="CD_C1")
    rows = decision_rows(conn)

    assert Collector.calls == []
    assert {row["reason_code"] for row in rows} == {
        "scheduled", "live_or_postgame", "future_placeholder",
        "unresolved_lifecycle", "missing_provider_identity",
    }
    assert len({row["run_id"] for row in rows}) == 5
    assert {row["correlation_id"] for row in rows} == {result.correlation_id}
    assert all((row["rows_read"], row["rows_written"]) == (0, 0) for row in rows)
    classes = {row["reason_code"]: row["decision_class"] for row in rows}
    assert classes == {
        "scheduled": "safe", "live_or_postgame": "safe",
        "future_placeholder": "safe", "unresolved_lifecycle": "material",
        "missing_provider_identity": "material",
    }
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0] == 0


def test_missing_explicit_target_has_no_fabricated_identity(tmp_path):
    conn, sync = setup(tmp_path, (), missing_provider=False)
    result = sync.run(
        season=2026, competition_code="AFL", competition_provider_id="CD_C1",
        options=SeasonSyncOptions(match_ids=(9999,)),
    )

    missing = next(row for row in decision_rows(conn)
                   if row["reason_code"] == "requested_match_not_found")
    returned = next(match for match in result.matches
                    if match.reason_code == "requested_match_not_found")
    assert missing["target_identifier"] == "9999"
    assert missing["decision_class"] == returned.decision_class == "material"
    assert missing["canonical_match_id"] is None
    assert missing["provider_match_id"] is None
    assert missing["round_identifier"] is None
    assert returned.requested_match_id == 9999 and returned.canonical_match_id is None


def test_already_complete_is_historical_and_collected_match_is_not_duplicated(tmp_path):
    conn, sync = setup(tmp_path, ("CONCLUDED",), missing_provider=False)
    Collector.results = {"CD_M1": concluded()}
    first = sync.run(season=2026, competition_code="AFL",
                     competition_provider_id="CD_C1")
    second = sync.run(season=2026, competition_code="AFL",
                      competition_provider_id="CD_C1")
    third = sync.run(season=2026, competition_code="AFL",
                     competition_provider_id="CD_C1")
    rows = decision_rows(conn)

    assert Collector.calls == ["CD_M1"]
    assert not [row for row in rows if row["correlation_id"] == first.correlation_id]
    complete = [row for row in rows if row["reason_code"] == "already_complete"]
    assert len(complete) == 2
    assert complete[0]["run_id"] != complete[1]["run_id"]
    assert {row["correlation_id"] for row in complete} == {
        second.correlation_id, third.correlation_id,
    }
    assert all(row["status"] == "completed" for row in complete)
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0] == 0
