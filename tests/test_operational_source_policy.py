import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
from afl_json import AflJsonAuthenticationError, AflJsonInvalidResponse
from afl_json import MatchStatusResolution, PlayerStatsCollectionResult, PlayerStatsStatus
from afl_json.rosters import RosterCollectionResult, RosterStatus
from collection import source_policy
from collection.source_policy import OperationalDomain, policy_for
from db.migration_runner import migrate_database
from scheduler import manual_triggers
from scheduler.match_windows import MatchWindowSettings, reconcile as reconcile_match_windows
from scheduler.schedule_lineup_scrapes import run_lineup_round_scraper
from scheduler.schedule_stat_scrapes import run_stats_scraper
from scheduler import scheduled_tasks
from scheduler import schedule_match_scrapes
from scheduler.collection import collect_scheduled
from scheduler.registry import execute_registered_job, upsert_job


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
    assert policy_for(OperationalDomain.METADATA).source_family == "public_afl_json"
    assert policy_for(OperationalDomain.MATCH_STATUS).source_family == "public_afl_json"
    assert policy_for(OperationalDomain.MATCH_ROSTERS).source_family == "cfs_json"
    # Canonical roster persistence (Issue #219, migration 0024) is a distinct
    # authority from the legacy HTML lineups domain below -- see
    # docs/architecture/data_authority_map.md.
    assert policy_for(OperationalDomain.MATCH_ROSTERS).persists is True
    assert policy_for(OperationalDomain.LINEUPS).source_family == "html"
    assert policy_for(OperationalDomain.LINEUPS).persists is True
    assert policy_for(OperationalDomain.LINEUPS).preferred_source_family == "cfs_json"
    # preferred_* is informational context only; it never redirects LINEUPS'
    # own source_family/collector/persists above, which remain HTML-only.
    assert policy_for(OperationalDomain.LINEUPS).preferred_persists is True
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
    assert len(calls) == 1 and calls[0][0] is OperationalDomain.MATCH_STATUS
    assert calls[0][1]["target_id"] == 10
    assert callable(calls[0][1]["write_executor"])


def test_match_status_advance_reconciles_active_window_immediately(tmp_path, monkeypatch):
    """Regression for issue #145 (match 8230): once canonical matches.status
    advances (here via the direct match-detail reconciliation), the active
    match_stat_windows row for that match must advance its lifecycle in the
    same operation, without waiting for the next reconciliation sweep or a
    scheduler restart, and without disturbing an in-flight polling lease."""
    path = _database(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "UPDATE matches SET status='LIVE', start_time_utc=? WHERE match_id=10",
        ((now - timedelta(hours=1)).isoformat(),),
    )
    conn.commit()
    settings = MatchWindowSettings(
        pre_match_window=timedelta(hours=2), post_match_horizon=timedelta(hours=6),
        lease_duration=timedelta(minutes=15),
    )
    reconcile_match_windows(conn, now=now, settings=settings)
    # Simulate an already-active polling series holding a live lease.
    conn.execute(
        "UPDATE match_stat_windows SET status='leased', lease_owner='poller-1', "
        "lease_token='tok-1', lease_generation=1, lease_claimed_at=?, lease_expires_at=? "
        "WHERE match_id=10",
        (now.isoformat(), (now + timedelta(minutes=15)).isoformat()),
    )
    conn.commit()
    before = conn.execute(
        "SELECT lifecycle, status, lease_token FROM match_stat_windows WHERE match_id=10"
    ).fetchone()
    assert before[:] == ("LIVE", "leased", "tok-1")

    class DirectClient:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def get(self, endpoint, **_kwargs):
            assert endpoint == "match_detail"
            return SimpleNamespace(data={"matches": [
                {"id": 10, "providerId": "CD_M10", "status": "CONCLUDED"},
            ]})

    def execute(_operation, _target, callback):
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            return callback(db)

    outcome = source_policy.collect_operational(
        OperationalDomain.MATCH_STATUS, target_id=10,
        client_factory=DirectClient, write_executor=execute,
    )
    assert outcome.persistence_performed is True

    conn2 = sqlite3.connect(path)
    conn2.row_factory = sqlite3.Row
    assert conn2.execute("SELECT status FROM matches WHERE match_id=10").fetchone()[0] == "CONCLUDED"
    after = conn2.execute(
        "SELECT lifecycle, status, lease_token FROM match_stat_windows WHERE match_id=10"
    ).fetchone()
    # Lifecycle advances promptly and the active lease/polling series is untouched.
    assert after[:] == ("CONCLUDED", "leased", "tok-1")


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


def test_scheduler_stats_orders_audit_network_parse_persist_and_final_audit(tmp_path, monkeypatch):
    _database(tmp_path, monkeypatch)
    events = []
    opened = []
    real_open = source_policy.get_db_connection
    def tracked_open():
        conn = real_open(); opened.append(conn); return conn
    monkeypatch.setattr(source_policy, "get_db_connection", tracked_open)

    class Client:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get(self, *_args, **_kwargs):
            assert all(not conn.in_transaction for conn in opened)
            events.append("network")
            return object()
    def reconcile(conn, client, **kwargs):
        client.get("status")
        return MatchStatusResolution("CD_M10", 10, "SCHEDULED", "LIVE", "LIVE",
                                     "direct_match_detail", True, ())
    class Collector:
        def __init__(self, client): self.client = client
        def collect(self, *_args, **_kwargs):
            self.client.get("stats"); events.append("parsed")
            return PlayerStatsCollectionResult("CD_M10", PlayerStatsStatus.LIVE_PARTIAL,
                                               [], [], "now")
    monkeypatch.setattr(source_policy, "reconcile_match_status", reconcile)
    monkeypatch.setattr(source_policy, "MatchPlayerStatsCollector", Collector)
    monkeypatch.setattr(source_policy, "persist_match_status_resolution",
                        lambda conn, result: events.append("status_persist"))
    monkeypatch.setattr(source_policy, "upsert_player_stats",
                        lambda conn, result: events.append("stats_persist") or 0)

    def execute(operation, target, callback):
        events.append(operation)
        with real_open() as conn:
            return callback(conn)
    source_policy.collect_operational(
        OperationalDomain.MATCH_PLAYER_STATS, target_id=10,
        client_factory=Client, write_executor=execute,
    )
    assert events == ["scrape_runs.start", "network", "network", "parsed",
                      "cfs_player_stats.persist_match", "status_persist", "stats_persist",
                      "scrape_runs.complete"]


def test_failed_scheduled_persistence_finalises_audit_and_registry(tmp_path, monkeypatch):
    _database(tmp_path, monkeypatch)
    upsert_job("failure-job", "lineup", None)
    monkeypatch.setattr("scraper.scrape_afl_lineups.scrape_team_lineups",
                        lambda **kwargs: [{"match_id": 10, "afl_id": 101}])
    monkeypatch.setattr("db.import_to_db.save_lineups_to_db",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("persist failed")))
    with pytest.raises(RuntimeError, match="persist failed"):
        execute_registered_job(
            "failure-job", lambda: collect_scheduled(OperationalDomain.LINEUPS, target_id=1))
    with sqlite3.connect(config.DB_PATH) as conn:
        assert conn.execute(
            "SELECT status FROM scheduler_job_registry WHERE job_id='failure-job'"
        ).fetchone()[0] == "failed"
        assert conn.execute(
            "SELECT status FROM scrape_runs WHERE correlation_id='failure-job'"
        ).fetchone()[0] == "failed"


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
