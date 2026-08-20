"""Persistent, idempotent orchestration for one canonical AFL season."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from db.scrape_runs import (TRIGGER_CLI, complete_scrape_run, fail_scrape_run,
                            record_scrape_decision, sanitize_error_summary,
                            start_scrape_run)

from .bootstrap import BootstrapSummary, persist_afl_metadata
from .match_status import normalise_match_status, reconcile_match_status
from .player_persistence import PlayerPersistenceSummary, persist_player_seasons
from .player_stats import (MatchPlayerStatsCollector, PlayerStatsStatus,
                           upsert_player_stats)


class SeasonSyncDecisionReason(str, Enum):
    """Stable audit vocabulary for decisions made before collection."""

    ALREADY_COMPLETE = "already_complete"
    SCHEDULED = "scheduled"
    LIVE_OR_POSTGAME = "live_or_postgame"
    FUTURE_PLACEHOLDER = "future_placeholder"
    UNRESOLVED_LIFECYCLE = "unresolved_lifecycle"
    MISSING_PROVIDER_IDENTITY = "missing_provider_identity"
    REQUESTED_MATCH_NOT_FOUND = "requested_match_not_found"
    BOUNDED_SELECTION_EMPTY = "bounded_selection_empty"


SAFE_DECISION_REASONS = frozenset({
    SeasonSyncDecisionReason.ALREADY_COMPLETE,
    SeasonSyncDecisionReason.SCHEDULED,
    SeasonSyncDecisionReason.LIVE_OR_POSTGAME,
    SeasonSyncDecisionReason.FUTURE_PLACEHOLDER,
})


@dataclass(frozen=True, slots=True)
class SeasonBootstrapResult:
    competition_id: int
    competition_provider_id: str | None
    season_id: int
    season_provider_id: str | None
    metadata: BootstrapSummary
    players: PlayerPersistenceSummary
    competition_name: str | None = None
    season_name: str | None = None
    player_diagnostics: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return "unavailable" if self.players.status == "unavailable" else "success"


def bootstrap_afl_season(client, conn: sqlite3.Connection, *, season: str | int,
                         competition_code: str, competition_provider_id: str,
                         raw_directory=None,
                         current_season_year: str | int | None = None) -> SeasonBootstrapResult:
    """Collect and persist the canonical season foundation without invoking the CLI.

    ``current_season_year`` is an operator-configured override (e.g. the
    ``AFL_SEASON_YEAR`` deployment setting) identifying the canonical current
    season independently of ``season`` -- so bootstrapping a specific (e.g.
    historical) season does not disturb an already-established current
    season marker. See ``afl_json.collectors.resolve_current_season``.
    """
    # Resolve through the public package boundary so existing callers can
    # inject the collector without patching implementation globals.
    from afl_json import PublicAflCollector
    collector = PublicAflCollector(client, raw_directory=raw_directory)
    result = collector.collect(competition_code=competition_code,
                               competition_provider_id=competition_provider_id,
                               season=season, current_season_year=current_season_year)
    player_result = collector.collect_players(result.season["provider_id"])
    metadata = persist_afl_metadata(conn, result)
    players = persist_player_seasons(conn, player_result,
                                     provider_season_id=result.season["provider_id"])
    return SeasonBootstrapResult(
        result.competition["afl_id"], result.competition.get("provider_id"),
        result.season["afl_id"], result.season.get("provider_id"), metadata, players,
        result.competition.get("name"), result.season.get("name"),
        tuple(diagnostic.code for diagnostic in player_result.diagnostics),
    )


@dataclass(frozen=True, slots=True)
class MatchSyncResult:
    match_id: int
    match_provider_id: str | None
    round_number: int | None
    outcome: str
    lifecycle: str | None = None
    records: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    audit_id: str | None = None
    error: str | None = None
    fallback_occurred: bool = False
    rows_written: int = 0
    collection_outcome: str = "not_attempted"
    persistence_outcome: str = "not_attempted"
    audit_outcome: str = "not_started"
    audit_error_class: str | None = None
    audit_error_summary: str | None = None
    correlation_id: str | None = None
    processing_continued: bool | None = None
    reason_code: str | None = None
    decision_class: str | None = None
    canonical_match_id: int | None = None
    requested_match_id: int | None = None


@dataclass(slots=True)
class SeasonSyncResult:
    requested_season: str | int
    competition_id: int | None = None
    competition_provider_id: str | None = None
    season_id: int | None = None
    season_provider_id: str | None = None
    bootstrap_outcome: str = "failure"
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    audit_id: str | None = None
    total_matches_discovered: int = 0
    eligible_matches: int = 0
    skipped_not_concluded: int = 0
    skipped_scheduled: int = 0
    skipped_live_or_postgame: int = 0
    skipped_future_placeholder: int = 0
    unresolved_lifecycle: int = 0
    skipped_missing_provider_identity: int = 0
    already_complete_unchanged: int = 0
    collected_successfully: int = 0
    unavailable_unpublished: int = 0
    empty: int = 0
    partial: int = 0
    unknown: int = 0
    failed: int = 0
    explicit_matches_requested: int = 0
    explicit_matches_unsatisfied: int = 0
    missing_requested_match_ids: list[int] = field(default_factory=list)
    selection_status: str = "selected"
    selection_reason_code: str | None = None
    selection_decision_class: str | None = None
    statistic_rows_inserted: int = 0
    statistic_rows_updated: int = 0
    statistic_rows_unchanged: int = 0
    outcome: str = "failure"
    audit_outcome: str = "not_started"
    audit_error_class: str | None = None
    audit_error_summary: str | None = None
    audit_failures: int = 0
    processing_stopped_for_safety: bool = False
    matches: list[MatchSyncResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SeasonSyncOptions:
    round_number: int | None = None
    round_from: int | None = None
    round_to: int | None = None
    match_ids: tuple[int, ...] = ()
    refresh_complete: bool = False


class SeasonSynchronizer:
    """Compose existing bootstrap, lifecycle, CFS and writer boundaries.

    The synchronizer owns ``conn`` for the duration of :meth:`run`.  Callers
    must supply a usable connection with no active transaction; the service
    commits and rolls back its bootstrap, per-match, and audit transactions.
    """

    def __init__(self, client, conn: sqlite3.Connection, *, bootstrap=bootstrap_afl_season,
                 collector_factory=MatchPlayerStatsCollector,
                 clock: Callable[[], datetime] | None = None,
                 start_audit=None, complete_audit=None, fail_audit=None,
                 decision_audit=None):
        self.client = client
        self.conn = conn
        self.bootstrap = bootstrap
        self.collector_factory = collector_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.start_audit = start_audit or start_scrape_run
        self.complete_audit = complete_audit or complete_scrape_run
        self.fail_audit = fail_audit or fail_scrape_run
        self.decision_audit = decision_audit or record_scrape_decision

    def run(self, *, season: str | int, competition_code: str,
            competition_provider_id: str, options: SeasonSyncOptions = SeasonSyncOptions(),
            raw_directory=None, current_season_year: str | int | None = None) -> SeasonSyncResult:
        """Synchronise a season using an exclusively owned, transaction-free connection.

        ``RuntimeError`` is raised before any side effect when ``conn`` already
        participates in a caller-managed transaction.
        """
        if self.conn.in_transaction:
            raise RuntimeError(
                "SeasonSynchronizer requires a connection without an active transaction; "
                "the service owns transaction boundaries for the duration of run()"
            )
        summary = SeasonSyncResult(season)
        parent_id = self.start_audit(
            "afl_season_sync", target_type="season", target_identifier=season,
            trigger_source=TRIGGER_CLI, correlation_id=summary.correlation_id, conn=self.conn,
        )
        summary.audit_id = parent_id
        summary.audit_outcome = "running"
        try:
            foundation = self.bootstrap(
                self.client, self.conn, season=season,
                competition_code=competition_code,
                competition_provider_id=competition_provider_id,
                raw_directory=raw_directory,
                current_season_year=current_season_year,
            )
            summary.competition_id = foundation.competition_id
            summary.competition_provider_id = foundation.competition_provider_id
            summary.season_id = foundation.season_id
            summary.season_provider_id = foundation.season_provider_id
            summary.bootstrap_outcome = foundation.status
            if foundation.status != "success":
                summary.outcome = "failure"
                self._finalise_failure(parent_id, "canonical player bootstrap unavailable", summary)
                return summary

            rows = self._matches(foundation.season_id, options)
            summary.total_matches_discovered = len(rows)
            summary.explicit_matches_requested = len(options.match_ids)
            bounded = bool(options.round_number is not None or options.round_from is not None
                           or options.match_ids)
            if not rows:
                summary.selection_status = "empty_bounded" if bounded else "empty_unbounded"
                if bounded:
                    summary.selection_reason_code = (
                        SeasonSyncDecisionReason.BOUNDED_SELECTION_EMPTY.value
                    )
                    summary.selection_decision_class = "material"
                    self._append_decision(
                        summary, SeasonSyncDecisionReason.BOUNDED_SELECTION_EMPTY,
                        target_type="selection", target_identifier=season,
                        append_match=False,
                        diagnostic="bounded season selection matched no canonical matches",
                    )
            collector = self.collector_factory(self.client, raw_directory=raw_directory)
            for index, row in enumerate(rows):
                safe = self._process_match(
                    row, options, collector, summary, has_later=index < len(rows) - 1)
                if not safe:
                    summary.processing_stopped_for_safety = True
                    break
            if summary.processing_stopped_for_safety:
                summary.outcome = "failure"
                summary.audit_outcome = "failed"
                return summary
            if options.match_ids:
                found = {row["match_id"] for row in rows}
                summary.missing_requested_match_ids = [
                    match_id for match_id in options.match_ids if match_id not in found
                ]
                for match_id in summary.missing_requested_match_ids:
                    self._append_decision(
                        summary, SeasonSyncDecisionReason.REQUESTED_MATCH_NOT_FOUND,
                        target_type="requested_match", target_identifier=match_id,
                        requested_match_id=match_id,
                        outcome="missing_requested_match",
                        diagnostic="explicitly requested match was not found",
                    )
                requested_outcomes = {
                    match.match_id: match.outcome for match in summary.matches
                    if match.match_id in options.match_ids
                }
                summary.explicit_matches_unsatisfied = sum(
                    requested_outcomes.get(match_id) not in {"collected", "already_complete"}
                    for match_id in options.match_ids
                )
            material = (summary.failed + summary.unavailable_unpublished + summary.empty
                        + summary.partial + summary.unknown
                        + summary.audit_failures
                        + summary.skipped_missing_provider_identity
                        + summary.unresolved_lifecycle
                        + summary.explicit_matches_unsatisfied
                        + int(summary.selection_status == "empty_bounded"))
            summary.outcome = "partial" if material else "success"
            try:
                self.complete_audit(
                    parent_id, rows_read=summary.total_matches_discovered,
                    rows_written=summary.statistic_rows_inserted + summary.statistic_rows_updated,
                    partial=summary.outcome == "partial", conn=self.conn,
                )
                summary.audit_outcome = ("partial" if summary.audit_failures else "completed")
            except Exception as audit_exc:
                self._record_season_audit_failure(summary, audit_exc)
                if summary.outcome == "success":
                    summary.outcome = "partial"
            return summary
        except Exception as exc:
            summary.outcome = "failure"
            summary.failed += 1
            self.conn.rollback()
            self._finalise_failure(parent_id, exc, summary)
            summary.matches.append(MatchSyncResult(
                0, None, None, "foundation_failed", error=sanitize_error_summary(exc),
                collection_outcome="failed", persistence_outcome="not_attempted",
            ))
            return summary

    def _matches(self, season_id: int, options: SeasonSyncOptions) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        clauses: list[str] = ["m.season_id=?"]
        params: list[object] = [season_id]
        if options.round_number is not None:
            clauses.append("r.round_number=?")
            params.append(options.round_number)
        if options.round_from is not None:
            if options.round_to is None:
                raise ValueError("round_to is required with round_from")
            clauses.append("r.round_number BETWEEN ? AND ?")
            params.extend((options.round_from, options.round_to))
        if options.match_ids:
            clauses.append(f"m.match_id IN ({','.join('?' for _ in options.match_ids)})")
            params.extend(options.match_ids)
        return self.conn.execute(
            "SELECT m.match_id,m.match_provider_id,m.status,m.start_time_utc,r.round_number "
            "FROM matches m LEFT JOIN rounds r ON r.round_id=m.round_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY r.round_number,m.match_id", params,
        ).fetchall()

    def _process_match(self, row: sqlite3.Row, options: SeasonSyncOptions, collector,
                       summary: SeasonSyncResult, *, has_later: bool) -> bool:
        match_id, provider_id, round_number = row["match_id"], row["match_provider_id"], row["round_number"]
        lifecycle = normalise_match_status(row["status"])
        if provider_id and (lifecycle is None or lifecycle in {"LIVE", "POSTGAME"}):
            try:
                resolution = reconcile_match_status(
                    self.conn, self.client, match_provider_id=provider_id,
                    afl_match_id=match_id,
                )
                lifecycle = resolution.resolved_status
                if resolution.canonical_refreshed:
                    self.conn.commit()
            except Exception as exc:
                self.conn.rollback()
                summary.failed += 1
                summary.matches.append(MatchSyncResult(
                    match_id, provider_id, round_number, "failed", lifecycle,
                    error=sanitize_error_summary(exc),
                ))
                return True
        if lifecycle != "CONCLUDED":
            if lifecycle == "SCHEDULED":
                outcome = "scheduled"
                summary.skipped_scheduled += 1
                summary.skipped_not_concluded += 1
            elif lifecycle in {"LIVE", "POSTGAME"}:
                outcome = "live_or_postgame"
                summary.skipped_live_or_postgame += 1
                summary.skipped_not_concluded += 1
            elif _is_future(row["start_time_utc"], self.clock()):
                outcome = "future_placeholder"
                summary.skipped_future_placeholder += 1
                summary.skipped_not_concluded += 1
            else:
                outcome = "unknown_lifecycle"
                summary.unresolved_lifecycle += 1
            self._append_decision(
                summary, (SeasonSyncDecisionReason.UNRESOLVED_LIFECYCLE
                          if outcome == "unknown_lifecycle"
                          else SeasonSyncDecisionReason(outcome)), match_id=match_id,
                provider_id=provider_id, round_number=round_number, lifecycle=lifecycle,
                explicit=match_id in options.match_ids,
                outcome=outcome,
                diagnostic=f"match lifecycle classified as {outcome}",
            )
            return True
        if not provider_id:
            summary.skipped_missing_provider_identity += 1
            self._append_decision(
                summary, SeasonSyncDecisionReason.MISSING_PROVIDER_IDENTITY,
                match_id=match_id, round_number=round_number, lifecycle=lifecycle,
                diagnostic="concluded match has no provider identity",
            )
            return True
        summary.eligible_matches += 1
        concluded_rows = self.conn.execute(
            "SELECT COUNT(*) FROM cfs_player_stats WHERE match_provider_id=? AND snapshot_authority=2",
            (provider_id,),
        ).fetchone()[0]
        completed_audit = self.conn.execute(
            "SELECT rows_read FROM scrape_runs WHERE scrape_type='season_match_player_stats' "
            "AND target_type='match' AND target_identifier=? AND status='completed' "
            "ORDER BY started_at DESC LIMIT 1",
            (provider_id,),
        ).fetchone()
        complete = bool(concluded_rows and completed_audit
                        and completed_audit[0] == concluded_rows)
        if complete and not options.refresh_complete:
            summary.already_complete_unchanged += 1
            summary.statistic_rows_unchanged += concluded_rows
            self._append_decision(
                summary, SeasonSyncDecisionReason.ALREADY_COMPLETE,
                match_id=match_id, provider_id=provider_id,
                round_number=round_number, lifecycle=lifecycle,
                rows_unchanged=concluded_rows,
                diagnostic="authoritative statistics are already complete",
            )
            return True

        audit_id = self.start_audit(
            "season_match_player_stats", target_type="match", target_identifier=provider_id,
            trigger_source=TRIGGER_CLI, correlation_id=summary.correlation_id, conn=self.conn,
        )
        try:
            reconciliation = reconcile_match_status(
                self.conn, self.client, match_provider_id=provider_id, afl_match_id=match_id)
            result = collector.collect(provider_id, afl_match_id=match_id,
                                       canonical_match_status=reconciliation.resolved_status)
            if result.rejected_records and result.status is PlayerStatsStatus.CONCLUDED:
                result = replace(result, status=PlayerStatsStatus.LIVE_PARTIAL)
            before = {r[0] for r in self.conn.execute(
                "SELECT champion_data_player_id FROM cfs_player_stats WHERE match_provider_id=?",
                (provider_id,),
            )}
            self.conn.execute("BEGIN")
            try:
                written = upsert_player_stats(self.conn, result)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            inserted = min(written, sum(r.champion_data_player_id not in before for r in result.records))
            updated = written - inserted
            unchanged = len(result.records) - written
            status = result.status
            if status is PlayerStatsStatus.CONCLUDED:
                outcome = "collected"
                summary.collected_successfully += 1
            elif status is PlayerStatsStatus.UNAVAILABLE:
                outcome = "unavailable"
                summary.unavailable_unpublished += 1
            elif status is PlayerStatsStatus.EMPTY:
                outcome = "empty"
                summary.empty += 1
            elif status is PlayerStatsStatus.LIVE_PARTIAL:
                outcome = "partial"
                summary.partial += 1
            else:
                outcome = "unknown"
                summary.unknown += 1
            summary.statistic_rows_inserted += inserted
            summary.statistic_rows_updated += updated
            summary.statistic_rows_unchanged += unchanged
            match_result = MatchSyncResult(
                match_id, provider_id, round_number, outcome, lifecycle, len(result.records),
                inserted, updated, unchanged, audit_id,
                rows_written=written,
                collection_outcome=status.value, persistence_outcome="committed",
                audit_outcome="completed", correlation_id=summary.correlation_id,
            )
            try:
                self.complete_audit(
                    audit_id, rows_read=len(result.records), rows_written=written,
                    partial=status is not PlayerStatsStatus.CONCLUDED, conn=self.conn)
            except Exception as audit_exc:
                safe = self._connection_usable_after_audit_failure()
                match_result = replace(
                    match_result, audit_outcome="failed",
                    audit_error_class=audit_exc.__class__.__name__,
                    audit_error_summary=sanitize_error_summary(audit_exc),
                    processing_continued=safe and has_later,
                )
                summary.audit_failures += 1
                if not safe:
                    summary.outcome = "failure"
                    summary.processing_stopped_for_safety = True
                summary.matches.append(match_result)
                return safe
            summary.matches.append(match_result)
            return True
        except Exception as exc:
            self.conn.rollback()
            summary.failed += 1
            error = sanitize_error_summary(exc)
            audit_outcome = "failed"
            audit_error_class = audit_error_summary = None
            try:
                self.fail_audit(audit_id, exc, conn=self.conn)
                audit_outcome = "completed"
                safe = True
            except Exception as audit_exc:
                safe = self._connection_usable_after_audit_failure()
                audit_error_class = audit_exc.__class__.__name__
                audit_error_summary = sanitize_error_summary(audit_exc)
                summary.audit_failures += 1
                if not safe:
                    summary.processing_stopped_for_safety = True
            summary.matches.append(MatchSyncResult(
                match_id, provider_id, round_number, "failed", lifecycle,
                audit_id=audit_id, error=error, collection_outcome="failed",
                persistence_outcome="not_committed", audit_outcome=audit_outcome,
                audit_error_class=audit_error_class,
                audit_error_summary=audit_error_summary,
                correlation_id=summary.correlation_id,
                processing_continued=safe and has_later,
            ))
            return safe

    def _append_decision(self, summary: SeasonSyncResult,
                         reason: SeasonSyncDecisionReason, *,
                         match_id: int | None = None,
                         provider_id: str | None = None,
                         round_number: int | None = None,
                         lifecycle: str | None = None,
                         target_type: str = "match",
                         target_identifier: object | None = None,
                         requested_match_id: int | None = None,
                         explicit: bool = False,
                         rows_unchanged: int = 0,
                         outcome: str | None = None,
                         append_match: bool = True,
                         diagnostic: str = "") -> None:
        """Keep decision classification separate from its compact audit write."""
        decision_class = (
            "safe" if reason in SAFE_DECISION_REASONS and not explicit else "material"
        )
        target = target_identifier if target_identifier is not None else match_id
        audit_id = None
        audit_outcome = "completed"
        audit_error_class = audit_error_summary = None
        try:
            audit_id = self.decision_audit(
                "afl_season_sync_decision", target_type=target_type,
                target_identifier=target, reason_code=reason.value,
                decision_class=decision_class,
                correlation_id=summary.correlation_id,
                canonical_match_id=match_id, provider_match_id=provider_id,
                round_identifier=round_number, diagnostic_summary=diagnostic,
                trigger_source=TRIGGER_CLI, conn=self.conn,
            )
        except Exception as exc:
            usable = self._connection_usable_after_audit_failure()
            audit_outcome = "failed"
            audit_error_class = exc.__class__.__name__
            audit_error_summary = sanitize_error_summary(exc)
            summary.audit_failures += 1
            if not usable:
                summary.processing_stopped_for_safety = True
        if not append_match:
            return
        summary.matches.append(MatchSyncResult(
            match_id if match_id is not None else (requested_match_id or 0),
            provider_id, round_number, outcome or reason.value, lifecycle,
            rows_unchanged=rows_unchanged, audit_id=audit_id,
            rows_written=0, collection_outcome="not_attempted",
            persistence_outcome="not_attempted", audit_outcome=audit_outcome,
            audit_error_class=audit_error_class,
            audit_error_summary=audit_error_summary,
            correlation_id=summary.correlation_id, reason_code=reason.value,
            decision_class=decision_class, canonical_match_id=match_id,
            requested_match_id=requested_match_id,
        ))

    def _connection_usable_after_audit_failure(self) -> bool:
        """Clear failed audit work and conservatively verify connection usability."""
        try:
            self.conn.rollback()
            self.conn.execute("SELECT 1").fetchone()
            return not self.conn.in_transaction
        except Exception:
            return False

    def _record_season_audit_failure(self, summary: SeasonSyncResult,
                                     exc: BaseException) -> None:
        self._connection_usable_after_audit_failure()
        summary.audit_outcome = "failed"
        summary.audit_error_class = exc.__class__.__name__
        summary.audit_error_summary = sanitize_error_summary(exc)
        summary.audit_failures += 1

    def _finalise_failure(self, audit_id: str, exc: BaseException | str,
                          summary: SeasonSyncResult) -> None:
        """Finalise once; retain ``exc`` as the primary domain failure."""
        try:
            self.fail_audit(audit_id, exc, conn=self.conn)
            summary.audit_outcome = "completed"
        except Exception as audit_exc:
            self._record_season_audit_failure(summary, audit_exc)


def _is_future(value: object, now: datetime) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) > now.astimezone(timezone.utc)
