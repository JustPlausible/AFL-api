"""Conservative lifecycle-driven CFS player-stat polling worker."""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import config
from afl_json.client import (AflJsonAuthenticationError, AflJsonClient, AflJsonHttpError,
                             AflJsonInvalidResponse, AflJsonTransportError,
                             HttpPolicy, WMCTokenProvider)
from afl_json.player_stats import MatchPlayerStatsCollector, PlayerStatsStatus, upsert_player_stats
from db.scrape_runs import (complete_scrape_run, fail_scrape_run, record_scrape_decision,
                            sanitize_error_summary, scheduler_job_context, start_scrape_run)
from scheduler.match_windows import (FinalityState, MatchWindowSettings,
                                     ReasonCode, _finality, _iso, claim_due_windows,
                                     release_window)
from scheduler.write_lane import write_lane

Clock = Callable[[], datetime]
Jitter = Callable[[str, str, int], timedelta]


@dataclass(frozen=True)
class PlayerStatPollingSettings:
    enabled: bool = False
    kill_switch: bool = False
    drain: bool = False
    max_workers: int = 2
    network_concurrency: int = 2
    claim_limit: int = 2
    live_cadence: timedelta = timedelta(seconds=60)
    pre_match_cadence: timedelta = timedelta(minutes=5)
    post_match_cadence: timedelta = timedelta(minutes=2)
    unavailable_cadence: timedelta = timedelta(minutes=5)
    partial_cadence: timedelta = timedelta(minutes=2)
    transient_backoff: timedelta = timedelta(minutes=5)
    rate_limit_backoff: timedelta = timedelta(minutes=10)
    auth_pause: timedelta = timedelta(minutes=30)
    max_backoff: timedelta = timedelta(hours=1)
    jitter_seconds: int = 5
    allowed_competitions: tuple[str, ...] = ()
    allowed_seasons: tuple[str, ...] = ()
    allowed_matches: tuple[str, ...] = ()

    @classmethod
    def from_config(cls) -> "PlayerStatPollingSettings":
        return cls(
            enabled=config.AFL_PLAYER_STAT_POLLING_ENABLED,
            kill_switch=config.AFL_PLAYER_STAT_POLLING_KILL_SWITCH,
            drain=config.AFL_PLAYER_STAT_POLLING_DRAIN,
            max_workers=config.AFL_PLAYER_STAT_POLLING_MAX_WORKERS,
            network_concurrency=config.AFL_PLAYER_STAT_POLLING_NETWORK_CONCURRENCY,
            claim_limit=config.AFL_PLAYER_STAT_POLLING_CLAIM_LIMIT,
            live_cadence=timedelta(seconds=config.AFL_PLAYER_STAT_POLLING_LIVE_SECONDS),
            pre_match_cadence=timedelta(seconds=config.AFL_PLAYER_STAT_POLLING_PRE_MATCH_SECONDS),
            post_match_cadence=timedelta(seconds=config.AFL_PLAYER_STAT_POLLING_POST_MATCH_SECONDS),
            unavailable_cadence=timedelta(seconds=config.AFL_PLAYER_STAT_POLLING_UNAVAILABLE_SECONDS),
            partial_cadence=timedelta(seconds=config.AFL_PLAYER_STAT_POLLING_PARTIAL_SECONDS),
            transient_backoff=timedelta(seconds=config.AFL_PLAYER_STAT_POLLING_TRANSIENT_BACKOFF_SECONDS),
            rate_limit_backoff=timedelta(seconds=config.AFL_PLAYER_STAT_POLLING_RATE_LIMIT_BACKOFF_SECONDS),
            auth_pause=timedelta(seconds=config.AFL_PLAYER_STAT_POLLING_AUTH_PAUSE_SECONDS),
            max_backoff=timedelta(seconds=config.AFL_PLAYER_STAT_POLLING_MAX_BACKOFF_SECONDS),
            jitter_seconds=config.AFL_PLAYER_STAT_POLLING_JITTER_SECONDS,
            allowed_competitions=tuple(config.AFL_PLAYER_STAT_POLLING_ALLOWED_COMPETITIONS),
            allowed_seasons=tuple(config.AFL_PLAYER_STAT_POLLING_ALLOWED_SEASONS),
            allowed_matches=tuple(config.AFL_PLAYER_STAT_POLLING_ALLOWED_MATCHES),
        )


def deterministic_jitter(series_id: str, phase: str, seconds: int) -> timedelta:
    if seconds <= 0:
        return timedelta(0)
    digest = hashlib.sha256(f"{series_id}:{phase}".encode()).digest()
    return timedelta(seconds=int.from_bytes(digest[:4], "big") % (seconds + 1))


class SynchronizedWMCTokenProvider(WMCTokenProvider):
    """Thread-safe process-local token provider over a public token acquisition callable."""

    def __init__(self, acquire: Callable[[], str]):
        super().__init__(acquire)
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            return super().get_token()

    def invalidate(self) -> None:
        with self._lock:
            super().invalidate()


class SchedulerCfsClientPool:
    """Per-thread HTTP sessions with one shared scheduler-process CFS token."""

    def __init__(self, *, policy: HttpPolicy | None = None,
                 token_acquirer: Callable[[], str] | None = None):
        self._token_bootstrap = AflJsonClient(policy=policy)
        self._provider = SynchronizedWMCTokenProvider(
            token_acquirer or self._token_bootstrap.acquire_wmc_token
        )
        self._policy = policy
        self._local = threading.local()
        self._lock = threading.Lock()
        self._clients: set[AflJsonClient] = set()
        self._closed = False

    def client(self) -> AflJsonClient:
        if self._closed:
            raise RuntimeError("scheduler CFS client pool is closed")
        client = getattr(self._local, "client", None)
        if client is None:
            client = AflJsonClient(policy=self._policy, token_provider=self._provider)
            self._local.client = client
            with self._lock:
                self._clients.add(client)
        return client

    def close(self) -> None:
        with self._lock:
            self._closed = True
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            client.close()
        self._token_bootstrap.close()


def _allowed(row: dict[str, Any], settings: PlayerStatPollingSettings) -> bool:
    return ((not settings.allowed_competitions or str(row.get("competition_id")) in settings.allowed_competitions)
            and (not settings.allowed_seasons or str(row.get("season_id")) in settings.allowed_seasons)
            and (not settings.allowed_matches or str(row.get("match_id")) in settings.allowed_matches or str(row.get("match_provider_id")) in settings.allowed_matches))


def cadence_for(row: dict[str, Any], status: PlayerStatsStatus | None, settings: PlayerStatPollingSettings) -> tuple[timedelta, str]:
    lifecycle = str(row.get("lifecycle") or "").upper()
    if status is PlayerStatsStatus.UNAVAILABLE:
        return settings.unavailable_cadence, "unpublished_or_unavailable"
    if status in {PlayerStatsStatus.EMPTY, PlayerStatsStatus.UNKNOWN}:
        return settings.partial_cadence, f"{status.value}_result"
    if status is PlayerStatsStatus.LIVE_PARTIAL:
        return (settings.live_cadence if lifecycle == "LIVE" else settings.partial_cadence), "live_partial"
    if lifecycle == "LIVE":
        return settings.live_cadence, "live"
    if lifecycle in {"POSTGAME", "CONCLUDED"}:
        return settings.post_match_cadence, "post_match_awaiting_final"
    return settings.pre_match_cadence, "pre_match_awaiting_live"


def _failure_backoff(row: dict[str, Any], base: timedelta, settings: PlayerStatPollingSettings) -> timedelta:
    failures = max(0, int(row.get("consecutive_failure_count") or 0))
    seconds = min(settings.max_backoff.total_seconds(), base.total_seconds() * (2 ** failures))
    return timedelta(seconds=seconds)


class PlayerStatPollingWorker:
    def __init__(self, *, settings: PlayerStatPollingSettings | None = None,
                 window_settings: MatchWindowSettings | None = None,
                 client_pool: SchedulerCfsClientPool | None = None,
                 collector_factory=MatchPlayerStatsCollector,
                 clock: Clock | None = None,
                 jitter: Jitter = deterministic_jitter,
                 lane=write_lane):
        self.settings = settings or PlayerStatPollingSettings.from_config()
        self.window_settings = window_settings or MatchWindowSettings.from_config()
        self.client_pool = client_pool or SchedulerCfsClientPool()
        self.collector_factory = collector_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.jitter = jitter
        self.lane = lane
        self._network = threading.BoundedSemaphore(max(1, self.settings.network_concurrency))
        self._state = threading.Lock()
        self._lifecycle = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, self.settings.max_workers),
            thread_name_prefix="cfs-player-stat",
        )
        self._accepting_claims = True
        self._lifecycle_state = "running"
        self._submitted_attempt_count = 0
        self._active_attempts: dict[str, dict[str, Any]] = {}
        self._network_waiters = 0
        self._active_network_requests = 0
        self._auth_paused_until: datetime | None = None

    def status(self) -> dict[str, Any]:
        now = self.clock().astimezone(timezone.utc)
        with self._state:
            active = list(self._active_attempts.values())
            auth_until = self._auth_paused_until
            waiting = self._network_waiters
            network_active = self._active_network_requests
            accepting = self._accepting_claims
            lifecycle_state = self._lifecycle_state
            submitted = self._submitted_attempt_count
        return {
            "enabled": self.settings.enabled,
            "kill_switch": self.settings.kill_switch,
            "drain": self.settings.drain,
            "max_workers": self.settings.max_workers,
            "network_concurrency": self.settings.network_concurrency,
            "active_attempt_count": len(active),
            "active_attempts": active,
            "submitted_attempt_count": submitted,
            "queued_attempt_count": max(0, submitted - len(active)),
            "accepting_claims": accepting,
            "lifecycle_state": lifecycle_state,
            "network_waiting_count": waiting,
            "active_network_request_count": network_active,
            "network_permits_in_use": network_active,
            "auth_paused": bool(auth_until and auth_until > now),
            "auth_paused_until": _iso(auth_until) if auth_until else None,
            "write_lane_pending": getattr(self.lane, "pending_count", None),
            "write_lane_active": getattr(self.lane, "active_count", None),
            "live_cadence_seconds": int(self.settings.live_cadence.total_seconds()),
            "jitter_seconds": self.settings.jitter_seconds,
        }

    def _auth_pause_active(self, now: datetime) -> bool:
        with self._state:
            return bool(self._auth_paused_until and self._auth_paused_until > now)

    def claim_due(self) -> list[dict[str, Any]]:
        now = self.clock().astimezone(timezone.utc)
        with self._state:
            accepting = self._accepting_claims
        if not accepting or not self.settings.enabled or self.settings.kill_switch or self.settings.drain:
            reason = "shutdown" if not accepting else "disabled" if not self.settings.enabled else "kill_switch" if self.settings.kill_switch else "drain"
            record_scrape_decision("cfs_player_stats_poll", target_type="domain", target_identifier="player_stats", reason_code=reason, decision_class="safe", correlation_id=f"poll_skip_{reason}_{int(now.timestamp())}", trigger_source="scheduler")
            return []
        if self._auth_pause_active(now):
            record_scrape_decision("cfs_player_stats_poll", target_type="domain", target_identifier="player_stats", reason_code=ReasonCode.AUTH_DOMAIN_PAUSED.value, decision_class="safe", correlation_id=f"poll_skip_auth_paused_{int(now.timestamp())}", trigger_source="scheduler")
            return []
        claimed = claim_due_windows(f"player-stat-poller:{uuid.uuid4().hex[:8]}", limit=min(self.settings.claim_limit, self.settings.max_workers), now=now, settings=self.window_settings, lane=self.lane)
        accepted: list[dict[str, Any]] = []
        for row in claimed:
            if _allowed(row, self.settings):
                accepted.append(row)
            else:
                self.lane.execute("player_stats_poll.release_disallowed", row["window_id"], lambda conn, r=row: release_window(conn, r["window_id"], r["lease_token"], now=now))
        return accepted

    def run_once(self) -> list[dict[str, Any]]:
        with self._lifecycle:
            claims = self.claim_due()
            if not claims:
                return []
            futures = [self._executor.submit(self.run_claim, row) for row in claims]
            with self._state:
                self._submitted_attempt_count += len(futures)
        try:
            return [future.result() for future in as_completed(futures)]
        finally:
            with self._state:
                self._submitted_attempt_count -= len(futures)

    @contextmanager
    def _network_permit(self):
        with self._state:
            self._network_waiters += 1
        self._network.acquire()
        with self._state:
            self._network_waiters -= 1
            self._active_network_requests += 1
        try:
            yield
        finally:
            with self._state:
                self._active_network_requests -= 1
            self._network.release()

    def run_claim(self, row: dict[str, Any]) -> dict[str, Any]:
        attempt = row["attempt_id"]
        job_id = row["scheduler_job_id"]
        started_at = self.clock().astimezone(timezone.utc)
        with self._state:
            self._active_attempts[attempt] = {
                "attempt_id": attempt,
                "window_id": row["window_id"],
                "match_id": row.get("match_id"),
                "match_provider_id": row.get("match_provider_id"),
                "started_at": _iso(started_at),
            }
        run_id = start_scrape_run("cfs_player_stats_poll", target_type="match", target_identifier=row["match_id"], trigger_source="scheduler", correlation_id=attempt)
        try:
            lifecycle = str(row.get("lifecycle") or "").upper()
            if lifecycle not in {"LIVE", "POSTGAME", "CONCLUDED"}:
                return self._persist_skip(row, run_id, "awaiting_authoritative_live", self.settings.pre_match_cadence, failure=False)
            started = time.monotonic()
            try:
                with scheduler_job_context(job_id):
                    with self._network_permit():
                        result = self.collector_factory(self.client_pool.client(), clock=self.clock).collect(row["match_provider_id"], afl_match_id=row.get("afl_match_id"), canonical_match_status=row.get("lifecycle"))
                network_ms = int((time.monotonic() - started) * 1000)
                return self._persist_success(row, result, run_id, network_ms)
            except AflJsonAuthenticationError as exc:
                return self._persist_failure(row, run_id, exc, _failure_backoff(row, self.settings.auth_pause, self.settings), "auth_failed_paused", set_auth_pause=True)
            except AflJsonHttpError as exc:
                base = self.settings.rate_limit_backoff if exc.status_code == 429 else self.settings.transient_backoff
                return self._persist_failure(row, run_id, exc, _failure_backoff(row, base, self.settings), "http_429" if exc.status_code == 429 else "http_failure")
            except (AflJsonTransportError, AflJsonInvalidResponse, ValueError) as exc:
                return self._persist_failure(row, run_id, exc, _failure_backoff(row, self.settings.transient_backoff, self.settings), "collector_failure")
            except BaseException as exc:
                return self._persist_failure(row, run_id, exc, _failure_backoff(row, self.settings.transient_backoff, self.settings), ReasonCode.INTERRUPTED.value)
        finally:
            with self._state:
                self._active_attempts.pop(attempt, None)

    def _persist_skip(self, row, run_id: str, reason: str, delay: timedelta, *, failure: bool) -> dict[str, Any]:
        now = self.clock().astimezone(timezone.utc)
        next_due = now + delay + self.jitter(row["window_id"], reason, self.settings.jitter_seconds)
        def op(conn):
            reason_code = (ReasonCode.AWAITING_AUTHORITATIVE_LIVE.value
                           if reason == "awaiting_authoritative_live"
                           else ReasonCode.ATTEMPT_SUCCEEDED_NON_FINAL.value)
            cur = conn.execute("""UPDATE match_stat_windows SET status='due', next_due_at=?, cadence_profile=?, last_attempted_at=?, reason_code=?, diagnostic_summary=?, lease_owner=NULL, lease_token=NULL, lease_claimed_at=NULL, lease_expires_at=NULL, updated_at=? WHERE window_id=? AND lease_token=?""",
                               (_iso(next_due), reason, _iso(now), reason_code, reason, _iso(now), row["window_id"], row["lease_token"]))
            if cur.rowcount != 1:
                fail_scrape_run(run_id, "lost lease before skip persistence", conn=conn)
                return {"status": "lost_lease", "scrape_run_id": run_id}
            complete_scrape_run(run_id, rows_read=0, rows_written=0, conn=conn)
            return {"status": reason, "next_due_at": _iso(next_due), "scrape_run_id": run_id}
        return self.lane.execute("player_stats_poll.persist_skip", row["window_id"], op)

    def _persist_success(self, row, result, run_id: str, network_ms: int) -> dict[str, Any]:
        now = self.clock().astimezone(timezone.utc)
        failure_like = result.status in {PlayerStatsStatus.EMPTY, PlayerStatsStatus.UNKNOWN}
        cadence, phase_reason = cadence_for(row, result.status, self.settings)
        next_due = now + cadence + self.jitter(row["window_id"], phase_reason, self.settings.jitter_seconds)
        def op(conn):
            owned = conn.execute(
                "SELECT 1 FROM match_stat_windows WHERE window_id=? AND lease_token=?",
                (row["window_id"], row["lease_token"]),
            ).fetchone()
            if owned is None:
                fail_scrape_run(run_id, ReasonCode.LOST_LEASE.value, conn=conn)
                return {"status": ReasonCode.LOST_LEASE.value, "rows_written": 0, "scrape_run_id": run_id}
            before_finality, before_auth = _finality(conn, row["match_provider_id"])
            if before_finality is FinalityState.AUTHORITATIVE_COMPLETE and result.status is not PlayerStatsStatus.CONCLUDED:
                written = 0
                finality, auth = before_finality, before_auth
            else:
                written = upsert_player_stats(conn, result)
                finality, auth = _finality(conn, row["match_provider_id"])
            complete = finality is FinalityState.AUTHORITATIVE_COMPLETE
            status = "complete" if complete else "backoff" if failure_like else "awaiting_final"
            reason = (ReasonCode.AUTHORITATIVE_FINAL_CONFIRMED.value if complete
                      else ReasonCode.EMPTY_RESULT.value if result.status is PlayerStatsStatus.EMPTY
                      else ReasonCode.UNKNOWN_RESULT.value if result.status is PlayerStatsStatus.UNKNOWN
                      else ReasonCode.FINAL_STATS_UNAVAILABLE_OR_PARTIAL.value if result.status is PlayerStatsStatus.UNAVAILABLE
                      else ReasonCode.ATTEMPT_SUCCEEDED_NON_FINAL.value)
            cur = conn.execute("""UPDATE match_stat_windows SET status=?, collection_phase=CASE WHEN ? THEN 'complete' ELSE collection_phase END, next_due_at=?, cadence_profile=?, attempt_count=attempt_count+1, consecutive_failure_count=CASE WHEN ? THEN consecutive_failure_count+1 ELSE 0 END, last_attempted_at=?, last_successful_collection_at=CASE WHEN ? THEN last_successful_collection_at ELSE ? END, last_successful_write_at=CASE WHEN ? > 0 THEN ? ELSE last_successful_write_at END, last_observed_snapshot_authority=?, finality_state=?, reason_code=?, diagnostic_summary=?, lease_owner=NULL, lease_token=NULL, lease_claimed_at=NULL, lease_expires_at=NULL, updated_at=? WHERE window_id=? AND lease_token=?""",
                               (status, complete, None if complete else _iso(next_due), phase_reason, failure_like, _iso(now), failure_like, _iso(now), written, _iso(now), auth, finality.value, reason, f"outcome={result.status.value}; records={len(result.records)}; rejected={result.rejected_records}; network_ms={network_ms}; next_due={_iso(next_due) if not complete else None}", _iso(now), row["window_id"], row["lease_token"]))
            if cur.rowcount != 1:
                fail_scrape_run(run_id, "lost lease before success persistence", conn=conn)
                return {"status": "lost_lease", "rows_written": 0, "scrape_run_id": run_id}
            complete_scrape_run(run_id, rows_read=len(result.records), rows_written=written, partial=(result.status in {PlayerStatsStatus.LIVE_PARTIAL, PlayerStatsStatus.UNKNOWN, PlayerStatsStatus.EMPTY}), conn=conn)
            return {"status": "complete" if complete else "rejected_backoff" if failure_like else "rescheduled", "rows_written": written, "next_due_at": None if complete else _iso(next_due), "scrape_run_id": run_id}
        return self.lane.execute("player_stats_poll.persist_success", row["window_id"], op)

    def _persist_failure(self, row, run_id: str, exc: BaseException, backoff: timedelta, reason: str, *, set_auth_pause: bool = False) -> dict[str, Any]:
        now = self.clock().astimezone(timezone.utc)
        next_due = now + backoff + self.jitter(row["window_id"], reason, self.settings.jitter_seconds)
        summary = sanitize_error_summary(exc)
        if set_auth_pause:
            with self._state:
                self._auth_paused_until = next_due
        def op(conn):
            reason_code = (ReasonCode.AUTH_DOMAIN_PAUSED.value if set_auth_pause
                           else ReasonCode.INTERRUPTED.value if reason == ReasonCode.INTERRUPTED.value
                           else ReasonCode.ATTEMPT_FAILED_BACKOFF.value)
            cur = conn.execute("""UPDATE match_stat_windows SET status='backoff', next_due_at=?, cadence_profile=?, attempt_count=attempt_count+1, consecutive_failure_count=consecutive_failure_count+1, last_attempted_at=?, reason_code=?, diagnostic_summary=?, lease_owner=NULL, lease_token=NULL, lease_claimed_at=NULL, lease_expires_at=NULL, updated_at=? WHERE window_id=? AND lease_token=?""",
                               (_iso(next_due), reason, _iso(now), reason_code, f"{reason}: {summary}; next_due={_iso(next_due)}", _iso(now), row["window_id"], row["lease_token"]))
            if cur.rowcount != 1:
                fail_scrape_run(run_id, "lost lease before failure persistence", conn=conn)
                return {"status": "lost_lease", "next_due_at": _iso(next_due), "scrape_run_id": run_id}
            fail_scrape_run(run_id, exc, conn=conn)
            return {"status": reason, "next_due_at": _iso(next_due), "scrape_run_id": run_id}
        return self.lane.execute("player_stats_poll.persist_failure", row["window_id"], op)

    def close(self) -> None:
        """Drain submitted attempts before closing their thread-owned sessions."""
        with self._lifecycle:
            with self._state:
                if self._lifecycle_state == "closed":
                    return
                self._accepting_claims = False
                self._lifecycle_state = "closing"
            self._executor.shutdown(wait=True, cancel_futures=False)
            self.client_pool.close()
            with self._state:
                self._lifecycle_state = "closed"


_worker_lock = threading.Lock()
_worker_singleton: PlayerStatPollingWorker | None = None


def get_player_stat_polling_worker() -> PlayerStatPollingWorker:
    global _worker_singleton
    with _worker_lock:
        if _worker_singleton is None:
            _worker_singleton = PlayerStatPollingWorker()
        return _worker_singleton


def shutdown_player_stat_polling_worker() -> None:
    global _worker_singleton
    with _worker_lock:
        worker = _worker_singleton
        _worker_singleton = None
    if worker is not None:
        worker.close()
