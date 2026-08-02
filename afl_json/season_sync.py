"""Persistent, idempotent orchestration for one canonical AFL season."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import asdict, dataclass, field, replace

from db.scrape_runs import (TRIGGER_CLI, complete_scrape_run, fail_scrape_run,
                            sanitize_error_summary, start_scrape_run)

from .bootstrap import BootstrapSummary, persist_afl_metadata
from .match_status import normalise_match_status, reconcile_match_status
from .player_persistence import PlayerPersistenceSummary, persist_player_seasons
from .player_stats import (MatchPlayerStatsCollector, PlayerStatsStatus,
                           upsert_player_stats)


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
                         raw_directory=None) -> SeasonBootstrapResult:
    """Collect and persist the canonical season foundation without invoking the CLI."""
    # Resolve through the public package boundary so existing callers can
    # inject the collector without patching implementation globals.
    from afl_json import PublicAflCollector
    collector = PublicAflCollector(client, raw_directory=raw_directory)
    result = collector.collect(competition_code=competition_code,
                               competition_provider_id=competition_provider_id,
                               season=season)
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
    skipped_missing_provider_identity: int = 0
    already_complete_unchanged: int = 0
    collected_successfully: int = 0
    unavailable_unpublished: int = 0
    empty: int = 0
    partial: int = 0
    unknown: int = 0
    failed: int = 0
    statistic_rows_inserted: int = 0
    statistic_rows_updated: int = 0
    statistic_rows_unchanged: int = 0
    outcome: str = "failure"
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
    """Compose existing bootstrap, lifecycle, CFS and writer boundaries."""

    def __init__(self, client, conn: sqlite3.Connection, *, bootstrap=bootstrap_afl_season,
                 collector_factory=MatchPlayerStatsCollector):
        self.client = client
        self.conn = conn
        self.bootstrap = bootstrap
        self.collector_factory = collector_factory

    def run(self, *, season: str | int, competition_code: str,
            competition_provider_id: str, options: SeasonSyncOptions = SeasonSyncOptions(),
            raw_directory=None) -> SeasonSyncResult:
        summary = SeasonSyncResult(season)
        parent_id = start_scrape_run(
            "afl_season_sync", target_type="season", target_identifier=season,
            trigger_source=TRIGGER_CLI, correlation_id=summary.correlation_id, conn=self.conn,
        )
        summary.audit_id = parent_id
        try:
            foundation = self.bootstrap(
                self.client, self.conn, season=season,
                competition_code=competition_code,
                competition_provider_id=competition_provider_id,
                raw_directory=raw_directory,
            )
            summary.competition_id = foundation.competition_id
            summary.competition_provider_id = foundation.competition_provider_id
            summary.season_id = foundation.season_id
            summary.season_provider_id = foundation.season_provider_id
            summary.bootstrap_outcome = foundation.status
            if foundation.status != "success":
                summary.outcome = "failure"
                fail_scrape_run(parent_id, "canonical player bootstrap unavailable", conn=self.conn)
                return summary

            rows = self._matches(foundation.season_id, options)
            summary.total_matches_discovered = len(rows)
            collector = self.collector_factory(self.client, raw_directory=raw_directory)
            for row in rows:
                self._process_match(row, options, collector, summary)
            material = (summary.failed + summary.unavailable_unpublished + summary.empty
                        + summary.partial + summary.unknown)
            summary.outcome = "partial" if material else "success"
            complete_scrape_run(
                parent_id,
                rows_read=summary.total_matches_discovered,
                rows_written=summary.statistic_rows_inserted + summary.statistic_rows_updated,
                partial=summary.outcome == "partial", conn=self.conn,
            )
            return summary
        except Exception as exc:
            summary.outcome = "failure"
            summary.failed += 1
            fail_scrape_run(parent_id, exc, conn=self.conn)
            summary.matches.append(MatchSyncResult(
                0, None, None, "foundation_failed", error=sanitize_error_summary(exc)
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
            "SELECT m.match_id,m.match_provider_id,m.status,r.round_number "
            "FROM matches m LEFT JOIN rounds r ON r.round_id=m.round_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY r.round_number,m.match_id", params,
        ).fetchall()

    def _process_match(self, row: sqlite3.Row, options: SeasonSyncOptions, collector,
                       summary: SeasonSyncResult) -> None:
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
                return
        if lifecycle != "CONCLUDED":
            outcome = "unknown_lifecycle" if lifecycle is None else (
                "partial_lifecycle" if lifecycle in {"LIVE", "POSTGAME"} else "not_concluded")
            if outcome == "unknown_lifecycle":
                summary.unknown += 1
            elif outcome == "partial_lifecycle":
                summary.partial += 1
            else:
                summary.skipped_not_concluded += 1
            summary.matches.append(MatchSyncResult(match_id, provider_id, round_number,
                                                    outcome, lifecycle))
            return
        if not provider_id:
            summary.skipped_missing_provider_identity += 1
            summary.matches.append(MatchSyncResult(match_id, None, round_number,
                                                    "missing_provider_identity", lifecycle))
            return
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
            summary.matches.append(MatchSyncResult(match_id, provider_id, round_number,
                                                    "already_complete", lifecycle,
                                                    rows_unchanged=concluded_rows))
            return

        audit_id = start_scrape_run(
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
            complete_scrape_run(audit_id, rows_read=len(result.records), rows_written=written,
                                partial=status is not PlayerStatsStatus.CONCLUDED, conn=self.conn)
            summary.matches.append(MatchSyncResult(
                match_id, provider_id, round_number, outcome, lifecycle, len(result.records),
                inserted, updated, unchanged, audit_id,
            ))
        except Exception as exc:
            self.conn.rollback()
            summary.failed += 1
            error = sanitize_error_summary(exc)
            fail_scrape_run(audit_id, exc, conn=self.conn)
            summary.matches.append(MatchSyncResult(match_id, provider_id, round_number,
                                                    "failed", lifecycle, audit_id=audit_id,
                                                    error=error))
