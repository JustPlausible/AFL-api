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
from typing import Callable

import config
from afl_json import (
    AflJsonClient, MatchPlayerStatsCollector, MatchRosterCollector,
    PlayerStatsStatus, PublicAflCollector, RosterStatus,
    persist_afl_metadata, reconcile_match_status, upsert_player_stats,
)
from db.connection import get_db_connection
from db.scrape_runs import TRIGGER_SCHEDULER, audited_scrape_run

logger = logging.getLogger("operational_collection")


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
        OperationalDomain.METADATA, "public_json", "PublicAflCollector", True, False,
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
        OperationalDomain.MATCH_STATUS, "public_json", "reconcile_match_status", True, False,
        "scraper.scrape_afl_matches / scraper.monitor_match_status",
    ),
    OperationalDomain.MATCH_PLAYER_STATS: SourcePolicy(
        OperationalDomain.MATCH_PLAYER_STATS, "cfs_json", "MatchPlayerStatsCollector", True, False,
        "scraper.scrape_afl_player_stats",
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


def collect_operational(
    domain: OperationalDomain | str, *, target_id: int | None = None,
    trigger_source: str = TRIGGER_SCHEDULER, correlation_id: str | None = None,
    client_factory: Callable[[], AflJsonClient] = AflJsonClient,
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
        with audited_scrape_run(
            selected.domain.value, target_type=target_type, target_identifier=target_id,
            trigger_source=trigger_source, correlation_id=correlation_id, conn=conn,
        ) as audit:
            if selected.domain is OperationalDomain.INJURIES:
                from scraper.scrape_afl_injuries import scrape_injury_list, save_injuries_to_db
                records = scrape_injury_list(conn, trigger_source=trigger_source,
                                             correlation_id=correlation_id)
                save_injuries_to_db(records, conn)
                outcome = CollectionOutcome(selected.domain.value, selected.source_family,
                    selected.collector, True, False, None, "success", len(records), len(records), target_id)
            elif selected.domain is OperationalDomain.LINEUPS:
                if target_id is None:
                    raise ValueError("lineup collection requires an internal round ID")
                from db.import_to_db import save_lineups_to_db
                from scraper.scrape_afl_lineups import scrape_team_lineups
                records = scrape_team_lineups(
                    round_number=target_id, trigger_source=trigger_source,
                    correlation_id=correlation_id,
                )
                written = save_lineups_to_db(records, conn, target_id)
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
                        summary = persist_afl_metadata(conn, result)
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
                            conn, client, match_provider_id=provider_id, afl_match_id=target_id
                        )
                        conn.commit()
                        written = int(result.canonical_refreshed)
                        outcome = CollectionOutcome(selected.domain.value, selected.source_family,
                            selected.collector, written > 0, False, None, "success", 1, written, target_id)
                    else:
                        if target_id is None:
                            raise ValueError("match player statistics require an internal match ID")
                        provider_id, stored_status = _match_context(conn, target_id)
                        resolution = reconcile_match_status(
                            conn, client, match_provider_id=provider_id, afl_match_id=target_id
                        )
                        result = MatchPlayerStatsCollector(client).collect(
                            provider_id, afl_match_id=target_id,
                            canonical_match_status=resolution.resolved_status or stored_status,
                        )
                        written = upsert_player_stats(conn, result)
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
