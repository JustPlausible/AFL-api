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
from scheduler.match_windows import (
    MatchWindowSettings,
    reconcile as reconcile_match_windows,
)
from scheduler.runtime import RuntimeOwnership, runtime_ownership
from scheduler.write_lane import write_lane


class RecoveryReason(str, Enum):
    LEASED_WORKER_NOT_STARTED = "leased_worker_not_started"
    REGISTRY_STARTED_BEFORE_SCRAPE_RUN = "registry_started_before_scrape_run"
    SCRAPE_STARTED_NO_COMPLETED_REQUEST = "scrape_started_no_completed_request"
    RESPONSE_RECEIVED_PERSISTENCE_UNPROVEN = "response_received_persistence_unproven"
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
    heartbeat_interval: timedelta = timedelta(seconds=15)
    startup_candidate_limit: int = 500

    @classmethod
    def from_config(cls) -> "RecoverySettings":
        value = cls(
            timedelta(seconds=config.AFL_RECOVERY_MAX_ATTEMPT_SECONDS),
            timedelta(seconds=config.AFL_RECOVERY_REGISTRY_STALE_SECONDS),
            timedelta(seconds=config.AFL_RECOVERY_SCRAPE_RUN_STALE_SECONDS),
            timedelta(seconds=config.AFL_RECOVERY_SHUTDOWN_GRACE_SECONDS),
            timedelta(seconds=config.AFL_SCHEDULER_HEARTBEAT_SECONDS),
            config.AFL_RECOVERY_STARTUP_CANDIDATE_LIMIT,
        )
        value.validate()
        return value

    def validate(self, *, lease_duration: timedelta | None = None) -> None:
        if min(
            self.maximum_attempt_duration,
            self.registry_running_staleness,
            self.scrape_run_running_staleness,
            self.shutdown_grace_period,
            self.heartbeat_interval,
        ) < timedelta(seconds=15):
            raise ValueError("recovery thresholds must be at least 15 seconds")
        if self.maximum_attempt_duration < timedelta(minutes=1):
            raise ValueError("maximum attempt duration must be at least 60 seconds")
        if self.registry_running_staleness < self.maximum_attempt_duration:
            raise ValueError(
                "registry staleness must not precede maximum attempt duration"
            )
        if self.scrape_run_running_staleness < self.maximum_attempt_duration:
            raise ValueError(
                "scrape-run staleness must not precede maximum attempt duration"
            )
        if self.shutdown_grace_period < self.heartbeat_interval * 2:
            raise ValueError(
                "shutdown grace must cover at least two heartbeat intervals"
            )
        if lease_duration and self.maximum_attempt_duration < lease_duration:
            raise ValueError(
                "maximum attempt duration must not be shorter than the lease"
            )
        if not 1 <= self.startup_candidate_limit <= 10_000:
            raise ValueError("startup recovery candidate limit must be between 1 and 10000")


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
    compatibility_records: int = 0
    startup_candidates_truncated: bool = False
    would_expire_leases: int = 0
    would_repair_registry_rows: int = 0
    would_repair_scrape_runs: int = 0
    would_replan_windows: int = 0
    would_complete_windows: int = 0
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


def _runtime_state(
    conn: sqlite3.Connection,
    instance_id: str | None,
    *,
    now: datetime,
    settings: RecoverySettings,
) -> RuntimeOwnership:
    if not instance_id:
        return RuntimeOwnership.UNKNOWN
    row = conn.execute(
        "SELECT * FROM scheduler_runtime_instances WHERE instance_id=?", (instance_id,)
    ).fetchone()
    return runtime_ownership(
        row, now=now, heartbeat_timeout=settings.shutdown_grace_period
    )


def _match_evidence(conn: sqlite3.Connection, window: dict[str, Any]) -> str:
    finality = authoritative_stats_finality_for_match(
        conn, window.get("match_provider_id")
    )
    lifecycle = normalise_match_status(window.get("current_match_lifecycle"))
    if lifecycle == "CONCLUDED" and finality.has_satisfactory_concluded_coverage:
        return "authoritative_final"
    if finality.has_authoritative_snapshot:
        return "partial_live"
    return "absent"


def _later_success(
    conn: sqlite3.Connection, *, window_id: str, attempt: dict[str, Any]
) -> str | None:
    """Return only a provably later success in the same polling window."""
    generation = attempt.get("lease_generation")
    started = _dt(attempt.get("started_at"))
    rows = conn.execute(
        """SELECT attempt_id,lease_generation,last_success_time,created_at,job_id
        FROM scheduler_job_registry WHERE window_id=? AND status='succeeded'
        AND attempt_id IS NOT NULL ORDER BY lease_generation DESC,
        last_success_time DESC,created_at DESC,job_id DESC""",
        (window_id,),
    ).fetchall()
    eligible = []
    for row in rows:
        if row["attempt_id"] == attempt.get("attempt_id"):
            continue
        later_generation = (
            generation is not None
            and row["lease_generation"] is not None
            and int(row["lease_generation"]) > int(generation)
        )
        success_time = _dt(row["last_success_time"] or row["created_at"])
        later_time = (
            started is not None and success_time is not None and success_time > started
        )
        if later_generation or later_time:
            eligible.append(row)
    return eligible[0]["attempt_id"] if eligible else None


def _reason(
    *,
    has_registry: bool,
    has_scrape: bool,
    response_received: bool,
    attempt_evidence: str,
    match_evidence: str,
    superseded: str | None,
    runtime_state: RuntimeOwnership,
) -> RecoveryReason:
    if superseded:
        return RecoveryReason.LATER_ATTEMPT_SUPERSEDED
    if match_evidence == "authoritative_final":
        return RecoveryReason.FINAL_AUTHORITATIVE_DATA_COMMITTED
    if attempt_evidence == "committed":
        return RecoveryReason.INTERRUPTED_AFTER_PERSISTENCE_COMMIT
    if runtime_state is RuntimeOwnership.GRACEFULLY_STOPPED:
        return RecoveryReason.GRACEFUL_SHUTDOWN
    if runtime_state is RuntimeOwnership.STALE_UNCLEAN:
        return RecoveryReason.UNCLEAN_PROCESS
    if has_registry and not has_scrape:
        return RecoveryReason.REGISTRY_STARTED_BEFORE_SCRAPE_RUN
    if has_scrape and response_received:
        return RecoveryReason.RESPONSE_RECEIVED_PERSISTENCE_UNPROVEN
    if has_scrape:
        return RecoveryReason.SCRAPE_STARTED_NO_COMPLETED_REQUEST
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


def _record_compatibility(
    report: ReconciliationReport, *, kind: str, identity: str, reason: str
) -> None:
    report.compatibility_records += 1
    report.unresolved_cases += 1
    report.decisions.append(
        {
            "record_type": kind,
            "identity": identity,
            "reason": reason,
            "attempt_persistence_evidence": "unknown",
            "match_authoritative_evidence": "absent",
            "action": "report_only",
        }
    )


def _reconcile(
    conn: sqlite3.Connection,
    report: ReconciliationReport,
    *,
    now: datetime,
    settings: RecoverySettings,
    scope: RecoveryScope,
    window_settings: MatchWindowSettings,
    startup_candidate_limit: int | None,
) -> None:
    """Classify attempts once, repair histories, then plan each affected window once."""
    _integrity(conn)
    window_sql = """SELECT w.*,m.status current_match_lifecycle FROM match_stat_windows w
                    LEFT JOIN matches m ON m.match_id=w.match_id WHERE 1=1"""
    params: list[Any] = []
    if scope.canonical_match_id is not None:
        window_sql += " AND w.match_id=?"
        params.append(scope.canonical_match_id)
    if scope.window_id:
        window_sql += " AND w.window_id=?"
        params.append(scope.window_id)
    if startup_candidate_limit is not None:
        window_sql += """ AND (w.status='leased'
            OR EXISTS (SELECT 1 FROM scheduler_job_registry cr
                       WHERE cr.window_id=w.window_id AND cr.status='running')
            OR EXISTS (SELECT 1 FROM scrape_runs cs
                       WHERE cs.window_id=w.window_id AND cs.status='running'))
            ORDER BY w.updated_at,w.window_id LIMIT ?"""
        params.append(startup_candidate_limit + 1)
    window_rows = conn.execute(window_sql, params).fetchall()
    if startup_candidate_limit is not None:
        report.startup_candidates_truncated = len(window_rows) > startup_candidate_limit
        window_rows = window_rows[:startup_candidate_limit]
    windows = {row["window_id"]: dict(row) for row in window_rows}

    # Attempt-scoped operation discovers its window without broadening mutation scope.
    if scope.attempt_id:
        ids = conn.execute(
            """SELECT window_id FROM scheduler_job_registry WHERE attempt_id=? OR job_id=?
            UNION SELECT window_id FROM scrape_runs WHERE attempt_id=? OR correlation_id=?""",
            (scope.attempt_id, scope.attempt_id, scope.attempt_id, scope.attempt_id),
        ).fetchall()
        allowed = {row[0] for row in ids if row[0]}
        windows = {key: value for key, value in windows.items() if key in allowed}

    report.inspected_windows = len(windows)
    if startup_candidate_limit is None:
        registry = conn.execute(
            "SELECT * FROM scheduler_job_registry WHERE status='running' "
            "ORDER BY COALESCE(last_attempt_time,created_at),job_id"
        ).fetchall()
        scrapes = conn.execute(
            "SELECT * FROM scrape_runs WHERE status='running' ORDER BY started_at,run_id"
        ).fetchall()
    else:
        window_ids = tuple(windows)
        placeholders = ",".join("?" for _ in window_ids)
        if window_ids:
            registry = conn.execute(
                f"""SELECT * FROM scheduler_job_registry WHERE status='running'
                AND attempt_id IS NOT NULL AND window_id IN ({placeholders})
                ORDER BY COALESCE(last_attempt_time,created_at),job_id""",
                window_ids,
            ).fetchall()
            scrapes = conn.execute(
                f"""SELECT * FROM scrape_runs WHERE status='running'
                AND attempt_id IS NOT NULL AND window_id IN ({placeholders})
                ORDER BY started_at,run_id""",
                window_ids,
            ).fetchall()
        else:
            registry, scrapes = [], []
        # Compatibility evidence is bounded independently. Rows correlated to
        # valid, unselected windows are deferred, not mislabeled as orphans.
        registry += conn.execute(
            """SELECT r.* FROM scheduler_job_registry r WHERE r.status='running'
            AND (r.window_id IS NULL OR r.attempt_id IS NULL OR NOT EXISTS
                 (SELECT 1 FROM match_stat_windows w WHERE w.window_id=r.window_id))
            ORDER BY COALESCE(r.last_attempt_time,r.created_at),r.job_id LIMIT ?""",
            (startup_candidate_limit,),
        ).fetchall()
        scrapes += conn.execute(
            """SELECT s.* FROM scrape_runs s WHERE s.status='running'
            AND (s.window_id IS NULL OR s.attempt_id IS NULL OR NOT EXISTS
                 (SELECT 1 FROM match_stat_windows w WHERE w.window_id=s.window_id))
            ORDER BY s.started_at,s.run_id LIMIT ?""",
            (startup_candidate_limit,),
        ).fetchall()
    groups: dict[tuple[str, str], dict[str, Any]] = {}

    def in_time_scope(value: str | None) -> bool:
        return not scope.started_since or (
            _dt(value) is not None and _dt(value) >= scope.started_since
        )

    for raw in registry:
        row = dict(raw)
        if scope.attempt_id and scope.attempt_id not in {
            row.get("attempt_id"),
            row.get("job_id"),
        }:
            continue
        if not in_time_scope(row.get("last_attempt_time") or row.get("created_at")):
            continue
        if (
            not row.get("window_id")
            or row["window_id"] not in windows
            or not row.get("attempt_id")
        ):
            if scope.window_id and row.get("window_id") != scope.window_id:
                continue
            # Legacy one-shots and orphan rows are never replayed or guessed.
            if (
                scope.canonical_match_id is None
                or row.get("match_id") == scope.canonical_match_id
            ):
                _record_compatibility(
                    report,
                    kind="registry",
                    identity=row["job_id"],
                    reason="uncorrelated_compatibility_record",
                )
            continue
        group = groups.setdefault(
            (row["window_id"], row["attempt_id"]), {"registry": [], "scrapes": []}
        )
        group["registry"].append(row)

    for raw in scrapes:
        row = dict(raw)
        if scope.attempt_id and scope.attempt_id not in {
            row.get("attempt_id"),
            row.get("correlation_id"),
        }:
            continue
        if not in_time_scope(row.get("started_at")):
            continue
        if (
            not row.get("window_id")
            or row["window_id"] not in windows
            or not row.get("attempt_id")
        ):
            if scope.window_id and row.get("window_id") != scope.window_id:
                continue
            _record_compatibility(
                report,
                kind="scrape_run",
                identity=row["run_id"],
                reason="uncorrelated_compatibility_record",
            )
            continue
        group = groups.setdefault(
            (row["window_id"], row["attempt_id"]), {"registry": [], "scrapes": []}
        )
        group["scrapes"].append(row)

    # A lease without an audit/control row is still a single synthetic attempt.
    for window in windows.values():
        if window["status"] == "leased" and not any(
            key[0] == window["window_id"] for key in groups
        ):
            attempt_id = (
                window.get("last_attempt_id") or f"lease:{window['lease_generation']}"
            )
            groups[(window["window_id"], attempt_id)] = {
                "registry": [],
                "scrapes": [],
                "lease_only": True,
            }

    affected: dict[str, dict[str, Any]] = {}
    for (window_id, attempt_id), group in sorted(groups.items()):
        window = windows[window_id]
        registries, audits = group["registry"], group["scrapes"]
        started_values = [
            r.get("last_attempt_time") or r.get("created_at") for r in registries
        ]
        started_values += [s.get("started_at") for s in audits]
        started_at = min(
            (value for value in started_values if value),
            default=window.get("lease_claimed_at"),
        )
        generation_values = [r.get("lease_generation") for r in registries] + [
            s.get("lease_generation") for s in audits
        ]
        generation = max(
            (int(v) for v in generation_values if v is not None),
            default=int(window.get("lease_generation") or 0),
        )
        instance = next(
            (
                r.get("scheduler_instance_id")
                for r in registries
                if r.get("scheduler_instance_id")
            ),
            None,
        )
        instance = instance or next(
            (
                s.get("scheduler_instance_id")
                for s in audits
                if s.get("scheduler_instance_id")
            ),
            None,
        )
        runtime_state = _runtime_state(conn, instance, now=now, settings=settings)
        lease_expiry = _dt(window.get("lease_expires_at"))
        lease_expired = window["status"] == "leased" and (
            lease_expiry is None or lease_expiry <= now
        )
        token_values = {r.get("lease_token") for r in registries} | {
            s.get("lease_token") for s in audits
        }
        owns_lease = bool(
            window.get("lease_token") and window.get("lease_token") in token_values
        )
        owner_matches = bool(
            instance and str(window.get("lease_owner") or "").startswith(instance)
        )
        if window["status"] == "leased" and not lease_expired:
            report.decisions.append(
                {
                    "window_id": window_id,
                    "attempt_id": attempt_id,
                    "runtime_ownership": runtime_state.value,
                    "action": "valid_lease_preserved",
                }
            )
            affected.setdefault(window_id, {})["active"] = True
            continue
        attempt_started = _dt(started_at)
        within_maximum = bool(
            attempt_started
            and attempt_started >= now - settings.maximum_attempt_duration
        )
        if (
            runtime_state is RuntimeOwnership.ACTIVE
            and (owns_lease or owner_matches)
            and within_maximum
        ):
            report.decisions.append(
                {
                    "window_id": window_id,
                    "attempt_id": attempt_id,
                    "runtime_ownership": runtime_state.value,
                    "action": "active_preserved",
                }
            )
            affected.setdefault(window_id, {})["active"] = True
            continue
        registry_stale = any(
            _dt(r.get("last_attempt_time") or r.get("created_at"))
            <= now - settings.registry_running_staleness
            for r in registries
        )
        scrape_stale = any(
            _dt(s.get("started_at")) <= now - settings.scrape_run_running_staleness
            for s in audits
        )
        stopped_recoverable = runtime_state in {
            RuntimeOwnership.GRACEFULLY_STOPPED,
            RuntimeOwnership.STALE_UNCLEAN,
        }
        if not (lease_expired or registry_stale or scrape_stale or stopped_recoverable):
            continue

        report.inspected_attempts += 1
        attempt_evidence = (
            "committed"
            if any(
                s.get("persistence_committed_at")
                or s.get("attempt_persistence_evidence") == "committed"
                for s in audits
            )
            else "unknown"
        )
        match_evidence = _match_evidence(conn, window)
        attempt = {
            "attempt_id": attempt_id,
            "lease_generation": generation,
            "started_at": started_at,
        }
        superseded = _later_success(conn, window_id=window_id, attempt=attempt)
        reason = _reason(
            has_registry=bool(registries),
            has_scrape=bool(audits),
            response_received=any(s.get("response_received_at") for s in audits),
            attempt_evidence=attempt_evidence,
            match_evidence=match_evidence,
            superseded=superseded,
            runtime_state=runtime_state,
        )
        decision = {
            "window_id": window_id,
            "attempt_id": attempt_id,
            "reason": reason.value,
            "attempt_persistence_evidence": attempt_evidence,
            "match_authoritative_evidence": match_evidence,
            "superseded_by_attempt_id": superseded,
            "runtime_ownership": runtime_state.value,
            "action": "repair_attempt",
        }
        report.decisions.append(decision)
        report.would_repair_registry_rows += len(registries)
        report.would_repair_scrape_runs += len(audits)
        if superseded:
            report.attempts_superseded_by_later_success += 1
        if attempt_evidence == "unknown":
            report.unresolved_cases += 1
        affected.setdefault(window_id, {}).update(
            {
                "attempt_id": attempt_id,
                "reason": reason.value,
                "superseded": superseded,
                "match_evidence": match_evidence,
                "lease_expired": lease_expired,
            }
        )
        if report.dry_run:
            continue
        savepoint = (
            "attempt_" + uuid.uuid5(uuid.NAMESPACE_OID, window_id + attempt_id).hex
        )
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            finished = _iso(now)
            repaired_registry = 0
            repaired_scrapes = 0
            for row in registries:
                repaired_registry += conn.execute(
                    """UPDATE scheduler_job_registry
                    SET status='interrupted',recovery_at=?,recovery_run_id=?,recovery_reason=?,
                    attempt_persistence_evidence=?,match_authoritative_evidence=?,
                    superseded_by_attempt_id=?,updated_at=? WHERE job_id=? AND status='running'""",
                    (
                        finished,
                        report.reconciliation_run_id,
                        reason.value,
                        attempt_evidence,
                        match_evidence,
                        superseded,
                        finished,
                        row["job_id"],
                    ),
                ).rowcount
            for row in audits:
                began = _dt(row.get("started_at"))
                duration = (
                    max(0, int((now - began).total_seconds() * 1000)) if began else None
                )
                repaired_scrapes += conn.execute(
                    """UPDATE scrape_runs SET status='interrupted',
                    finished_at=?,duration_ms=?,error_class='InterruptedAttempt',error_summary=?,
                    reason_code=?,recovery_at=?,recovery_run_id=?,recovery_reason=?,
                    attempt_persistence_evidence=?,match_authoritative_evidence=?,superseded_by_attempt_id=?
                    WHERE run_id=? AND status='running'""",
                    (
                        finished,
                        duration,
                        reason.value,
                        reason.value,
                        finished,
                        report.reconciliation_run_id,
                        reason.value,
                        attempt_evidence,
                        match_evidence,
                        superseded,
                        row["run_id"],
                    ),
                ).rowcount
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            report.registry_rows_repaired += repaired_registry
            report.scrape_runs_repaired += repaired_scrapes
        except Exception as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            affected.setdefault(window_id, {})["blocked"] = True
            report.per_item_failures.append(
                {"attempt_id": attempt_id, "error": sanitize_error_summary(exc)}
            )

    # One optimistic lease transition and one planner decision per affected window.
    planned_match_ids: set[int] = set()
    for window_id, action in sorted(affected.items()):
        window = windows[window_id]
        if action.get("active") or action.get("blocked") or window["status"] in {
            "complete",
            "cancelled",
            "not_applicable",
            "failed_terminal",
        }:
            continue
        report.would_expire_leases += int(window["status"] == "leased")
        report.stale_leases_found += int(window["status"] == "leased")
        if action.get("match_evidence") == "authoritative_final":
            report.would_complete_windows += 1
        else:
            report.would_replan_windows += 1
        if report.dry_run:
            continue
        savepoint = "window_" + uuid.uuid5(uuid.NAMESPACE_OID, window_id).hex
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            changed = conn.execute(
                """UPDATE match_stat_windows SET status='due',next_due_at=NULL,
                lease_owner=NULL,lease_token=NULL,lease_claimed_at=NULL,lease_expires_at=NULL,
                recovery_at=?,recovery_run_id=?,recovery_reason=?,recovered_attempt_id=?,
                superseded_attempt_id=?,updated_at=? WHERE window_id=? AND status NOT IN
                ('complete','cancelled','not_applicable','failed_terminal') AND
                (lease_token IS ? OR lease_token=?)""",
                (
                    _iso(now),
                    report.reconciliation_run_id,
                    action.get("reason"),
                    action.get("attempt_id"),
                    action.get("superseded"),
                    _iso(now),
                    window_id,
                    window.get("lease_token"),
                    window.get("lease_token"),
                ),
            ).rowcount
            if changed:
                report.stale_leases_expired += int(window["status"] == "leased")
                planned_match_ids.add(int(window["match_id"]))
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            report.per_item_failures.append(
                {"window_id": window_id, "error": sanitize_error_summary(exc)}
            )

    for match_id in sorted(planned_match_ids):
        savepoint = f"planner_{match_id}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            reconcile_match_windows(
                conn,
                now=now,
                settings=window_settings,
                correlation_id=report.reconciliation_run_id,
                match_ids={match_id},
            )
            current = conn.execute(
                "SELECT status FROM match_stat_windows WHERE match_id=? AND policy_version=?",
                (match_id, window_settings.policy_version),
            ).fetchone()
            if current and current[0] == "complete":
                report.windows_completed_from_existing_data += 1
            else:
                report.windows_replanned += 1
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            report.per_item_failures.append(
                {"match_id": str(match_id), "error": sanitize_error_summary(exc)}
            )


def reconcile_interrupted_attempts(
    *,
    trigger_source: str = "startup",
    dry_run: bool = False,
    scope: RecoveryScope | None = None,
    settings: RecoverySettings | None = None,
    window_settings: MatchWindowSettings | None = None,
    now: datetime | None = None,
    lane=write_lane,
    run_id: str | None = None,
) -> ReconciliationReport:
    if trigger_source not in {"startup", "manual", "test"}:
        raise ValueError("trigger_source must be startup, manual, or test")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    settings = settings or RecoverySettings.from_config()
    window_settings = window_settings or MatchWindowSettings.from_config()
    settings.validate(lease_duration=window_settings.lease_duration)
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
            "heartbeat_seconds": int(settings.heartbeat_interval.total_seconds()),
            "startup_candidate_limit": settings.startup_candidate_limit,
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
        lambda conn: _reconcile(
            conn,
            report,
            now=now,
            settings=settings,
            scope=scope,
            window_settings=window_settings,
            startup_candidate_limit=(
                settings.startup_candidate_limit if trigger_source == "startup" else None
            ),
        ),
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
