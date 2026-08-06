"""Evidence-driven recovery of interrupted CFS polling attempts.

This is deliberately a control-plane reconciler: it never imports or invokes a
collector.  All mutation is performed in one SQLite immediate write lane.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import config
from afl_json.match_status import normalise_match_status
from afl_json.season_report import authoritative_stats_finality_for_match
from db.migration_runner import migrate_database
from db.scrape_runs import sanitize_error_summary
from scheduler.write_lane import write_lane


class RecoveryReason(str, Enum):
    LEASED_WORKER_NOT_STARTED = "leased_worker_not_started"
    REGISTRY_STARTED_BEFORE_SCRAPE_RUN = "registry_started_before_scrape_run"
    SCRAPE_STARTED_NO_COMPLETED_REQUEST = "scrape_started_no_completed_request"
    RESPONSE_RECEIVED_PERSISTENCE_UNPROVEN = "response_received_persistence_unproven"
    PERSISTENCE_ROLLED_BACK = "persistence_rolled_back"
    INTERRUPTED_AFTER_PERSISTENCE_COMMIT = "interrupted_after_persistence_commit"
    INTERRUPTED_DURING_AUDIT_FINALISATION = "interrupted_during_audit_finalisation"
    INTERRUPTED_DURING_REGISTRY_FINALISATION = (
        "interrupted_during_registry_finalisation"
    )
    INTERRUPTED_DURING_WINDOW_UPDATE = "interrupted_during_window_update"
    FINAL_AUTHORITATIVE_DATA_COMMITTED = "final_authoritative_data_already_committed"
    LATER_ATTEMPT_SUPERSEDED = "later_attempt_superseded_stale_attempt"
    GRACEFUL_SHUTDOWN = "graceful_shutdown_interruption"
    UNCLEAN_PROCESS = "unclean_process_interruption"
    STALE_LEASE_RECLAIMED = "stale_lease_reclaimed"
    RETRY_REPLANNED = "retry_replanned"
    OUTCOME_UNRESOLVED = "outcome_unresolved"


@dataclass(frozen=True)
class RecoverySettings:
    maximum_attempt_duration: timedelta = timedelta(minutes=30)
    registry_running_staleness: timedelta = timedelta(minutes=30)
    scrape_run_running_staleness: timedelta = timedelta(minutes=30)
    shutdown_grace_period: timedelta = timedelta(minutes=2)

    @classmethod
    def from_config(cls) -> "RecoverySettings":
        value = cls(
            timedelta(seconds=config.AFL_RECOVERY_MAX_ATTEMPT_SECONDS),
            timedelta(seconds=config.AFL_RECOVERY_REGISTRY_STALE_SECONDS),
            timedelta(seconds=config.AFL_RECOVERY_SCRAPE_RUN_STALE_SECONDS),
            timedelta(seconds=config.AFL_RECOVERY_SHUTDOWN_GRACE_SECONDS),
        )
        if min(
            value.maximum_attempt_duration,
            value.registry_running_staleness,
            value.scrape_run_running_staleness,
            value.shutdown_grace_period,
        ) < timedelta(0):
            raise ValueError("recovery thresholds must be non-negative")
        return value


@dataclass(frozen=True)
class RecoveryScope:
    canonical_match_id: int | None = None
    window_id: str | None = None
    attempt_id: str | None = None
    started_since: datetime | None = None


@dataclass
class ReconciliationReport:
    reconciliation_run_id: str
    trigger_source: str
    dry_run: bool
    started_at: str
    thresholds: dict[str, Any]
    scope: dict[str, Any]
    inspected_windows: int = 0
    inspected_attempts: int = 0
    stale_leases_found: int = 0
    stale_leases_expired: int = 0
    registry_rows_repaired: int = 0
    scrape_runs_repaired: int = 0
    windows_completed_from_existing_data: int = 0
    windows_replanned: int = 0
    attempts_superseded_by_later_success: int = 0
    unresolved_cases: int = 0
    per_item_failures: list[dict[str, str]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    result = datetime.fromisoformat(value)
    return (
        result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    ).astimezone(timezone.utc)


def _scope_sql(scope: RecoveryScope, alias: str = "w") -> tuple[str, list[Any]]:
    terms, values = [], []
    if scope.canonical_match_id is not None:
        terms.append(f"{alias}.match_id=?")
        values.append(scope.canonical_match_id)
    if scope.window_id:
        terms.append(f"{alias}.window_id=?")
        values.append(scope.window_id)
    if scope.attempt_id:
        terms.append(
            "(r.attempt_id=? OR s.attempt_id=? OR r.job_id=? OR s.correlation_id=?)"
        )
        values.extend([scope.attempt_id] * 4)
    if scope.started_since:
        terms.append(
            "COALESCE(r.last_attempt_time,s.started_at,w.lease_claimed_at,w.updated_at)>=?"
        )
        values.append(_iso(scope.started_since))
    return (" AND " + " AND ".join(terms) if terms else "", values)


def _reason(
    has_registry: bool,
    has_scrape: bool,
    scrape_running: bool,
    response_received: bool,
    committed: bool,
    final: bool,
    superseded: str | None,
    graceful: bool,
) -> RecoveryReason:
    if superseded:
        return RecoveryReason.LATER_ATTEMPT_SUPERSEDED
    if final:
        return RecoveryReason.FINAL_AUTHORITATIVE_DATA_COMMITTED
    if committed:
        if scrape_running:
            return RecoveryReason.INTERRUPTED_DURING_AUDIT_FINALISATION
        if has_registry:
            return RecoveryReason.INTERRUPTED_DURING_REGISTRY_FINALISATION
        return RecoveryReason.INTERRUPTED_DURING_WINDOW_UPDATE
    if has_registry and not has_scrape:
        return RecoveryReason.REGISTRY_STARTED_BEFORE_SCRAPE_RUN
    if has_scrape and response_received:
        return RecoveryReason.RESPONSE_RECEIVED_PERSISTENCE_UNPROVEN
    if has_scrape:
        return RecoveryReason.SCRAPE_STARTED_NO_COMPLETED_REQUEST
    if graceful:
        return RecoveryReason.GRACEFUL_SHUTDOWN
    return RecoveryReason.LEASED_WORKER_NOT_STARTED


def _integrity(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA quick_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError("database integrity check failed")
    required = {
        "match_stat_windows",
        "scheduler_job_registry",
        "scrape_runs",
        "cfs_player_stats",
    }
    actual = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if required - actual:
        raise RuntimeError("recovery migration readiness check failed")


def _reconcile(
    conn: sqlite3.Connection,
    report: ReconciliationReport,
    *,
    now: datetime,
    settings: RecoverySettings,
    scope: RecoveryScope,
) -> None:
    _integrity(conn)
    suffix, values = _scope_sql(scope)
    rows = conn.execute(
        """SELECT w.*, r.job_id registry_job_id,r.status registry_status,
        r.last_attempt_time registry_started,r.attempt_id registry_attempt_id,
        r.scrape_run_id registry_scrape_run_id,r.scheduler_instance_id registry_instance,
        s.run_id scrape_id,s.status scrape_status,s.started_at scrape_started,
        s.attempt_id scrape_attempt_id,s.response_received_at,s.persistence_committed_at,
        s.rows_read,s.rows_written,s.scheduler_instance_id scrape_instance,
        m.status current_match_lifecycle
        FROM match_stat_windows w
        JOIN matches m ON m.match_id=w.match_id
        LEFT JOIN scheduler_job_registry r ON r.window_id=w.window_id AND r.status='running'
        LEFT JOIN scrape_runs s ON s.window_id=w.window_id AND
          ((r.attempt_id IS NOT NULL AND s.attempt_id=r.attempt_id) OR
           (r.attempt_id IS NULL AND s.status='running'))
        WHERE (w.status='leased' OR r.job_id IS NOT NULL OR s.run_id IS NOT NULL)"""
        + suffix
        + " ORDER BY w.window_id",
        values,
    ).fetchall()
    seen_windows: set[str] = set()
    for combined in rows:
        try:
            row = dict(combined)
            wid = row["window_id"]
            seen_windows.add(wid)
            lease_expiry = _dt(row["lease_expires_at"])
            valid_lease = (
                row["status"] == "leased"
                and lease_expiry is not None
                and lease_expiry > now
            )
            registry_age = (
                max(
                    settings.maximum_attempt_duration,
                    settings.registry_running_staleness,
                )
                + settings.shutdown_grace_period
            )
            scrape_age = (
                max(
                    settings.maximum_attempt_duration,
                    settings.scrape_run_running_staleness,
                )
                + settings.shutdown_grace_period
            )
            registry_stale = bool(
                row["registry_job_id"]
                and _dt(row["registry_started"])
                and _dt(row["registry_started"]) <= now - registry_age
            )
            scrape_stale = bool(
                row["scrape_id"]
                and row["scrape_status"] == "running"
                and _dt(row["scrape_started"])
                and _dt(row["scrape_started"]) <= now - scrape_age
            )
            expired = row["status"] == "leased" and (
                lease_expiry is None or lease_expiry <= now
            )
            if valid_lease or not (expired or registry_stale or scrape_stale):
                continue
            report.inspected_attempts += int(
                bool(row["registry_job_id"] or row["scrape_id"])
            )
            if expired:
                report.stale_leases_found += 1
            attempt = (
                row["registry_attempt_id"]
                or row["scrape_attempt_id"]
                or row["last_attempt_id"]
            )
            later = conn.execute(
                """SELECT attempt_id FROM scheduler_job_registry
                WHERE window_id=? AND status='succeeded' AND attempt_id IS NOT NULL
                AND (? IS NULL OR attempt_id<>?) ORDER BY last_success_time DESC LIMIT 1""",
                (wid, attempt, attempt),
            ).fetchone()
            superseded = later[0] if later else None
            finality = authoritative_stats_finality_for_match(
                conn, row["match_provider_id"]
            )
            concluded = (
                normalise_match_status(row["current_match_lifecycle"]) == "CONCLUDED"
            )
            final = concluded and finality.has_satisfactory_concluded_coverage
            committed = bool(row["persistence_committed_at"])
            instance = row["registry_instance"] or row["scrape_instance"]
            runtime = (
                conn.execute(
                    "SELECT shutdown_kind FROM scheduler_runtime_instances WHERE instance_id=?",
                    (instance,),
                ).fetchone()
                if instance
                else None
            )
            graceful = bool(runtime and runtime[0] == "graceful")
            reason = _reason(
                bool(row["registry_job_id"]),
                bool(row["scrape_id"]),
                row["scrape_status"] == "running",
                bool(row["response_received_at"]),
                committed,
                final,
                superseded,
                graceful,
            )
            evidence = "committed" if committed else "unknown"
            replannable = (
                row["status"]
                not in {"complete", "cancelled", "not_applicable", "failed_terminal"}
                and not final
            )
            decision = {
                "window_id": wid,
                "attempt_id": attempt,
                "reason": reason.value,
                "persistence_evidence": evidence,
                "superseded_by_attempt_id": superseded,
                "action": "complete"
                if final
                else "replan"
                if replannable
                else "terminal_preserved",
            }
            report.decisions.append(decision)
            if superseded:
                report.attempts_superseded_by_later_success += 1
            if not committed and not superseded:
                report.unresolved_cases += 1
            if report.dry_run:
                if expired:
                    report.stale_leases_expired += 1
                if row["registry_job_id"]:
                    report.registry_rows_repaired += 1
                if row["scrape_id"] and row["scrape_status"] == "running":
                    report.scrape_runs_repaired += 1
                if final:
                    report.windows_completed_from_existing_data += 1
                elif replannable:
                    report.windows_replanned += 1
                continue
            finished = _iso(now)
            if row["registry_job_id"]:
                changed = conn.execute(
                    """UPDATE scheduler_job_registry SET status='interrupted',
                    recovery_at=?,recovery_run_id=?,recovery_reason=?,persistence_evidence=?,
                    superseded_by_attempt_id=?,updated_at=? WHERE job_id=? AND status='running'""",
                    (
                        finished,
                        report.reconciliation_run_id,
                        reason.value,
                        evidence,
                        superseded,
                        finished,
                        row["registry_job_id"],
                    ),
                ).rowcount
                report.registry_rows_repaired += changed
            if row["scrape_id"] and row["scrape_status"] == "running":
                started = _dt(row["scrape_started"])
                duration = (
                    max(0, int((now - started).total_seconds() * 1000))
                    if started
                    else None
                )
                changed = conn.execute(
                    """UPDATE scrape_runs SET status='interrupted',finished_at=?,duration_ms=?,
                    error_class='InterruptedAttempt',error_summary=?,reason_code=?,recovery_at=?,
                    recovery_run_id=?,recovery_reason=?,persistence_evidence=?,superseded_by_attempt_id=?
                    WHERE run_id=? AND status='running'""",
                    (
                        finished,
                        duration,
                        reason.value,
                        reason.value,
                        finished,
                        report.reconciliation_run_id,
                        reason.value,
                        evidence,
                        superseded,
                        row["scrape_id"],
                    ),
                ).rowcount
                report.scrape_runs_repaired += changed
            if final:
                changed = conn.execute(
                    """UPDATE match_stat_windows SET status='complete',collection_phase='complete',
                    next_due_at=NULL,finality_state='authoritative_complete',last_observed_snapshot_authority=2,
                    lease_owner=NULL,lease_token=NULL,lease_claimed_at=NULL,lease_expires_at=NULL,
                    reason_code=?,recovery_at=?,recovery_run_id=?,recovery_reason=?,recovered_attempt_id=?,
                    superseded_attempt_id=?,updated_at=? WHERE window_id=? AND status NOT IN
                    ('complete','cancelled','not_applicable') AND (lease_token IS ? OR lease_token=?)""",
                    (
                        RecoveryReason.FINAL_AUTHORITATIVE_DATA_COMMITTED.value,
                        finished,
                        report.reconciliation_run_id,
                        reason.value,
                        attempt,
                        superseded,
                        finished,
                        wid,
                        row["lease_token"],
                        row["lease_token"],
                    ),
                ).rowcount
                report.windows_completed_from_existing_data += changed
            elif replannable:
                changed = conn.execute(
                    """UPDATE match_stat_windows SET status='backoff',next_due_at=?,
                    lease_owner=NULL,lease_token=NULL,lease_claimed_at=NULL,lease_expires_at=NULL,
                    consecutive_failure_count=consecutive_failure_count+1,reason_code=?,recovery_at=?,
                    recovery_run_id=?,recovery_reason=?,recovered_attempt_id=?,superseded_attempt_id=?,updated_at=?
                    WHERE window_id=? AND status NOT IN ('complete','cancelled','not_applicable','failed_terminal')
                    AND (lease_token IS ? OR lease_token=?)""",
                    (
                        _iso(now + settings.shutdown_grace_period),
                        RecoveryReason.RETRY_REPLANNED.value,
                        finished,
                        report.reconciliation_run_id,
                        reason.value,
                        attempt,
                        superseded,
                        finished,
                        wid,
                        row["lease_token"],
                        row["lease_token"],
                    ),
                ).rowcount
                report.windows_replanned += changed
            if expired:
                report.stale_leases_expired += 1
        except Exception as exc:
            report.per_item_failures.append(
                {
                    "window_id": str(dict(combined).get("window_id", "unknown")),
                    "error": sanitize_error_summary(exc),
                }
            )
    report.inspected_windows = len(seen_windows)


def reconcile_interrupted_attempts(
    *,
    trigger_source: str = "startup",
    dry_run: bool = False,
    scope: RecoveryScope | None = None,
    settings: RecoverySettings | None = None,
    now: datetime | None = None,
    lane=write_lane,
    run_id: str | None = None,
) -> ReconciliationReport:
    if trigger_source not in {"startup", "manual", "test"}:
        raise ValueError("trigger_source must be startup, manual, or test")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    settings = settings or RecoverySettings.from_config()
    scope = scope or RecoveryScope()
    started = time.monotonic()
    report = ReconciliationReport(
        run_id or f"recovery_{uuid.uuid4().hex}",
        trigger_source,
        dry_run,
        _iso(now),
        {
            "maximum_attempt_seconds": int(
                settings.maximum_attempt_duration.total_seconds()
            ),
            "registry_stale_seconds": int(
                settings.registry_running_staleness.total_seconds()
            ),
            "scrape_run_stale_seconds": int(
                settings.scrape_run_running_staleness.total_seconds()
            ),
            "shutdown_grace_seconds": int(
                settings.shutdown_grace_period.total_seconds()
            ),
        },
        {
            "canonical_match_id": scope.canonical_match_id,
            "window_id": scope.window_id,
            "attempt_id": scope.attempt_id,
            "started_since": _iso(scope.started_since) if scope.started_since else None,
        },
    )
    executor = getattr(lane, "execute_immediate", lane.execute)
    executor(
        "interrupted_attempts.reconcile",
        report.reconciliation_run_id,
        lambda conn: _reconcile(conn, report, now=now, settings=settings, scope=scope),
    )
    report.duration_ms = max(0, int((time.monotonic() - started) * 1000))
    report.finished_at = _iso(now)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile interrupted CFS polling attempts (never runs collectors)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report without mutation"
    )
    parser.add_argument("--match-id", type=int)
    parser.add_argument("--window-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--since", help="only attempts at/after an ISO-8601 instant")
    args = parser.parse_args(argv)
    migrate_database()
    scope = RecoveryScope(
        args.match_id,
        args.window_id,
        args.attempt_id,
        _dt(args.since) if args.since else None,
    )
    report = reconcile_interrupted_attempts(
        trigger_source="manual", dry_run=args.dry_run, scope=scope
    )
    print(json.dumps(report.to_dict(), sort_keys=True, indent=2))
    return 0 if not report.per_item_failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
