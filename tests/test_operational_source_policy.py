import logging
import sqlite3

import pytest

import config
from afl_json import AflJsonAuthenticationError, AflJsonInvalidResponse
from afl_json.rosters import RosterCollectionResult, RosterStatus
from collection import source_policy
from collection.source_policy import OperationalDomain, policy_for
from db.migration_runner import migrate_database
from scheduler import manual_triggers
from scheduler.schedule_lineup_scrapes import run_lineup_round_scraper
from scheduler.schedule_stat_scrapes import run_stats_scraper
from scheduler import scheduled_tasks
from scheduler import schedule_match_scrapes


def _database(tmp_path, monkeypatch):
    path = tmp_path / "policy.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO rounds(round_id, round_label, provider_id) VALUES (1, 'R1', 'CD_R1')")
    conn.execute("""INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, status)
                    VALUES (10, 'CD_M10', 1, 'A', 'B', 'SCHEDULED')""")
    conn.commit()
    conn.close()
    return path


def test_policy_is_json_first_and_keeps_injuries_intentionally_html():
    assert policy_for(OperationalDomain.METADATA).source_family == "public_json"
    assert policy_for(OperationalDomain.MATCH_STATUS).source_family == "public_json"
    assert policy_for(OperationalDomain.MATCH_ROSTERS).source_family == "cfs_json"
    assert policy_for(OperationalDomain.MATCH_ROSTERS).persists is False
    assert policy_for(OperationalDomain.LINEUPS).source_family == "html"
    assert policy_for(OperationalDomain.LINEUPS).persists is True
    assert policy_for(OperationalDomain.LINEUPS).preferred_source_family == "cfs_json"
    assert policy_for(OperationalDomain.LINEUPS).preferred_persists is False
    assert policy_for(OperationalDomain.MATCH_PLAYER_STATS).source_family == "cfs_json"
    assert policy_for(OperationalDomain.INJURIES).source_family == "html"
    assert all(not item.fallback_permitted for item in source_policy.SOURCE_POLICY.values())


def test_scheduler_and_admin_dispatch_equivalent_operations_through_policy(monkeypatch):
    calls = []
    monkeypatch.setattr(source_policy, "collect_operational",
                        lambda domain, **kwargs: calls.append((domain, kwargs)))
    # Imports inside wrappers resolve the patched shared function.
    run_lineup_round_scraper(1)
    manual_triggers.manual_refresh_lineups_round(1, "admin-job")
    run_stats_scraper(10)
    manual_triggers.manual_refresh_player_stats_match(10, "admin-job")
    assert [call[0] for call in calls] == [
        OperationalDomain.LINEUPS, OperationalDomain.LINEUPS,
        OperationalDomain.MATCH_PLAYER_STATS, OperationalDomain.MATCH_PLAYER_STATS,
    ]
    assert calls[0][1]["target_id"] == calls[1][1]["target_id"] == 1
    assert calls[2][1]["target_id"] == calls[3][1]["target_id"] == 10


def test_daily_fixture_jobs_keep_complete_public_metadata_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(source_policy, "collect_operational",
                        lambda domain, **kwargs: calls.append(domain))
    monkeypatch.setattr(scheduled_tasks, "execute_registered_job",
                        lambda _job_id, func, *args: func(*args))
    scheduled_tasks.daily_fixture_scrape()
    scheduled_tasks.daily_match_scrape()
    assert calls == [OperationalDomain.METADATA, OperationalDomain.METADATA]


def test_live_match_refresh_is_narrow_public_status_between_metadata_runs(tmp_path, monkeypatch):
    _database(tmp_path, monkeypatch)
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE matches SET status='LIVE' WHERE match_id=10")
    conn.commit()
    conn.close()
    calls = []
    monkeypatch.setattr(source_policy, "collect_operational",
                        lambda domain, **kwargs: calls.append((domain, kwargs)))
    monkeypatch.setattr(schedule_match_scrapes.time, "sleep", lambda _seconds: None)
    schedule_match_scrapes.refresh_live_matches()
    assert calls == [(OperationalDomain.MATCH_STATUS, {"target_id": 10})]


def test_operational_lineups_persist_and_report_written_rows(tmp_path, monkeypatch):
    _database(tmp_path, monkeypatch)
    records = [{"match_id": 10, "afl_id": 101}]
    saved = []
    monkeypatch.setattr("scraper.scrape_afl_lineups.scrape_team_lineups",
                        lambda **kwargs: records)
    monkeypatch.setattr("db.import_to_db.save_lineups_to_db",
                        lambda rows, conn, round_id: saved.append((rows, round_id)) or len(rows))
    outcome = source_policy.collect_operational(OperationalDomain.LINEUPS, target_id=1)
    assert saved == [(records, 1)]
    assert outcome.persistence_performed is True
    assert outcome.rows_read == outcome.rows_written == 1
    assert outcome.source_family == "html"


def test_empty_lineup_collection_is_not_reported_as_persisted(tmp_path, monkeypatch):
    _database(tmp_path, monkeypatch)
    monkeypatch.setattr("scraper.scrape_afl_lineups.scrape_team_lineups",
                        lambda **kwargs: [])
    monkeypatch.setattr("db.import_to_db.save_lineups_to_db",
                        lambda rows, conn, round_id: 0)
    outcome = source_policy.collect_operational(OperationalDomain.LINEUPS, target_id=1)
    assert outcome.status == "unavailable"
    assert outcome.persistence_performed is False
    assert outcome.rows_read == outcome.rows_written == 0


class _Client:
    def __enter__(self): return self
    def __exit__(self, *_args): pass


def test_unpublished_roster_is_observable_and_does_not_run_html(tmp_path, monkeypatch, caplog):
    _database(tmp_path, monkeypatch)
    legacy_called = False
    class Collector:
        def __init__(self, _client): pass
        def collect(self, provider_id):
            assert provider_id == "CD_R1"
            return RosterCollectionResult(provider_id, RosterStatus.UNAVAILABLE, [], [])
    monkeypatch.setattr(source_policy, "MatchRosterCollector", Collector)
    caplog.set_level(logging.INFO, logger="operational_collection")
    outcome = source_policy.collect_operational(
        OperationalDomain.MATCH_ROSTERS, target_id=1, client_factory=_Client
    )
    assert outcome.status == "unavailable"
    assert outcome.fallback_occurred is False
    assert outcome.persistence_performed is False
    assert "source_family\": \"cfs_json" in caplog.text
    assert legacy_called is False


@pytest.mark.parametrize("error", [
    AflJsonAuthenticationError("bad auth", endpoint="match_rosters"),
    AflJsonInvalidResponse("bad payload", endpoint="match_rosters"),
    RuntimeError("programming or persistence failure"),
])
def test_unsafe_failures_never_trigger_fallback(tmp_path, monkeypatch, error):
    _database(tmp_path, monkeypatch)
    class Collector:
        def __init__(self, _client): pass
        def collect(self, _provider_id): raise error
    monkeypatch.setattr(source_policy, "MatchRosterCollector", Collector)
    with pytest.raises(type(error)):
        source_policy.collect_operational(
            OperationalDomain.MATCH_ROSTERS, target_id=1, client_factory=_Client
        )
    conn = sqlite3.connect(config.DB_PATH)
    audit = conn.execute("SELECT status, error_class FROM scrape_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    conn.close()
    assert audit == ("failed", type(error).__name__)
