from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from afl_json.client import AflJsonAuthenticationError, AflJsonClient
from afl_json.match_period import MatchPeriodState
from afl_json.player_stats import PlayerStatsStatus, normalise_player_stats
from db.migration_runner import migrate_database
from scheduler.match_windows import MatchWindowSettings, reconcile
from scheduler.player_stat_polling import PlayerStatPollingSettings, PlayerStatPollingWorker, deterministic_jitter

NOW = datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)

class DirectLane:
    def __init__(self, path): self.path = path; self.active = 0; self.max_active = 0; self.network_during_write = False; self.lock = threading.Lock()
    def execute(self, op, target, cb): return self._run(cb)
    def execute_immediate(self, op, target, cb): return self._run(cb, immediate=True)
    def _run(self, cb, immediate=False):
        with self.lock:
            self.active += 1; self.max_active=max(self.max_active,self.active)
            conn=sqlite3.connect(self.path, timeout=10); conn.row_factory=sqlite3.Row; conn.execute("PRAGMA foreign_keys=ON")
            try:
                if immediate: conn.execute("BEGIN IMMEDIATE")
                result=cb(conn); conn.commit(); return result
            except Exception:
                conn.rollback(); raise
            finally:
                conn.close(); self.active -= 1

@pytest.fixture
def db(tmp_path, monkeypatch):
    path=tmp_path/"afl.db"; monkeypatch.setenv("DB_PATH", str(path))
    import config; monkeypatch.setattr(config, "DB_PATH", str(path), raising=False)
    migrate_database(path)
    conn=sqlite3.connect(path); conn.row_factory=sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn,path
    conn.close()

def add_match(conn, match_id=8001, provider="CD_M1", status="LIVE", start=None):
    start=start or (NOW-timedelta(minutes=10)).isoformat()
    conn.execute("INSERT OR IGNORE INTO rounds(round_id,round_label,season_id,competition_id,scraped_at) VALUES(1,'R1',73,1,?)", (NOW.isoformat(),))
    conn.execute("INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,status,start_time_utc,season_id,scraped_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (match_id,provider,1,'A','B','V',status,start,73,NOW.isoformat()))
    conn.commit()

def settings(**kw):
    base=dict(enabled=True, jitter_seconds=0)
    base.update(kw); return PlayerStatPollingSettings(**base)

def window_settings(): return MatchWindowSettings(pre_match_window=timedelta(hours=2), post_match_horizon=timedelta(hours=6), lease_duration=timedelta(minutes=5))

class Client:
    def __init__(self, payload=None, error=None): self.payload=payload; self.error=error; self.calls=0
    def get(self, endpoint, **kwargs):
        self.calls += 1
        if self.error: raise self.error
        return SimpleNamespace(data=self.payload)

class Pool:
    def __init__(self, client): self._client=client; self.closed=False
    def client(self): return self._client
    def close(self): self.closed=True

def live_payload(status="LIVE", players=2):
    mk=lambda i: {"player":{"playerId":f"CD_I{i}"}, "playerStats":{"stats":{"goals":i,"kicks":i}}}
    return {"matchStatus":status,"homeTeamPlayerStats":[mk(1)],"awayTeamPlayerStats":[mk(2)] if players>1 else []}

def test_due_live_window_claimed_collected_persisted_and_rescheduled(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    client=Client(live_payload()); lane=DirectLane(path)
    worker=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(client), clock=lambda: NOW, lane=lane)
    out=worker.run_once()[0]
    assert out["status"] == "rescheduled"
    assert client.calls == 1
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0] == 0
    row=conn.execute("SELECT status,next_due_at,cadence_profile,lease_token FROM match_stat_windows").fetchone()
    assert row["status"] == "awaiting_final" and row["lease_token"] is None
    assert row["next_due_at"] == (NOW + timedelta(seconds=60)).isoformat()
    assert row["cadence_profile"] == "live_partial"
    assert lane.max_active == 1
    assert_no_running_attempts(conn)

def test_live_default_cadence_is_sixty_seconds_and_restart_persists_next_due(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(live_payload())), clock=lambda: NOW, lane=DirectLane(path)).run_once()
    stored=conn.execute("SELECT next_due_at FROM match_stat_windows").fetchone()[0]
    assert datetime.fromisoformat(stored) == NOW + timedelta(seconds=60)
    # A later worker sees the persisted due time and does not replay missed ticks before it is due.
    assert PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(live_payload())), clock=lambda: NOW+timedelta(seconds=30), lane=DirectLane(path)).run_once() == []

def test_lifecycle_phases_and_deterministic_jitter():
    s=settings(jitter_seconds=5)
    assert deterministic_jitter("a","live",30) != deterministic_jitter("b","live",30)
    for lifecycle, status, expected in [("LIVE", PlayerStatsStatus.LIVE_PARTIAL, "live_partial"),("CONCLUDED", PlayerStatsStatus.UNAVAILABLE, "unpublished_or_unavailable"),("POSTGAME", PlayerStatsStatus.CONCLUDED, "post_match_awaiting_final")]:
        from scheduler.player_stat_polling import cadence_for
        assert cadence_for({"lifecycle": lifecycle}, status, s)[1] == expected

def test_valid_ownership_prevents_duplicate_attempts(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    worker=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(live_payload())), clock=lambda: NOW, lane=DirectLane(path))
    first=worker.claim_due(); second=worker.claim_due()
    assert len(first) == 1 and second == []
    assert conn.execute("SELECT attempt_count FROM match_stat_windows").fetchone()[0] == 0

def test_unpublished_transient_and_auth_failure_backoff_without_hot_loop(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    worker=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(error=__import__("afl_json.client", fromlist=["AflJsonResourceUnavailable"]).AflJsonResourceUnavailable("not published", endpoint="match_player_statistics"))), clock=lambda: NOW, lane=DirectLane(path))
    worker.run_once(); assert conn.execute("SELECT consecutive_failure_count FROM match_stat_windows").fetchone()[0] == 0
    conn.execute("UPDATE match_stat_windows SET status='due', next_due_at=?", (NOW.isoformat(),)); conn.commit()
    err=AflJsonAuthenticationError("Authorization: Bearer secret-token", endpoint="match_player_statistics", status_code=401)
    out=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(error=err)), clock=lambda: NOW, lane=DirectLane(path)).run_once()[0]
    assert out["status"] == "auth_failed_paused"
    row=conn.execute("SELECT consecutive_failure_count,diagnostic_summary,next_due_at FROM match_stat_windows").fetchone()
    assert row[0] == 1 and "secret-token" not in row[1]
    assert datetime.fromisoformat(row[2]) >= NOW + timedelta(minutes=30)
    assert PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(live_payload())), clock=lambda: NOW+timedelta(seconds=1), lane=DirectLane(path)).run_once() == []

def test_final_complete_closes_and_live_cannot_regress(db):
    conn,path=db; add_match(conn,status="CONCLUDED"); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    payload=live_payload("CONCLUDED")
    # add enough final rows for completeness
    payload["homeTeamPlayerStats"]=[{"player":{"playerId":f"CD_H{i}"},"playerStats":{"stats":{"goals":1}}} for i in range(20)]
    payload["awayTeamPlayerStats"]=[{"player":{"playerId":f"CD_A{i}"},"playerStats":{"stats":{"goals":1}}} for i in range(20)]
    PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(payload)), clock=lambda: NOW, lane=DirectLane(path)).run_once()
    assert_no_running_attempts(conn)
    assert conn.execute("SELECT status,finality_state FROM match_stat_windows").fetchone()[:] == ("complete","authoritative_complete")
    before=conn.execute("SELECT COUNT(*), MIN(snapshot_authority) FROM cfs_player_stats").fetchone()
    conn.execute("UPDATE match_stat_windows SET status='due', next_due_at=?, lease_token=NULL", (NOW.isoformat(),)); conn.commit()
    PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(live_payload("LIVE"))), clock=lambda: NOW+timedelta(minutes=1), lane=DirectLane(path)).run_once()
    assert conn.execute("SELECT COUNT(*), MIN(snapshot_authority) FROM cfs_player_stats").fetchone() == before

def test_stale_claimed_window_lifecycle_resolves_canonical_conclusion(db):
    """Regression for issue #145 (match 8230): matches.status advances to
    CONCLUDED after a window was planned/claimed but before this attempt's
    collection. The claimed row's cached lifecycle is still LIVE and the CFS
    endpoint itself still reports LIVE (an explicit but stale endpoint
    lifecycle), yet collection must resolve and persist canonical CONCLUDED,
    and the window must move into post-match cadence *and* the
    final_confirmation phase without a scheduler restart.

    partial_cadence and post_match_cadence are deliberately set to different
    values here: their shared 2-minute default would let a next_due_at
    assertion pass by coincidence even if the wrong cadence branch fired."""
    conn, path = db
    add_match(conn, status="LIVE")
    reconcile(conn, now=NOW, settings=window_settings())
    conn.commit()
    before = conn.execute(
        "SELECT lifecycle, collection_phase FROM match_stat_windows"
    ).fetchone()
    assert before[:] == ("LIVE", "live")
    # The canonical match concludes after the window was reconciled/claimed,
    # without the window row itself being touched again.
    conn.execute("UPDATE matches SET status='CONCLUDED' WHERE match_id=8001")
    conn.commit()
    client = Client(live_payload("LIVE"))  # stale/explicit endpoint lifecycle
    worker_settings = settings(partial_cadence=timedelta(seconds=99),
                                post_match_cadence=timedelta(seconds=45))
    worker = PlayerStatPollingWorker(settings=worker_settings, window_settings=window_settings(),
        client_pool=Pool(client), clock=lambda: NOW, lane=DirectLane(path))
    out = worker.run_once()[0]
    assert client.calls == 1
    assert out["status"] == "rescheduled"
    rows = conn.execute("SELECT resolved_match_status FROM cfs_player_stats").fetchall()
    assert rows and all(row[0] == "CONCLUDED" for row in rows)
    after = conn.execute(
        "SELECT next_due_at, collection_phase, cadence_profile FROM match_stat_windows"
    ).fetchone()
    # Cadence must reflect the distinct post_match_cadence, not partial_cadence
    # and not the stale claimed lifecycle's 60-second live cadence.
    assert after["next_due_at"] == (NOW + timedelta(seconds=45)).isoformat()
    assert after["collection_phase"] == "final_confirmation"
    assert after["cadence_profile"] == "post_match_awaiting_final"


def test_stale_window_with_absent_endpoint_lifecycle_reaches_authoritative_completion(db):
    """Regression for issue #145: canonical CONCLUDED with a stale claimed
    window lifecycle *and* a completely absent endpoint lifecycle (no
    matchStatus/status/matchPhase field at all, not merely an older explicit
    value). Once enough authoritative rows are present, the window must reach
    'complete' without ever having its cached lifecycle reconciled first."""
    conn, path = db
    add_match(conn, status="LIVE")
    reconcile(conn, now=NOW, settings=window_settings())
    conn.commit()
    assert conn.execute("SELECT lifecycle FROM match_stat_windows").fetchone()[0] == "LIVE"
    conn.execute("UPDATE matches SET status='CONCLUDED' WHERE match_id=8001")
    conn.commit()
    payload = {
        "homeTeamPlayerStats": [{"player": {"playerId": f"CD_H{i}"}, "playerStats": {"stats": {"goals": 1}}} for i in range(20)],
        "awayTeamPlayerStats": [{"player": {"playerId": f"CD_A{i}"}, "playerStats": {"stats": {"goals": 1}}} for i in range(20)],
    }  # deliberately no matchStatus/status/matchPhase key present
    client = Client(payload)
    worker = PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(),
        client_pool=Pool(client), clock=lambda: NOW, lane=DirectLane(path))
    out = worker.run_once()[0]
    assert client.calls == 1
    assert out["status"] == "complete"
    rows = conn.execute("SELECT resolved_match_status, snapshot_authority FROM cfs_player_stats").fetchall()
    assert len(rows) == 40
    assert all(row[0] == "CONCLUDED" and row[1] == 2 for row in rows)
    after = conn.execute(
        "SELECT status, collection_phase, finality_state, lease_token FROM match_stat_windows"
    ).fetchone()
    assert after[:] == ("complete", "complete", "authoritative_complete", None)


def test_controls_allowlist_and_drain(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    assert PlayerStatPollingWorker(settings=settings(enabled=False), client_pool=Pool(Client(live_payload())), lane=DirectLane(path), clock=lambda: NOW).run_once() == []
    assert PlayerStatPollingWorker(settings=settings(allowed_matches=("999",)), client_pool=Pool(Client(live_payload())), lane=DirectLane(path), clock=lambda: NOW).run_once() == []
    assert PlayerStatPollingWorker(settings=settings(drain=True), client_pool=Pool(Client(live_payload())), lane=DirectLane(path), clock=lambda: NOW).run_once() == []

def add_two_due_live_matches(conn):
    add_match(conn, match_id=8001, provider="CD_M1", status="LIVE")
    add_match(conn, match_id=8002, provider="CD_M2", status="LIVE")
    reconcile(conn, now=NOW, settings=window_settings()); conn.commit()


def test_run_once_executes_different_matches_concurrently_with_serialized_writes(db):
    conn,path=db; add_two_due_live_matches(conn)
    barrier = threading.Barrier(2)
    state = {"active": 0, "max_active": 0}
    lock = threading.Lock()

    class ConcurrentCollector:
        def __init__(self, client, *, clock): self.clock = clock
        def collect(self, match_provider_id, **kwargs):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            barrier.wait(timeout=5)
            try:
                return normalise_player_stats(live_payload("LIVE"), match_provider_id, collected_at=NOW.isoformat(), afl_match_id=kwargs.get("afl_match_id"), canonical_match_status="LIVE")
            finally:
                with lock: state["active"] -= 1

    lane = DirectLane(path)
    out = PlayerStatPollingWorker(settings=settings(max_workers=2, claim_limit=2, network_concurrency=2), window_settings=window_settings(), client_pool=Pool(Client(live_payload())), collector_factory=ConcurrentCollector, clock=lambda: NOW, lane=lane).run_once()
    assert len(out) == 2
    assert state["max_active"] == 2
    assert lane.max_active == 1
    assert conn.execute("SELECT COUNT(DISTINCT match_provider_id) FROM cfs_player_stats").fetchone()[0] == 2


def test_pre_match_window_does_not_call_cfs_before_authoritative_live(db):
    conn,path=db; add_match(conn, status="SCHEDULED", start=(NOW + timedelta(minutes=30)).isoformat())
    reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    client = Client(live_payload())
    out = PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(client), clock=lambda: NOW, lane=DirectLane(path)).run_once()[0]
    assert out["status"] == "awaiting_authoritative_live"
    assert client.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 0
    assert conn.execute("SELECT reason_code FROM match_stat_windows").fetchone()[0] == "awaiting_authoritative_live"
    assert_no_running_attempts(conn)


def test_process_client_pool_uses_public_shared_token_boundary_and_closes_all_thread_clients():
    pool = __import__("scheduler.player_stat_polling", fromlist=["SchedulerCfsClientPool"]).SchedulerCfsClientPool(token_acquirer=lambda: "token")
    clients = []
    def get_client(): clients.append(pool.client())
    threads = [threading.Thread(target=get_client) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len({id(c.session) for c in clients}) == 2
    assert clients[0].token_provider is clients[1].token_provider
    pool.close()
    with pytest.raises(RuntimeError):
        pool.client()


def test_domain_auth_pause_blocks_other_matches_without_token_hammering(db):
    conn,path=db; add_match(conn, match_id=8001, provider="CD_M1", status="LIVE"); add_match(conn, match_id=8002, provider="CD_M2", status="LIVE")
    reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    err=AflJsonAuthenticationError("cookie: secret", endpoint="match_player_statistics", status_code=401)
    client = Client(error=err)
    worker = PlayerStatPollingWorker(settings=settings(claim_limit=1, max_workers=1), window_settings=window_settings(), client_pool=Pool(client), clock=lambda: NOW, lane=DirectLane(path))
    assert worker.run_once()[0]["status"] == "auth_failed_paused"
    assert worker.status()["auth_paused"] is True
    assert worker.run_once() == []
    assert client.calls == 1
    assert conn.execute("SELECT COUNT(*) FROM scrape_runs WHERE reason_code='auth_domain_paused'").fetchone()[0] == 1


def test_increasing_backoff_uses_persisted_failure_history(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    conn.execute("UPDATE match_stat_windows SET consecutive_failure_count=2"); conn.commit()
    err=AflJsonAuthenticationError("authorization: Bearer token", endpoint="match_player_statistics", status_code=401)
    PlayerStatPollingWorker(settings=settings(auth_pause=timedelta(minutes=10)), window_settings=window_settings(), client_pool=Pool(Client(error=err)), clock=lambda: NOW, lane=DirectLane(path)).run_once()
    due=datetime.fromisoformat(conn.execute("SELECT next_due_at FROM match_stat_windows").fetchone()[0])
    assert due == NOW + timedelta(minutes=40)


def test_empty_and_unknown_results_are_typed_backoff_not_ordinary_success(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    worker=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client({"matchStatus":"LIVE","homeTeamPlayerStats":[],"awayTeamPlayerStats":[]})), clock=lambda: NOW, lane=DirectLane(path))
    out=worker.run_once()[0]
    row=conn.execute("SELECT status,consecutive_failure_count,cadence_profile FROM match_stat_windows").fetchone()
    assert out["status"] == "rejected_backoff"
    assert row[:] == ("backoff", 1, "empty_result")


def test_lost_lease_before_persistence_fails_audit_without_window_mutation(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    class StealingCollector:
        def __init__(self, client, *, clock): pass
        def collect(self, match_provider_id, **kwargs):
            steal=sqlite3.connect(path); steal.execute("UPDATE match_stat_windows SET lease_token='stolen'"); steal.commit(); steal.close()
            return normalise_player_stats(live_payload("LIVE"), match_provider_id, collected_at=NOW.isoformat(), afl_match_id=kwargs.get("afl_match_id"), canonical_match_status="LIVE")
    out=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(live_payload())), collector_factory=StealingCollector, clock=lambda: NOW, lane=DirectLane(path)).run_once()[0]
    assert out["status"] == "lost_lease"
    assert conn.execute("SELECT status FROM scrape_runs ORDER BY started_at DESC LIMIT 1").fetchone()[0] == "failed"
    assert conn.execute("SELECT attempt_count, lease_token FROM match_stat_windows").fetchone()[:] == (0, "stolen")
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 0
    assert_no_running_attempts(conn)


def test_status_reports_operational_state_and_windows(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    worker=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(live_payload())), clock=lambda: NOW, lane=DirectLane(path))
    status=worker.status()
    assert status["enabled"] is True
    assert status["active_attempt_count"] == 0
    assert status["live_cadence_seconds"] == 60

def test_read_only_status_endpoint_includes_operational_state_and_window_rows(db, monkeypatch):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    import scheduler.player_stat_polling as polling
    worker=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(live_payload())), clock=lambda: NOW, lane=DirectLane(path))
    monkeypatch.setattr(polling, "_worker_singleton", worker, raising=False)
    from scheduler.api import polling_status
    body=polling_status()
    assert body["read_only"] is True
    assert body["operational"]["active_attempt_count"] == 0
    assert body["operational"]["submitted_attempt_count"] == 0
    assert body["operational"]["queued_attempt_count"] == 0
    assert body["operational"]["accepting_claims"] is True
    assert body["operational"]["lifecycle_state"] == "running"
    assert body["operational"]["network_waiting_count"] == 0
    assert body["operational"]["active_network_request_count"] == 0
    assert body["operational"]["network_concurrency"] == 2
    assert body["operational"]["write_lane_pending"] is None
    assert body["operational"]["write_lane_active"] is None
    assert body["operational"]["live_cadence_seconds"] == 60
    assert body["windows"][0]["match_provider_id"] == "CD_M1"
    rendered=str(body).lower()
    assert "authorization" not in rendered and "cookie" not in rendered and "wmctok" not in rendered


def test_real_client_second_401_opens_domain_auth_pause_after_one_refresh(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()

    class Response:
        headers = {}
        def __init__(self, status, payload): self.status_code=status; self.payload=payload; self.text=""
        def json(self): return self.payload
    class Session:
        def __init__(self): self.responses=iter([Response(401, {}), Response(401, {})]); self.calls=[]
        def request(self, method, url, **kwargs):
            kwargs["headers"] = dict(kwargs["headers"])
            self.calls.append((method,url,kwargs)); return next(self.responses)
        def close(self): pass

    session=Session(); tokens=iter(["old", "new"])
    from afl_json.client import WMCTokenProvider
    real_client=AflJsonClient(session=session, token_provider=WMCTokenProvider(lambda: next(tokens)))
    class RealClientCollector:
        def __init__(self, client, *, clock): self.client=client
        def collect(self, match_provider_id, **kwargs):
            return self.client.get("match_player_statistics", path_parameters={"match_provider_id": match_provider_id})
    worker=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(real_client), collector_factory=RealClientCollector, clock=lambda: NOW, lane=DirectLane(path))
    assert worker.run_once()[0]["status"] == "auth_failed_paused"
    assert len(session.calls) == 2
    assert session.calls[0][2]["headers"] != session.calls[1][2]["headers"]
    assert worker.status()["auth_paused"] is True
    assert conn.execute("SELECT reason_code FROM match_stat_windows").fetchone()[0] == "auth_domain_paused"
    worker.close()


def test_network_metrics_distinguish_active_request_waiter_and_attempts(db):
    conn,path=db; add_two_due_live_matches(conn)
    entered=threading.Event(); release=threading.Event()
    class BlockingCollector:
        def __init__(self, client, *, clock): pass
        def collect(self, match_provider_id, **kwargs):
            entered.set(); release.wait(timeout=5)
            return normalise_player_stats(live_payload(), match_provider_id, collected_at=NOW.isoformat(), canonical_match_status="LIVE")
    worker=PlayerStatPollingWorker(settings=settings(max_workers=2, claim_limit=2, network_concurrency=1), window_settings=window_settings(), client_pool=Pool(Client()), collector_factory=BlockingCollector, clock=lambda: NOW, lane=DirectLane(path))
    runner=threading.Thread(target=worker.run_once); runner.start(); assert entered.wait(timeout=5)
    for _ in range(100):
        status=worker.status()
        if status["network_waiting_count"] == 1: break
        threading.Event().wait(.01)
    assert status["active_attempt_count"] == 2
    assert status["active_network_request_count"] == status["network_permits_in_use"] == 1
    assert status["network_waiting_count"] == 1
    release.set(); runner.join(timeout=5); worker.close()


def test_shutdown_drains_active_attempt_before_closing_pool(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    entered=threading.Event(); release=threading.Event(); pool=Pool(Client())
    class BlockingCollector:
        def __init__(self, client, *, clock): pass
        def collect(self, match_provider_id, **kwargs):
            entered.set(); release.wait(timeout=5)
            return normalise_player_stats(live_payload(), match_provider_id, collected_at=NOW.isoformat(), canonical_match_status="LIVE")
    worker=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=pool, collector_factory=BlockingCollector, clock=lambda: NOW, lane=DirectLane(path))
    runner=threading.Thread(target=worker.run_once); runner.start(); assert entered.wait(timeout=5)
    closer=threading.Thread(target=worker.close); closer.start(); threading.Event().wait(.05)
    assert pool.closed is False and closer.is_alive()
    release.set(); runner.join(timeout=5); closer.join(timeout=5)
    assert pool.closed is True and not closer.is_alive()
    assert worker.run_once() == []


def test_process_lifetime_executor_is_reused_and_closed(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    worker=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(live_payload())), clock=lambda: NOW, lane=DirectLane(path))
    executor=worker._executor
    worker.run_once()
    assert worker._executor is executor
    worker.close()
    assert executor._shutdown is True


def test_close_is_idempotent_and_status_reports_closed_worker(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    class CountingPool(Pool):
        def __init__(self, client): super().__init__(client); self.close_calls=0
        def close(self): self.close_calls += 1; super().close()
    pool=CountingPool(Client(live_payload()))
    worker=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=pool, clock=lambda: NOW, lane=DirectLane(path))
    worker.close(); worker.close()
    status=worker.status()
    assert pool.close_calls == 1
    assert status["accepting_claims"] is False
    assert status["lifecycle_state"] == "closed"
    assert status["submitted_attempt_count"] == status["queued_attempt_count"] == 0
    assert worker.run_once() == []


def test_run_once_waits_for_its_bounded_batch_and_reports_submission(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    entered=threading.Event(); release=threading.Event()
    class BlockingCollector:
        def __init__(self, client, *, clock): pass
        def collect(self, match_provider_id, **kwargs):
            entered.set(); release.wait(timeout=5)
            return normalise_player_stats(live_payload(), match_provider_id, collected_at=NOW.isoformat(), canonical_match_status="LIVE")
    worker=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client()), collector_factory=BlockingCollector, clock=lambda: NOW, lane=DirectLane(path))
    runner=threading.Thread(target=worker.run_once); runner.start(); assert entered.wait(timeout=5)
    assert runner.is_alive()
    status=worker.status()
    assert status["submitted_attempt_count"] == status["active_attempt_count"] == 1
    assert status["queued_attempt_count"] == 0
    release.set(); runner.join(timeout=5)
    assert not runner.is_alive() and worker.status()["submitted_attempt_count"] == 0
    worker.close()


def test_horizon_reconcile_stops_unresolved_window_without_network_request(db):
    conn,path=db; add_match(conn, status="CONCLUDED", start=(NOW-timedelta(hours=10)).isoformat())
    reconcile(conn, now=NOW-timedelta(hours=7), settings=window_settings()); conn.commit()
    reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    client=Client(live_payload("CONCLUDED"))
    worker=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(client), clock=lambda: NOW, lane=DirectLane(path))
    assert worker.run_once() == []
    row=conn.execute("SELECT status,finality_state,reason_code,next_due_at FROM match_stat_windows").fetchone()
    assert row[:] == ("failed_terminal", "unconfirmed", "polling_horizon_exceeded", None)
    assert client.calls == 0
    worker.close()


def test_restart_preserves_due_time_and_failure_backoff_but_not_process_auth_circuit(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    err=AflJsonAuthenticationError("failed", endpoint="match_player_statistics", status_code=401)
    first=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(error=err)), clock=lambda: NOW, lane=DirectLane(path))
    first.run_once(); stored=conn.execute("SELECT next_due_at,consecutive_failure_count FROM match_stat_windows").fetchone(); first.close()
    replacement_client=Client(live_payload())
    restarted=PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(replacement_client), clock=lambda: NOW+timedelta(minutes=1), lane=DirectLane(path))
    assert restarted.status()["auth_paused"] is False
    assert restarted.run_once() == []
    assert conn.execute("SELECT next_due_at,consecutive_failure_count FROM match_stat_windows").fetchone()[:] == stored[:]
    assert replacement_client.calls == 0
    restarted.close()


def assert_no_running_attempts(conn):
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM scheduler_job_registry WHERE status='running'"
        ).fetchone()[0]
        == 0
    )
    row = conn.execute(
        "SELECT job_id,attempt_id,scrape_run_id,lease_token,lease_generation FROM scheduler_job_registry ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    audit = conn.execute(
        "SELECT scheduler_job_id,attempt_id,run_id,lease_token,lease_generation FROM scrape_runs WHERE scrape_type='cfs_player_stats_poll' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert row is not None and audit is not None
    assert row[:] == (audit[0], audit[1], audit[2], audit[3], audit[4])


@pytest.mark.parametrize(
    "exception", [RuntimeError("unexpected"), ValueError("invalid response")]
)
def test_collection_failures_terminalise_the_explicit_registry_identity(db, exception):
    conn, path = db
    add_match(conn)
    reconcile(conn, now=NOW, settings=window_settings())
    conn.commit()

    class RaisingCollector:
        def __init__(self, client, *, clock):
            pass

        def collect(self, *args, **kwargs):
            raise exception

    PlayerStatPollingWorker(
        settings=settings(),
        window_settings=window_settings(),
        client_pool=Pool(Client()),
        collector_factory=RaisingCollector,
        clock=lambda: NOW,
        lane=DirectLane(path),
    ).run_once()
    assert_no_running_attempts(conn)
    assert conn.execute(
        "SELECT status,attempt_persistence_evidence FROM scheduler_job_registry"
    ).fetchone()[:] == ("failed", "uncommitted")


def test_atomic_finalisation_rollback_leaves_no_domain_or_completion_writes(db):
    conn, path = db
    add_match(conn)
    reconcile(conn, now=NOW, settings=window_settings())
    conn.commit()

    def crash(point):
        if point == "after_domain_write":
            raise RuntimeError("injected finalisation crash")

    PlayerStatPollingWorker(
        settings=settings(),
        window_settings=window_settings(),
        client_pool=Pool(Client(live_payload())),
        finalization_hook=crash,
        clock=lambda: NOW,
        lane=DirectLane(path),
    ).run_once()
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 0
    assert conn.execute(
        "SELECT status,response_received_at,persistence_committed_at,attempt_persistence_evidence FROM scrape_runs"
    ).fetchone()[:] == ("failed", NOW.isoformat(), None, "uncommitted")
    assert conn.execute(
        "SELECT status,attempt_persistence_evidence FROM scheduler_job_registry"
    ).fetchone()[:] == ("failed", "uncommitted")
    assert (
        conn.execute("SELECT finality_state FROM match_stat_windows").fetchone()[0]
        == "unconfirmed"
    )
    assert conn.execute("SELECT status,lease_token FROM match_stat_windows").fetchone()[:] == ("backoff", None)


def test_failed_t4_and_failed_failure_finalisation_remain_running_unknown(db):
    conn, path = db
    add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()

    class FailureLane(DirectLane):
        def execute(self, op, target, cb):
            if op == "player_stats_poll.persist_failure":
                raise RuntimeError("injected failure finalisation crash")
            return super().execute(op, target, cb)

    def crash(point):
        if point == "after_domain_write":
            raise RuntimeError("injected T4 crash")

    worker = PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(),
        client_pool=Pool(Client(live_payload())), finalization_hook=crash,
        clock=lambda: NOW, lane=FailureLane(path))
    with pytest.raises(RuntimeError, match="failure finalisation"):
        worker.run_once()
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 0
    assert conn.execute("SELECT status,response_received_at,attempt_persistence_evidence FROM scrape_runs").fetchone()[:] == ("running", NOW.isoformat(), None)
    assert conn.execute("SELECT status,attempt_persistence_evidence FROM scheduler_job_registry").fetchone()[:] == ("running", None)
    window = conn.execute("SELECT status,lease_token FROM match_stat_windows").fetchone()
    assert window[0] == "leased" and window[1] is not None


@pytest.mark.parametrize("mutation", [
    "DELETE FROM scheduler_job_registry",
    "UPDATE scheduler_job_registry SET status='failed'",
    "DELETE FROM scrape_runs",
    "UPDATE scrape_runs SET status='failed'",
])
def test_missing_or_preterminal_control_row_rolls_back_success(db, mutation):
    conn, path = db
    add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()

    class TamperLane(DirectLane):
        tampered = False
        def execute(self, op, target, cb):
            if op == "player_stats_poll.persist_success" and not self.tampered:
                with sqlite3.connect(self.path) as tamper:
                    tamper.execute(mutation)
                self.tampered = True
            return super().execute(op, target, cb)

    worker = PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(),
        client_pool=Pool(Client(live_payload())), clock=lambda: NOW, lane=TamperLane(path))
    with pytest.raises((RuntimeError, ValueError)):
        worker.run_once()
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 0
    assert conn.execute("SELECT status,lease_token FROM match_stat_windows").fetchone()[0] == "leased"


def test_period_state_provider_hook_tags_checkpoints_without_a_second_network_call(db):
    """Issue #195: the scheduler's optional MatchPeriodState hook must reach
    cfs_player_stat_history/checkpoints purely through the existing single
    upsert_player_stats() call inside this same attempt -- no additional
    client.get() call, no second scheduler/poller path."""
    conn, path = db
    add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    client = Client(live_payload()); lane = DirectLane(path)
    worker = PlayerStatPollingWorker(
        settings=settings(), window_settings=window_settings(), client_pool=Pool(client),
        clock=lambda: NOW, lane=lane,
        period_state_provider=lambda row: MatchPeriodState.HALF_TIME,
    )
    out = worker.run_once()[0]
    assert out["status"] == "rescheduled"
    assert client.calls == 1
    markers = {row[0] for row in conn.execute("SELECT DISTINCT checkpoint_marker FROM cfs_player_stat_checkpoints")}
    assert markers == {"BASELINE", "HT"}


def test_period_state_provider_failure_is_swallowed_and_never_blocks_persistence(db):
    conn, path = db
    add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    client = Client(live_payload()); lane = DirectLane(path)

    def broken_provider(row):
        raise RuntimeError("boom")

    worker = PlayerStatPollingWorker(
        settings=settings(), window_settings=window_settings(), client_pool=Pool(client),
        clock=lambda: NOW, lane=lane, period_state_provider=broken_provider,
    )
    out = worker.run_once()[0]
    assert out["status"] == "rescheduled"
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 2
    markers = {row[0] for row in conn.execute("SELECT DISTINCT checkpoint_marker FROM cfs_player_stat_checkpoints")}
    assert markers == {"BASELINE"}


def test_default_worker_has_no_period_state_provider_and_unchanged_network_call_count(db):
    conn, path = db
    add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    client = Client(live_payload()); lane = DirectLane(path)
    worker = PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(),
        client_pool=Pool(client), clock=lambda: NOW, lane=lane)
    assert worker.period_state_provider is None
    worker.run_once()
    assert client.calls == 1


def test_polling_import_has_no_runtime_database_side_effect_and_identity_is_process_specific(tmp_path):
    database = tmp_path / "must-not-exist.db"
    env = {**os.environ, "DB_PATH": str(database)}
    command = [sys.executable, "-c", "from scheduler.runtime import INSTANCE_ID; import scheduler.player_stat_polling; print(INSTANCE_ID)"]
    first = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
    second = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
    assert not database.exists()
    assert first.stdout.strip() != second.stdout.strip()
