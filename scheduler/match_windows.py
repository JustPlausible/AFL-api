"""Durable lifecycle-driven match-window planning and lease state.

A match window is a polling series.  It is intentionally separate from an
APScheduler job (one attempt) and a scrape_runs row (one collection audit).
Transitions are machine-code driven; display text is never used for recovery.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import config
from afl_json.season_report import authoritative_stats_finality_for_match
from afl_json.match_status import normalise_match_status
from scheduler.time_policy import MetadataTimestampError, parse_metadata_timestamp
from scheduler.write_lane import write_lane

ACTIVE_STATUSES = ("planned", "due", "leased", "backoff", "awaiting_final", "planning_error", "disabled")
TERMINAL_STATUSES = ("complete", "failed_terminal", "cancelled", "not_applicable")
PLANNER_VERSION = "match_window_planner_v1"
DEFAULT_POLICY_VERSION = "cfs_match_stats_v1"


class MatchWindowStatus(str, Enum):
    PLANNED = "planned"
    DUE = "due"
    LEASED = "leased"
    BACKOFF = "backoff"
    AWAITING_FINAL = "awaiting_final"
    COMPLETE = "complete"
    PLANNING_ERROR = "planning_error"
    FAILED_TERMINAL = "failed_terminal"
    DISABLED = "disabled"
    CANCELLED = "cancelled"
    NOT_APPLICABLE = "not_applicable"


class CollectionPhase(str, Enum):
    NOT_STARTED = "not_started"
    PRE_MATCH = "pre_match"
    LIVE = "live"
    POST_GAME = "post_game"
    FINAL_CONFIRMATION = "final_confirmation"
    COMPLETE = "complete"
    NONE = "none"


class FinalityState(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    UNCONFIRMED = "unconfirmed"
    PARTIAL = "partial"
    AUTHORITATIVE_COMPLETE = "authoritative_complete"


class CadenceProfile(str, Enum):
    NONE = "none"
    PRE_MATCH_PLACEHOLDER = "pre_match_placeholder"
    LIVE_PLACEHOLDER = "live_placeholder"
    POST_GAME_PLACEHOLDER = "post_game_placeholder"
    FINAL_PLACEHOLDER = "final_placeholder"


class ReasonCode(str, Enum):
    FUTURE_OUTSIDE_WINDOW = "future_outside_window"
    APPROACHING_START = "approaching_start"
    LIVE = "live"
    AWAITING_FINAL = "awaiting_final"
    AUTHORITATIVE_FINAL_CONFIRMED = "authoritative_final_confirmed"
    FINAL_STATS_UNAVAILABLE_OR_PARTIAL = "final_stats_unavailable_or_partial"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    UNKNOWN_LIFECYCLE = "unknown_lifecycle"
    CONTRADICTORY_LIFECYCLE = "contradictory_lifecycle"
    MISSING_START_TIME = "missing_start_time"
    MISSING_PROVIDER_IDENTITY = "missing_provider_identity"
    POLLING_HORIZON_EXCEEDED = "polling_horizon_exceeded"
    FEATURE_DISABLED = "feature_disabled"
    UNSUPPORTED_COMPETITION_OR_SEASON = "unsupported_competition_or_season"
    LEASE_EXPIRED_RECLAIMED = "lease_expired_reclaimed"
    ATTEMPT_FAILED_BACKOFF = "attempt_failed_backoff"
    ATTEMPT_SUCCEEDED_NON_FINAL = "attempt_succeeded_non_final"
    AWAITING_AUTHORITATIVE_LIVE = "awaiting_authoritative_live"
    AUTH_DOMAIN_PAUSED = "auth_domain_paused"
    EMPTY_RESULT = "empty_result"
    UNKNOWN_RESULT = "unknown_result"
    LOST_LEASE = "lost_lease"
    INTERRUPTED = "interrupted"
    RELEASED = "released"


@dataclass(frozen=True)
class MatchWindowSettings:
    enabled: bool = True
    pre_match_window: timedelta = timedelta(hours=2)
    post_match_horizon: timedelta = timedelta(hours=12)
    lease_duration: timedelta = timedelta(minutes=15)
    reconciliation_interval: timedelta = timedelta(minutes=30)
    expected_match_duration: timedelta = timedelta(hours=3)
    supported_competitions: tuple[str, ...] = ()
    supported_seasons: tuple[str, ...] = ()
    policy_version: str = DEFAULT_POLICY_VERSION
    planner_version: str = PLANNER_VERSION

    @classmethod
    def from_config(cls) -> "MatchWindowSettings":
        settings = cls(
            enabled=config.AFL_MATCH_WINDOW_PLANNER_ENABLED,
            pre_match_window=timedelta(seconds=config.AFL_MATCH_WINDOW_PRE_MATCH_SECONDS),
            post_match_horizon=timedelta(seconds=config.AFL_MATCH_WINDOW_POST_HORIZON_SECONDS),
            lease_duration=timedelta(seconds=config.AFL_MATCH_WINDOW_LEASE_SECONDS),
            reconciliation_interval=timedelta(seconds=config.AFL_MATCH_WINDOW_RECONCILE_SECONDS),
            expected_match_duration=timedelta(seconds=config.AFL_MATCH_WINDOW_EXPECTED_MATCH_SECONDS),
            supported_competitions=tuple(config.AFL_MATCH_WINDOW_SUPPORTED_COMPETITIONS),
            supported_seasons=tuple(config.AFL_MATCH_WINDOW_SUPPORTED_SEASONS),
            policy_version=config.AFL_MATCH_WINDOW_POLICY_VERSION,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if min(self.pre_match_window, self.post_match_horizon, self.lease_duration, self.reconciliation_interval, self.expected_match_duration) < timedelta(0):
            raise ValueError("match-window durations must be non-negative")
        if self.lease_duration <= timedelta(0):
            raise ValueError("match-window lease duration must be positive")


@dataclass(frozen=True)
class Decision:
    status: MatchWindowStatus
    phase: CollectionPhase
    next_due: datetime | None
    cadence: CadenceProfile
    finality: FinalityState
    reason: ReasonCode
    snapshot_authority: int | None = None
    diagnostic: str | None = None


def window_id(match_id: int, policy_version: str = DEFAULT_POLICY_VERSION) -> str:
    return f"mw_cfs_stats_{int(match_id)}_{policy_version}"


def attempt_id(series_id: str, generation: int, attempt_count: int) -> str:
    return f"{series_id}_attempt_{generation}_{attempt_count + 1}"


def scheduler_job_id(series_id: str, generation: int, attempt_count: int) -> str:
    return f"mw_attempt_{series_id}_{generation}_{attempt_count + 1}"


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _finality(conn: sqlite3.Connection, match_provider_id: str | None) -> tuple[FinalityState, int | None]:
    finality = authoritative_stats_finality_for_match(conn, match_provider_id)
    if finality.has_satisfactory_concluded_coverage:
        return FinalityState.AUTHORITATIVE_COMPLETE, 2
    if finality.has_authoritative_snapshot or finality.max_authority:
        return FinalityState.PARTIAL, finality.max_authority
    return FinalityState.UNCONFIRMED, None


def _horizon_anchor(row: sqlite3.Row, start: datetime, settings: MatchWindowSettings) -> datetime:
    observed = _dt(row["lifecycle_observed_at"]) if "lifecycle_observed_at" in row.keys() else None
    status = normalise_match_status(row["status"])
    if status in {"POSTGAME", "CONCLUDED"} and observed is not None:
        return observed
    return start + settings.expected_match_duration


def evaluate(row: sqlite3.Row, now: datetime, settings: MatchWindowSettings, finality: FinalityState, snapshot_authority: int | None) -> Decision:
    if not settings.enabled:
        return Decision(MatchWindowStatus.DISABLED, CollectionPhase.NONE, None, CadenceProfile.NONE, finality, ReasonCode.FEATURE_DISABLED, snapshot_authority)
    comp = str(row["competition_id"] or "")
    season = str(row["season_id"] or "")
    if (settings.supported_competitions and comp not in settings.supported_competitions) or (settings.supported_seasons and season not in settings.supported_seasons):
        return Decision(MatchWindowStatus.NOT_APPLICABLE, CollectionPhase.NONE, None, CadenceProfile.NONE, finality, ReasonCode.UNSUPPORTED_COMPETITION_OR_SEASON, snapshot_authority)
    raw_status = row["status"]
    status = normalise_match_status(raw_status)
    if raw_status and str(raw_status).upper() in {"POSTPONED", "DELAYED"}:
        return Decision(MatchWindowStatus.BACKOFF, CollectionPhase.NONE, None, CadenceProfile.NONE, finality, ReasonCode.POSTPONED, snapshot_authority)
    if raw_status and str(raw_status).upper() in {"CANCELLED", "CANCELED", "ABANDONED"}:
        return Decision(MatchWindowStatus.CANCELLED, CollectionPhase.NONE, None, CadenceProfile.NONE, finality, ReasonCode.CANCELLED, snapshot_authority)
    if status is None:
        return Decision(MatchWindowStatus.PLANNING_ERROR, CollectionPhase.NONE, None, CadenceProfile.NONE, finality, ReasonCode.UNKNOWN_LIFECYCLE, snapshot_authority)
    if not row["match_provider_id"] or row["match_id"] is None:
        return Decision(MatchWindowStatus.PLANNING_ERROR, CollectionPhase.NONE, None, CadenceProfile.NONE, finality, ReasonCode.MISSING_PROVIDER_IDENTITY, snapshot_authority)
    try:
        start = parse_metadata_timestamp(row["start_time_utc"])
    except MetadataTimestampError as exc:
        return Decision(MatchWindowStatus.PLANNING_ERROR, CollectionPhase.NONE, None, CadenceProfile.NONE, finality, ReasonCode.MISSING_START_TIME, snapshot_authority, exc.reason_code)
    window_start = start - settings.pre_match_window
    horizon_anchor = _horizon_anchor(row, start, settings)
    horizon = horizon_anchor + settings.post_match_horizon
    if status == "CONCLUDED" and finality is FinalityState.AUTHORITATIVE_COMPLETE:
        return Decision(MatchWindowStatus.COMPLETE, CollectionPhase.COMPLETE, None, CadenceProfile.NONE, finality, ReasonCode.AUTHORITATIVE_FINAL_CONFIRMED, snapshot_authority)
    if now > horizon and status == "CONCLUDED":
        return Decision(MatchWindowStatus.FAILED_TERMINAL, CollectionPhase.FINAL_CONFIRMATION, None, CadenceProfile.NONE, finality, ReasonCode.POLLING_HORIZON_EXCEEDED, snapshot_authority)
    if now < window_start:
        return Decision(MatchWindowStatus.PLANNED, CollectionPhase.NOT_STARTED, window_start, CadenceProfile.PRE_MATCH_PLACEHOLDER, finality, ReasonCode.FUTURE_OUTSIDE_WINDOW, snapshot_authority)
    if status == "SCHEDULED":
        due = now if now >= window_start else window_start
        return Decision(MatchWindowStatus.DUE if due <= now else MatchWindowStatus.PLANNED, CollectionPhase.PRE_MATCH, due, CadenceProfile.PRE_MATCH_PLACEHOLDER, finality, ReasonCode.APPROACHING_START, snapshot_authority)
    if status == "LIVE":
        return Decision(MatchWindowStatus.DUE, CollectionPhase.LIVE, now, CadenceProfile.LIVE_PLACEHOLDER, finality, ReasonCode.LIVE, snapshot_authority)
    if status in {"POSTGAME", "CONCLUDED"}:
        reason = ReasonCode.FINAL_STATS_UNAVAILABLE_OR_PARTIAL if status == "CONCLUDED" else ReasonCode.AWAITING_FINAL
        return Decision(MatchWindowStatus.AWAITING_FINAL, CollectionPhase.FINAL_CONFIRMATION, now, CadenceProfile.FINAL_PLACEHOLDER, finality, reason, snapshot_authority)
    return Decision(MatchWindowStatus.FAILED_TERMINAL, CollectionPhase.NONE, None, CadenceProfile.NONE, finality, ReasonCode.CONTRADICTORY_LIFECYCLE, snapshot_authority)


@dataclass
class ReconcileResult:
    correlation_id: str
    planned: int = 0
    updated: int = 0
    failed: int = 0
    degraded: bool = False
    failures: list[dict[str, Any]] | None = None


def reconcile(conn: sqlite3.Connection, *, now: datetime | None = None, settings: MatchWindowSettings | None = None, correlation_id: str = "startup_reconciliation", match_ids: set[int] | None = None) -> ReconcileResult:
    settings = settings or MatchWindowSettings.from_config()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result = ReconcileResult(correlation_id, failures=[])
    sql = "SELECT m.match_id, m.match_provider_id, m.season_id, r.competition_id AS competition_id, m.status, m.start_time_utc, NULL AS lifecycle_observed_at FROM matches m LEFT JOIN rounds r ON r.round_id=m.round_id"
    params: tuple[Any, ...] = ()
    if match_ids is not None:
        if not match_ids:
            return result
        placeholders = ",".join("?" for _ in match_ids)
        sql += f" WHERE m.match_id IN ({placeholders})"
        params = tuple(sorted(match_ids))
    rows = conn.execute(sql + " ORDER BY m.match_id", params).fetchall()
    for row in rows:
        try:
            finality, auth = _finality(conn, row["match_provider_id"])
            wid = window_id(row["match_id"], settings.policy_version)
            current = conn.execute("SELECT * FROM match_stat_windows WHERE window_id=?", (wid,)).fetchone()
            lifecycle = normalise_match_status(row["status"]) or str(row["status"] or "UNKNOWN")
            previous_lifecycle = current["lifecycle"] if current else None
            observed_at = (current["lifecycle_observed_at"] if current and current["lifecycle_observed_at"] and previous_lifecycle == lifecycle else _iso(now))
            eval_values = dict(row)
            eval_values["lifecycle_observed_at"] = observed_at
            decision = evaluate(_DictRow(eval_values), now, settings, finality, auth)
            valid_lease = bool(current and current["status"] == MatchWindowStatus.LEASED.value and _dt(current["lease_expires_at"]) and _dt(current["lease_expires_at"]) > now)
            expired_lease = bool(current and current["status"] == MatchWindowStatus.LEASED.value and _dt(current["lease_expires_at"]) and _dt(current["lease_expires_at"]) <= now)
            if valid_lease:
                conn.execute("""
                    UPDATE match_stat_windows SET
                      match_id=?, afl_match_id=?, match_provider_id=?, competition_id=?, season_id=?,
                      lifecycle=?, lifecycle_observed_at=?, last_observed_snapshot_authority=?, finality_state=?,
                      planner_version=?, updated_at=?
                    WHERE window_id=?
                """, (row["match_id"], row["match_id"], row["match_provider_id"], row["competition_id"], row["season_id"], lifecycle, observed_at, auth, finality.value, settings.planner_version, _iso(now), wid))
                result.updated += 1
                continue
            reason = ReasonCode.LEASE_EXPIRED_RECLAIMED.value if expired_lease else decision.reason.value
            conn.execute("""
                INSERT INTO match_stat_windows(window_id, match_id, afl_match_id, match_provider_id, competition_id, season_id, policy_version, lifecycle, lifecycle_observed_at, collection_phase, status, next_due_at, cadence_profile, last_observed_snapshot_authority, finality_state, reason_code, diagnostic_summary, planner_version, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(window_id) DO UPDATE SET
                  match_id=excluded.match_id, afl_match_id=excluded.afl_match_id, match_provider_id=excluded.match_provider_id,
                  competition_id=excluded.competition_id, season_id=excluded.season_id, lifecycle=excluded.lifecycle,
                  lifecycle_observed_at=excluded.lifecycle_observed_at, collection_phase=excluded.collection_phase,
                  status=excluded.status, next_due_at=excluded.next_due_at,
                  cadence_profile=excluded.cadence_profile, last_observed_snapshot_authority=excluded.last_observed_snapshot_authority,
                  finality_state=excluded.finality_state, reason_code=excluded.reason_code, diagnostic_summary=excluded.diagnostic_summary,
                  planner_version=excluded.planner_version,
                  lease_owner=CASE WHEN ? THEN NULL ELSE match_stat_windows.lease_owner END,
                  lease_token=CASE WHEN ? THEN NULL ELSE match_stat_windows.lease_token END,
                  lease_claimed_at=CASE WHEN ? THEN NULL ELSE match_stat_windows.lease_claimed_at END,
                  lease_expires_at=CASE WHEN ? THEN NULL ELSE match_stat_windows.lease_expires_at END,
                  updated_at=excluded.updated_at
            """, (wid, row["match_id"], row["match_id"], row["match_provider_id"], row["competition_id"], row["season_id"], settings.policy_version, lifecycle, observed_at, decision.phase.value, decision.status.value, _iso(decision.next_due), decision.cadence.value, auth, decision.finality.value, reason, decision.diagnostic, settings.planner_version, _iso(now), expired_lease, expired_lease, expired_lease, expired_lease))
            result.planned += 0 if current else 1; result.updated += 1 if current else 0
        except Exception as exc:
            result.failed += 1; result.degraded = True; result.failures.append({"match_id": row["match_id"], "error": type(exc).__name__})
    return result


class _DictRow(dict):
    def keys(self):
        return super().keys()

def claim_due_windows(owner: str, *, limit: int = 1, now: datetime | None = None, settings: MatchWindowSettings | None = None, lane=write_lane) -> list[dict[str, Any]]:
    settings = settings or MatchWindowSettings.from_config(); now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    def op(conn: sqlite3.Connection):
        rows = conn.execute("""SELECT * FROM match_stat_windows WHERE status IN ('due','awaiting_final','backoff','leased') AND next_due_at IS NOT NULL AND next_due_at <= ? AND (lease_expires_at IS NULL OR lease_expires_at <= ?) ORDER BY next_due_at, window_id LIMIT ?""", (_iso(now), _iso(now), limit)).fetchall()
        claimed=[]
        for row in rows:
            gen = int(row["lease_generation"] or 0) + 1; token = f"{owner}:{gen}:{int(now.timestamp())}"
            cur = conn.execute("UPDATE match_stat_windows SET status='leased', lease_owner=?, lease_token=?, lease_generation=?, lease_claimed_at=?, lease_expires_at=?, reason_code=CASE WHEN lease_expires_at IS NOT NULL AND lease_expires_at <= ? THEN ? ELSE reason_code END, updated_at=? WHERE window_id=? AND (lease_expires_at IS NULL OR lease_expires_at <= ?)", (owner, token, gen, _iso(now), _iso(now + settings.lease_duration), _iso(now), ReasonCode.LEASE_EXPIRED_RECLAIMED.value, _iso(now), row["window_id"], _iso(now)))
            if cur.rowcount == 1:
                item=dict(row); item.update({"lease_owner": owner, "lease_token": token, "lease_generation": gen, "attempt_id": attempt_id(row["window_id"], gen, row["attempt_count"]), "scheduler_job_id": scheduler_job_id(row["window_id"], gen, row["attempt_count"])})
                claimed.append(item)
        return claimed
    executor = getattr(lane, "execute_immediate", lane.execute)
    return executor("match_windows.claim", owner, op)


def _record_final_success(conn: sqlite3.Connection, window: str, token: str, *, now: datetime, rows_written: int) -> bool:
    row = conn.execute("SELECT match_provider_id FROM match_stat_windows WHERE window_id=? AND lease_token=?", (window, token)).fetchone()
    finality, authority = _finality(conn, row["match_provider_id"] if row else None)
    if row is None or finality is not FinalityState.AUTHORITATIVE_COMPLETE:
        return False
    cur = conn.execute("""
        UPDATE match_stat_windows SET
          status='complete', collection_phase='complete', finality_state='authoritative_complete',
          reason_code=?, attempt_count=attempt_count+1, consecutive_failure_count=0,
          last_attempted_at=?, last_successful_collection_at=?,
          last_successful_write_at=CASE WHEN ? > 0 THEN ? ELSE last_successful_write_at END,
          last_observed_snapshot_authority=?,
          lease_owner=NULL, lease_token=NULL, lease_claimed_at=NULL, lease_expires_at=NULL, updated_at=?
        WHERE window_id=? AND lease_token=?
    """, (ReasonCode.AUTHORITATIVE_FINAL_CONFIRMED.value, _iso(now), _iso(now),
          rows_written, _iso(now), authority, _iso(now), window, token))
    return cur.rowcount == 1


def complete_window(conn: sqlite3.Connection, window: str, token: str, *, now: datetime) -> bool:
    return _record_final_success(conn, window, token, now=now, rows_written=0)


def record_attempt_success(conn: sqlite3.Connection, window: str, token: str, *, now: datetime, rows_written: int, final: bool = False) -> bool:
    if final:
        return _record_final_success(conn, window, token, now=now, rows_written=rows_written)
    status = 'awaiting_final'; phase = 'final_confirmation'; reason = ReasonCode.ATTEMPT_SUCCEEDED_NON_FINAL.value
    cur=conn.execute("UPDATE match_stat_windows SET status=?, collection_phase=?, attempt_count=attempt_count+1, consecutive_failure_count=0, last_attempted_at=?, last_successful_collection_at=?, last_successful_write_at=CASE WHEN ? > 0 THEN ? ELSE last_successful_write_at END, reason_code=?, lease_owner=NULL, lease_token=NULL, lease_claimed_at=NULL, lease_expires_at=NULL, updated_at=? WHERE window_id=? AND lease_token=?", (status, phase, _iso(now), _iso(now), rows_written, _iso(now), reason, _iso(now), window, token))
    return cur.rowcount == 1


def record_attempt_failure(conn: sqlite3.Connection, window: str, token: str, *, now: datetime, backoff: timedelta = timedelta(minutes=10), reason: str | None = None) -> bool:
    cur=conn.execute("UPDATE match_stat_windows SET status='backoff', attempt_count=attempt_count+1, consecutive_failure_count=consecutive_failure_count+1, last_attempted_at=?, next_due_at=?, reason_code=?, diagnostic_summary=substr(?,1,500), lease_owner=NULL, lease_token=NULL, lease_claimed_at=NULL, lease_expires_at=NULL, updated_at=? WHERE window_id=? AND lease_token=?", (_iso(now), _iso(now+backoff), ReasonCode.ATTEMPT_FAILED_BACKOFF.value, reason, _iso(now), window, token))
    return cur.rowcount == 1


def release_window(conn: sqlite3.Connection, window: str, token: str, *, now: datetime) -> bool:
    cur=conn.execute("UPDATE match_stat_windows SET status='due', lease_owner=NULL, lease_token=NULL, lease_claimed_at=NULL, lease_expires_at=NULL, reason_code=?, updated_at=? WHERE window_id=? AND lease_token=?", (ReasonCode.RELEASED.value, _iso(now), window, token))
    return cur.rowcount == 1


def inspection_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT window_id, match_id, afl_match_id, match_provider_id, competition_id, season_id, lifecycle, collection_phase, status, next_due_at, cadence_profile, lease_owner, lease_claimed_at, lease_expires_at, last_attempted_at, last_successful_collection_at, last_successful_write_at, finality_state, attempt_count, consecutive_failure_count, reason_code FROM match_stat_windows ORDER BY next_due_at, match_id").fetchall()]
