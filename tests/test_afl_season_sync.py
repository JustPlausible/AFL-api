from __future__ import annotations

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

from afl_json import (CollectionResult, PlayerPersistenceSummary,
                      PlayerStatsStatus, normalise_player_stats, persist_afl_metadata)
from afl_json.season_sync import (SeasonBootstrapResult, SeasonSynchronizer,
                                  SeasonSyncOptions)
from db.migration_runner import migrate_database
from db.scrape_runs import complete_scrape_run, start_scrape_run


def metadata(statuses=("CONCLUDED", "SCHEDULED", "CONCLUDED"), *, missing_provider=True):
    matches = []
    for index, status in enumerate(statuses, 1):
        matches.append({
            "afl_id": 8000 + index,
            "provider_id": None if missing_provider and index == 3 else f"CD_M{index}",
            "status": status, "round": {"id": 100 + index},
            "home": {}, "away": {}, "home_score": None, "away_score": None,
            "venue": None, "utc_start_time": None, "metadata": None, "source": {},
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
         for i in range(1, 4)], [], matches,
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
          missing_provider=True, client=None):
    path = tmp_path / "sync.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    collected = metadata(statuses, missing_provider=missing_provider)

    def bootstrap(_client, db, **_kwargs):
        summary = persist_afl_metadata(db, collected)
        players = PlayerPersistenceSummary("published", 0, 0, 0, 0, 0, 0, 0, 0)
        return SeasonBootstrapResult(1, "CD_C1", 85, "CD_S85", summary, players)

    Collector.calls = []
    return conn, SeasonSynchronizer(client or object(), conn, bootstrap=bootstrap,
                                    collector_factory=Collector)


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
