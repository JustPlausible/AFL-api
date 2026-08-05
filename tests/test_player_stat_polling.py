from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from afl_json.client import AflJsonAuthenticationError
from afl_json.player_stats import MatchPlayerStatsCollector, PlayerStatsStatus, normalise_player_stats
from db.migration_runner import migrate_database
from scheduler.match_windows import MatchWindowSettings, reconcile, window_id
from scheduler.player_stat_polling import PlayerStatPollingSettings, PlayerStatPollingWorker, deterministic_jitter

NOW = datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)

class DirectLane:
    def __init__(self, path): self.path = path; self.active = 0; self.max_active = 0; self.network_during_write = False
    def execute(self, op, target, cb): return self._run(cb)
    def execute_immediate(self, op, target, cb): return self._run(cb, immediate=True)
    def _run(self, cb, immediate=False):
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
    assert row["cadence_profile"] == "live"
    assert lane.max_active == 1

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
    for lifecycle, status, expected in [("LIVE", PlayerStatsStatus.LIVE_PARTIAL, "live"),("CONCLUDED", PlayerStatsStatus.UNAVAILABLE, "unpublished_or_unavailable"),("POSTGAME", PlayerStatsStatus.CONCLUDED, "post_match_awaiting_final")]:
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
    assert conn.execute("SELECT status,finality_state FROM match_stat_windows").fetchone()[:] == ("complete","authoritative_complete")
    before=conn.execute("SELECT COUNT(*), MIN(snapshot_authority) FROM cfs_player_stats").fetchone()
    conn.execute("UPDATE match_stat_windows SET status='due', next_due_at=?, lease_token=NULL", (NOW.isoformat(),)); conn.commit()
    PlayerStatPollingWorker(settings=settings(), window_settings=window_settings(), client_pool=Pool(Client(live_payload("LIVE"))), clock=lambda: NOW+timedelta(minutes=1), lane=DirectLane(path)).run_once()
    assert conn.execute("SELECT COUNT(*), MIN(snapshot_authority) FROM cfs_player_stats").fetchone() == before

def test_controls_allowlist_and_drain(db):
    conn,path=db; add_match(conn); reconcile(conn, now=NOW, settings=window_settings()); conn.commit()
    assert PlayerStatPollingWorker(settings=settings(enabled=False), client_pool=Pool(Client(live_payload())), lane=DirectLane(path), clock=lambda: NOW).run_once() == []
    assert PlayerStatPollingWorker(settings=settings(allowed_matches=("999",)), client_pool=Pool(Client(live_payload())), lane=DirectLane(path), clock=lambda: NOW).run_once() == []
    assert PlayerStatPollingWorker(settings=settings(drain=True), client_pool=Pool(Client(live_payload())), lane=DirectLane(path), clock=lambda: NOW).run_once() == []
