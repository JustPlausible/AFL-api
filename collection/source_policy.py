"""Shared JSON-first source policy for scheduler and Admin collection.

This deliberately small application boundary owns source selection.  Entry
points provide an operation and an internal target; they do not choose a
scraper.  CFS unpublished states are reported as unavailable and are never
converted into permission to run HTML.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Protocol

import config
from afl_json import (
    AflJsonClient, MatchPlayerStatsCollector, MatchRosterCollector,
    PlayerStatsStatus, PublicAflCollector, RosterStatus,
    persist_afl_metadata, persist_match_status_resolution, reconcile_match_status,
    upsert_player_stats,
)
from db.connection import get_db_connection
from db.scrape_runs import TRIGGER_SCHEDULER, audited_scrape_run

logger = logging.getLogger("operational_collection")


class PersistenceExecutor(Protocol):
    def __call__(self, operation_name: str, target_id: object,
                 callback: Callable[[Any], Any]) -> Any: ...


class OperationalDomain(str, Enum):
    METADATA = "metadata"
    MATCH_STATUS = "match_status"
    LINEUPS = "lineups"
    MATCH_ROSTERS = "match_rosters"
    MATCH_PLAYER_STATS = "match_player_stats"
    INJURIES = "injuries"


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    domain: OperationalDomain
    source_family: str
    collector: str
    persists: bool
    fallback_permitted: bool
    legacy_collector: str | None = None
    note: str | None = None
    preferred_source_family: str | None = None
    preferred_collector: str | None = None
    preferred_persists: bool | None = None


SOURCE_POLICY = {
    OperationalDomain.METADATA: SourcePolicy(
        OperationalDomain.METADATA, "public_afl_json", "PublicAflCollector", True, False,
        "scraper.scrape_afl_fixtures / scraper.scrape_afl_matches",
    ),
    OperationalDomain.MATCH_ROSTERS: SourcePolicy(
        OperationalDomain.MATCH_ROSTERS, "cfs_json", "MatchRosterCollector", False, False,
        "scraper.scrape_afl_lineups",
        "Canonical roster persistence is not implemented; collection is explicitly read-only.",
    ),
    OperationalDomain.LINEUPS: SourcePolicy(
        OperationalDomain.LINEUPS, "html", "scraper.scrape_afl_lineups", True, False,
        None,
        "HTML remains the operational writer until canonical CFS roster persistence exists.",
        "cfs_json", "MatchRosterCollector", False,
    ),
    OperationalDomain.MATCH_STATUS: SourcePolicy(
        OperationalDomain.MATCH_STATUS, "public_afl_json", "reconcile_match_status", True, False,
        "scraper.scrape_afl_matches / scraper.monitor_match_status",
    ),
    OperationalDomain.MATCH_PLAYER_STATS: SourcePolicy(
        OperationalDomain.MATCH_PLAYER_STATS, "cfs_json", "MatchPlayerStatsCollector", True, False,
        "scraper.scrape_afl_player_stats",
        "Persists only authoritative cfs_player_stats; legacy HTML is explicit and never fallback.",
    ),
    OperationalDomain.INJURIES: SourcePolicy(
        OperationalDomain.INJURIES, "html", "scraper.scrape_afl_injuries", True, False, None,
        "No maintained structured injury source is implemented.",
    ),
}


@dataclass(frozen=True, slots=True)
class CollectionOutcome:
    domain: str
    source_family: str
    collector: str
    persistence_performed: bool
    fallback_occurred: bool
    fallback_reason: str | None
    status: str
    rows_read: int = 0
    rows_written: int = 0
    target: str | int | None = None
    details: dict | None = None


def policy_for(domain: OperationalDomain | str) -> SourcePolicy:
    return SOURCE_POLICY[OperationalDomain(domain)]


def _log(outcome: CollectionOutcome, *, trigger_source: str) -> None:
    logger.info("operational_collection %s", json.dumps({
        **asdict(outcome), "requested_domain": outcome.domain,
        "trigger_source": trigger_source,
    }, sort_keys=True))


def _provider_id(conn, table: str, internal_column: str, internal_id: int) -> str:
    row = conn.execute(
        f"SELECT provider_id FROM {table} WHERE {internal_column}=? LIMIT 1", (internal_id,)
    ).fetchone()
    if row is None or not row[0]:
        raise ValueError(
            f"{table}.{internal_column}={internal_id} has no provider ID; run the public JSON season bootstrap"
        )
    return str(row[0])


def _match_context(conn, match_id: int) -> tuple[str, str | None]:
    row = conn.execute(
        "SELECT match_provider_id, status FROM matches WHERE match_id=? LIMIT 1", (match_id,)
    ).fetchone()
    if row is None or not row[0]:
        raise ValueError(
            f"matches.match_id={match_id} has no provider ID; run the public JSON season bootstrap"
        )
    return str(row[0]), row[1]


def _reconcile_window_for_advanced_status(conn, match_id: int) -> None:
    """Reuse the existing targeted match-window reconciliation immediately after
    matches.status advances, so match_stat_windows.lifecycle (and therefore
    polling cadence/phase) does not lag the canonical match until the next
    scheduled reconciliation sweep or a scheduler restart."""
    from scheduler.match_windows import reconcile as reconcile_match_windows
    reconcile_match_windows(conn, match_ids={match_id}, correlation_id="match_status_advance")


def collect_operational(
    domain: OperationalDomain | str, *, target_id: int | None = None,
    trigger_source: str = TRIGGER_SCHEDULER, correlation_id: str | None = None,
    client_factory: Callable[[], AflJsonClient] = AflJsonClient,
    write_executor: PersistenceExecutor | None = None,
) -> CollectionOutcome:
    """Run the policy-selected collector without implicit dual execution."""
    selected = policy_for(domain)
    conn = get_db_connection()
    target_type = "season" if selected.domain is OperationalDomain.METADATA else (
        "injury_list" if selected.domain is OperationalDomain.INJURIES else
        "round" if selected.domain in {OperationalDomain.MATCH_ROSTERS, OperationalDomain.LINEUPS}
        else "match"
    )
    try:
        if selected.domain is OperationalDomain.INJURIES:
            from scraper.injuries.orchestration import collect_injuries
            injury = collect_injuries(
                conn, trigger_source=trigger_source, correlation_id=correlation_id
            )
            return CollectionOutcome(
                selected.domain.value, selected.source_family, selected.collector,
                True, False, None, injury.status, injury.rows_parsed,
                injury.rows_persisted, target_id, {
                    "rows_parsed": injury.rows_parsed,
                    "rows_resolved": injury.rows_resolved,
                    "rows_persisted": injury.rows_persisted,
                    "rows_unresolved": injury.rows_unresolved,
                    "rows_ambiguous": injury.rows_ambiguous,
                    "status": injury.status,
                    "diagnostics": list(injury.diagnostics),
                },
            )
        with audited_scrape_run(
            selected.domain.value, target_type=target_type, target_identifier=target_id,
            trigger_source=trigger_source, correlation_id=correlation_id,
            conn=None if write_executor else conn, write_executor=write_executor,
        ) as audit:
            if selected.domain is OperationalDomain.LINEUPS:
                if target_id is None:
                    raise ValueError("lineup collection requires an internal round ID")
                from db.import_to_db import save_lineups_to_db
                from scraper.scrape_afl_lineups import scrape_team_lineups
                records = scrape_team_lineups(
                    round_number=target_id, trigger_source=trigger_source,
                    correlation_id=correlation_id,
                )
                persist = lambda db: save_lineups_to_db(records, db, target_id)
                written = (write_executor("lineups.persist_round", target_id, persist)
                           if write_executor else persist(conn))
                status = "success" if written else "unavailable"
                outcome = CollectionOutcome(selected.domain.value, selected.source_family,
                    selected.collector, written > 0, False, None, status,
                    len(records), written, target_id)
            else:
                with client_factory() as client:
                    if selected.domain is OperationalDomain.METADATA:
                        season = config.AFL_SEASON_YEAR or datetime.now(timezone.utc).year
                        result = PublicAflCollector(client).collect(
                            competition_code=config.AFL_COMPETITION_CODE,
                            competition_provider_id=config.AFL_COMPETITION_PROVIDER_ID,
                            season=season,
                        )
                        persist = lambda db: persist_afl_metadata(db, result)
                        summary = (write_executor("metadata.persist", target_id, persist)
                                   if write_executor else persist(conn))
                        outcome = CollectionOutcome(selected.domain.value, selected.source_family,
                            selected.collector, True, False, None, "success",
                            summary.records_read, summary.inserted + summary.updated, target_id)
                    elif selected.domain is OperationalDomain.MATCH_ROSTERS:
                        if target_id is None:
                            raise ValueError("match rosters require an internal round or match ID")
                        # A match target is resolved to its round by callers before dispatch.
                        provider_id = _provider_id(conn, "rounds", "round_id", target_id)
                        result = MatchRosterCollector(client).collect(provider_id)
                        status = "unavailable" if result.status is RosterStatus.UNAVAILABLE else "success"
                        outcome = CollectionOutcome(selected.domain.value, selected.source_family,
                            selected.collector, False, False, None, status,
                            len(result.selections), 0, target_id)
                    elif selected.domain is OperationalDomain.MATCH_STATUS:
                        if target_id is None:
                            raise ValueError("match status requires an internal match ID")
                        provider_id, _ = _match_context(conn, target_id)
                        result = reconcile_match_status(
                            conn, client, match_provider_id=provider_id,
                            afl_match_id=target_id, persist=write_executor is None,
                        )
                        if write_executor:
                            def _persist_status(db):
                                refreshed = persist_match_status_resolution(db, result)
                                if refreshed:
                                    _reconcile_window_for_advanced_status(db, target_id)
                                return refreshed
                            written = int(write_executor(
                                "match_status.persist", target_id, _persist_status,
                            ))
                        else:
                            if result.canonical_refreshed:
                                _reconcile_window_for_advanced_status(conn, target_id)
                            conn.commit()
                            written = int(result.canonical_refreshed)
                        outcome = CollectionOutcome(selected.domain.value, selected.source_family,
                            selected.collector, written > 0, False, None, "success", 1, written, target_id)
                    else:
                        if target_id is None:
                            raise ValueError("match player statistics require an internal match ID")
                        provider_id, stored_status = _match_context(conn, target_id)
                        resolution = reconcile_match_status(
                            conn, client, match_provider_id=provider_id,
                            afl_match_id=target_id, persist=write_executor is None,
                        )
                        result = MatchPlayerStatsCollector(client).collect(
                            provider_id, afl_match_id=target_id,
                            canonical_match_status=resolution.resolved_status or stored_status,
                        )
                        def persist_stats(db):
                            refreshed = persist_match_status_resolution(db, resolution)
                            if refreshed:
                                _reconcile_window_for_advanced_status(db, target_id)
                            return upsert_player_stats(db, result)
                        written = (write_executor("cfs_player_stats.persist_match", target_id, persist_stats)
                                   if write_executor else upsert_player_stats(conn, result))
                        if not write_executor:
                            if resolution.canonical_refreshed:
                                _reconcile_window_for_advanced_status(conn, target_id)
                            conn.commit()
                        status = "unavailable" if result.status is PlayerStatsStatus.UNAVAILABLE else "success"
                        outcome = CollectionOutcome(selected.domain.value, selected.source_family,
                            selected.collector, written > 0, False, None, status,
                            len(result.records), written, target_id)
            audit["rows_read"] = outcome.rows_read
            audit["rows_written"] = outcome.rows_written
        _log(outcome, trigger_source=trigger_source)
        return outcome
    except Exception:
        logger.exception(
            "operational_collection failure domain=%s source_family=%s collector=%s "
            "persistence_supported=%s fallback_occurred=false",
            selected.domain.value, selected.source_family, selected.collector, selected.persists,
        )
        raise
    finally:
        conn.close()


def round_for_match(match_id: int) -> int:
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT round_id FROM matches WHERE match_id=? LIMIT 1", (match_id,)).fetchone()
        if row is None or row[0] is None:
            raise ValueError(f"matches.match_id={match_id} has no round")
        return int(row[0])
    finally:
        conn.close()
