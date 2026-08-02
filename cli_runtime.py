"""Operation-specific CLI handlers.

This module itself is lightweight. Each handler imports only the runtime family
needed for the selected operation.
"""
import json
import sqlite3
import sys
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path

from collection.diagnostics import CollectionDiagnostic


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _diagnostic_output(diagnostic, *, details=None, pretty=False):
    """Serialize one envelope and its non-conflicting domain-specific details."""
    return json.dumps(diagnostic.to_dict(details=details), default=_json_default,
                      indent=2 if pretty else None)


def import_clubs_to_db():
    """Load clubs from the canonical seed and import using a shared connection."""
    from db.club_seed import upsert_club_seed
    from db.connection import get_db_connection
    from utils.log import log

    conn = get_db_connection()
    count = upsert_club_seed(conn)
    conn.commit()
    conn.close()
    log(f"✅ Imported {count} canonical clubs into DB", "SUCCESS")


def _scrape_all_clubs(skip_existing=False):
    from scraper.scrape_afl_clubs import save_club_players_to_json
    from utils.club_lookup import load_clubs

    summaries = [save_club_players_to_json(club, skip_existing=skip_existing)
                 for club in load_clubs()]
    print("\n📊 Scrape Summary:")
    print(f"{'Club':<30} {'Total':>5}  {'Missing Image':>14}  {'Missing CD ID':>15}  {'Missing Club ID':>15}")
    print("-" * 85)
    for summary in summaries:
        print(f"{summary['club']:<30} {summary['total']:>5}  {summary['missing_image']:>14}  "
              f"{summary['missing_champion_id']:>15}  {summary['missing_club_id']:>15}")


def _enrich_all_clubs():
    from merge.helpers import resolve_players_for_club

    for path in sorted(Path("data").glob("players-*-raw.json")):
        resolve_players_for_club(path.stem.replace("players-", "").replace("-raw", ""))


def handle_collect_afl_data(args):
    from afl_json import (AflJsonClient, BatchCollectionError,
                          CollectionOrchestrator, CollectionRequest)

    families = (tuple(item.strip() for item in args.collection_endpoints.split(",")
                      if item.strip()) if args.collection_endpoints else
                ("metadata", "players", "fixtures", "rosters", "lineups", "player-stats"))
    mode = "overwrite" if args.collection_overwrite else ("resume" if args.collection_resume else "new")
    request = CollectionRequest(
        season=args.afl_season, output=args.collection_output,
        rounds=tuple(args.collection_round), matches=tuple(args.collection_match),
        endpoint_families=families, competition_code=args.afl_competition_code,
        competition_provider_id=args.afl_competition_provider_id, mode=mode,
    )
    try:
        with AflJsonClient() as client:
            summary = CollectionOrchestrator(client).run(request)
    except (BatchCollectionError, FileExistsError, ValueError) as exc:
        diagnostic = CollectionDiagnostic(
            operation="collect_afl_data", domain="orchestration",
            source_family="public_afl_json+cfs_json", collector="CollectionOrchestrator",
            mode="database_free", database_opened=False, persistence_target="none",
            result_status="failed", result_detail=exc.__class__.__name__,
            fallback_allowed=False, fallback_occurred=False,
        )
        print(_diagnostic_output(diagnostic), file=sys.stderr)
        raise SystemExit(1) from None
    status = summary.get("status", "unknown")
    common_status = "success" if status in {"success", "successful"} else status
    diagnostic = CollectionDiagnostic(
        operation="collect_afl_data", domain="orchestration",
        source_family="public_afl_json+cfs_json", collector="CollectionOrchestrator",
        mode="database_free", database_opened=False, persistence_target="none",
        result_status=common_status if common_status in {"success", "partial", "failed", "skipped"} else "unknown",
        fallback_allowed=False, fallback_occurred=False,
        season_id=args.afl_season,
    )
    print(_diagnostic_output(diagnostic, details=summary))
    if summary["status"] == "failed":
        raise SystemExit(1)


def handle_scrape_club(args):
    from scraper.scrape_afl_clubs import save_club_players_to_json
    from utils.club_lookup import get_club
    from utils.log import log

    club = get_club(args.scrape_club.lower())
    if club:
        save_club_players_to_json(club)
    else:
        log(f"❌ Unknown club: {args.scrape_club}", "ERROR")


def handle_scrape_clubs(args):
    _scrape_all_clubs(skip_existing=args.skip_existing)


def handle_enrich_club(args):
    from merge.helpers import resolve_players_for_club
    resolve_players_for_club(args.enrich_club.lower())


def handle_enrich_clubs(args):
    _enrich_all_clubs()


def handle_scrape_enrich_all(args):
    from db.import_to_db import import_players
    _scrape_all_clubs(skip_existing=args.skip_existing)
    _enrich_all_clubs()
    import_players()


def handle_scrape_injuries(args):
    from collection.source_policy import OperationalDomain, collect_operational
    from db.scrape_runs import TRIGGER_CLI

    outcome = collect_operational(OperationalDomain.INJURIES, trigger_source=TRIGGER_CLI)
    diagnostic = CollectionDiagnostic(
        operation="scrape_injuries", domain="injuries", source_family="html",
        collector=outcome.collector, mode="persistent", database_opened=True,
        persistence_target="injuries,injury_history", persistence_action="upsert",
        records_received=outcome.rows_read, rows_written=outcome.rows_written,
        result_status=outcome.status, fallback_allowed=False, fallback_occurred=False,
        diagnostic_count=len((outcome.details or {}).get("diagnostics", ())),
    )
    print(_diagnostic_output(diagnostic, details=outcome.details,
                             pretty=args.print_json))


def handle_scrape_lineups(args):
    from db.connection import get_db_connection
    from db.import_to_db import save_lineups_to_db
    from db.scrape_runs import audited_scrape_run
    from scraper.scrape_afl_lineups import scrape_team_lineups
    from utils.log import log

    log(f"🧹 Scraping team lineups for Round {args.scrape_lineups}", "INFO")
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    with audited_scrape_run("lineup", target_type="round",
                            target_identifier=args.scrape_lineups, conn=conn) as audit:
        players = scrape_team_lineups(round_number=args.scrape_lineups)
        audit["rows_read"] = len(players)
        save_lineups_to_db(players, conn, args.scrape_lineups)
        audit["rows_written"] = len(players)
        diagnostic = CollectionDiagnostic(
            operation="scrape_lineups", domain="lineups", source_family="html",
            collector="scraper.scrape_afl_lineups", mode="persistent",
            database_opened=True, persistence_target="lineups,player_lineups",
            persistence_action="upsert", records_received=len(players),
            rows_written=len(players), result_status="success" if players else "empty",
            fallback_allowed=False, fallback_occurred=False,
            round_id=args.scrape_lineups, audit_id=audit["run_id"],
        )
        details = {"records": players} if args.print_json else None
        print(_diagnostic_output(diagnostic, details=details, pretty=args.print_json))
    conn.close()


def handle_import_clubs(_args):
    from utils.log import log
    log("📥 Importing clubs from JSON to DB...", "INFO")
    import_clubs_to_db()


def handle_export_clubs(_args):
    from db.import_to_db import export_clubs_from_db
    from utils.log import log
    log("📤 Exporting clubs from DB to backup JSON...", "INFO")
    export_clubs_from_db()


def handle_scrape_round(args):
    from scraper import scrape_afl_matches
    from utils.log import log
    log(f"📥 Scraping match data for round_id {args.scrape_round}", "INFO")
    scrape_afl_matches.run(round_id=args.scrape_round)


def handle_scrape_all_rounds(_args):
    from scraper import scrape_afl_matches
    from utils.log import log
    log("📥 Scraping all match data from DB rounds...", "INFO")
    scrape_afl_matches.run(round_id=None)


def handle_scrape_match(args):
    from scraper import scrape_afl_player_stats
    from utils.log import log
    diagnostic = CollectionDiagnostic(
        operation="scrape_match", domain="match_player_stats", source_family="html",
        collector="scraper.scrape_afl_player_stats", mode="legacy_persistent",
        database_opened=True, persistence_target="player_stats", persistence_action="upsert",
        result_status="success", fallback_allowed=False, fallback_occurred=False,
        match_id=args.scrape_match,
    )
    log("📊 Explicit legacy player-stat collection " +
        " ".join(f"{key}={value}" for key, value in diagnostic.to_dict(omit_none=True).items()), "INFO")
    scrape_afl_player_stats.run_scraper(match_id=args.scrape_match, once=True)


def handle_collect_match_player_stats(args):
    from afl_json import (AflJsonClient, MatchPlayerStatsCollector, later_match_status,
                          reconcile_match_status, upsert_player_stats)
    from db.connection import get_db_connection
    from db.scrape_runs import audited_scrape_run

    conn = get_db_connection()
    try:
        with AflJsonClient() as client:
            reconciliation = reconcile_match_status(
                conn, client, match_provider_id=args.collect_match_player_stats,
                afl_match_id=args.afl_match_id)
            resolved_status = later_match_status(reconciliation.resolved_status, args.source_status)
            status_resolution = ("explicit" if args.source_status == resolved_status
                                 and args.source_status != reconciliation.resolved_status
                                 else reconciliation.resolution_source)
            with audited_scrape_run("match_player_stats", target_type="match",
                                    target_identifier=args.collect_match_player_stats,
                                    conn=conn) as audit:
                result = MatchPlayerStatsCollector(client, raw_directory=args.afl_raw_directory).collect(
                    args.collect_match_player_stats, afl_match_id=reconciliation.afl_match_id,
                    canonical_match_status=resolved_status)
                conn.execute("BEGIN")
                try:
                    rows_written = upsert_player_stats(conn, result)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                audit["rows_read"] = len(result.records)
                audit["rows_written"] = rows_written
    finally:
        conn.close()
    output = {
        "fallback_reason": None,
        "match_provider_id": result.match_provider_id, "status": result.status.value,
        "endpoint_source_status": result.endpoint_source_status,
        "stored_canonical_status": reconciliation.stored_status,
        "direct_match_detail_status": reconciliation.direct_status,
        "resolved_match_status": result.resolved_match_status,
        "status_resolution": status_resolution,
        "canonical_match_refreshed": reconciliation.canonical_refreshed,
        "afl_match_id": reconciliation.afl_match_id, "collected_at": result.collected_at,
        "records_collected": len(result.records), "rejected_records": result.rejected_records,
        "diagnostics": [*reconciliation.diagnostics, *result.diagnostics], "records": result.records,
    }
    diagnostic = CollectionDiagnostic(
        operation="collect_match_player_stats", domain="match_player_stats",
        source_family="cfs_json", collector="MatchPlayerStatsCollector", mode="persistent",
        database_opened=True, persistence_target="cfs_player_stats", persistence_action="upsert",
        source_endpoint=result.source_endpoint, records_received=len(result.records) + result.rejected_records,
        records_normalised=len(result.records), records_rejected=result.rejected_records,
        rows_written=rows_written, result_status=result.status.value,
        result_detail=result.endpoint_source_status, fallback_allowed=False,
        fallback_occurred=False, diagnostic_count=len(output["diagnostics"]),
        match_id=reconciliation.afl_match_id, provider_match_id=result.match_provider_id,
        audit_id=audit["run_id"],
    )
    if args.print_json:
        print(_diagnostic_output(diagnostic, details=output, pretty=True))
    else:
        output.pop("records")
        output["diagnostics"] = [diagnostic.code for diagnostic in output["diagnostics"]]
        print(_diagnostic_output(diagnostic, details=output))


def handle_collect_match_rosters(args):
    from afl_json import AflJsonClient, MatchRosterCollector
    with AflJsonClient() as client:
        result = MatchRosterCollector(client, raw_directory=args.afl_raw_directory).collect(
            args.collect_match_rosters)
    output = {
        "persistence_performed": False, "fallback_reason": None,
        "round_provider_id": result.round_provider_id, "status": result.status.value,
        "publication_state": result.publication_state,
        "provider_timestamp": result.provider_timestamp,
        "provider_version": result.provider_version, "rosters": result.rosters,
        "selections": result.selections,
    }
    diagnostic = CollectionDiagnostic(
        operation="collect_match_rosters", domain="match_rosters", source_family="cfs_json",
        collector="MatchRosterCollector", mode="read_only", database_opened=False,
        persistence_target="none", records_received=len(result.selections),
        records_normalised=len(result.selections),
        result_status="success" if result.status.value == "published" else result.status.value,
        result_detail=result.publication_state, fallback_allowed=False, fallback_occurred=False,
        round_id=result.round_provider_id,
    )
    if args.print_json:
        print(_diagnostic_output(diagnostic, details=output, pretty=True))
    else:
        output.pop("rosters")
        output["selections"] = len(result.selections)
        print(_diagnostic_output(diagnostic, details=output))


def handle_bootstrap_afl_season(args):
    from afl_json import AflJsonClient
    from afl_json.season_sync import bootstrap_afl_season
    from db.connection import get_db_connection
    from db.scrape_runs import audited_scrape_run

    conn = get_db_connection()
    try:
        with audited_scrape_run("afl_metadata_bootstrap", target_type="season",
                                target_identifier=args.bootstrap_afl_season, conn=conn) as audit:
            with AflJsonClient() as client:
                result = bootstrap_afl_season(
                    client, conn, season=args.bootstrap_afl_season,
                    competition_code=args.afl_competition_code,
                    competition_provider_id=args.afl_competition_provider_id,
                    raw_directory=args.afl_raw_directory)
            summary, player_summary = result.metadata, result.players
            audit["rows_read"] = summary.records_read + player_summary.records_read
            audit["rows_written"] = summary.inserted + summary.updated + player_summary.rows_written
    finally:
        conn.close()
    details = {
        "competition": result.competition_name, "season": result.season_name,
        "resolved_competition_id": result.competition_id,
        "resolved_season_id": result.season_id,
        "records_read": summary.records_read, "inserted": summary.inserted,
        "updated": summary.updated, "unchanged": summary.unchanged, "failed": summary.failed,
        "player_collection_status": player_summary.status,
        "players_collected": player_summary.records_read,
        "canonical_players_inserted": player_summary.players_inserted,
        "canonical_players_updated": player_summary.players_updated,
        "provider_mappings_inserted": player_summary.mappings_inserted,
        "player_seasons_inserted": player_summary.associations_inserted,
        "player_seasons_updated": player_summary.associations_updated,
        "player_seasons_unchanged": player_summary.unchanged,
        "missing_team_links": player_summary.missing_team_links,
        "player_diagnostics": list(result.player_diagnostics),
    }
    status = "unavailable" if player_summary.status == "unavailable" else (
        "unchanged" if summary.inserted + summary.updated + player_summary.rows_written == 0
        else "success")
    diagnostic = CollectionDiagnostic(
        operation="bootstrap_afl_season", domain="season_bootstrap",
        source_family="public_afl_json+cfs_json", collector="PublicAflCollector",
        mode="composite", database_opened=True,
        persistence_target="afl_metadata,canonical_players,player_seasons",
        persistence_action="upsert", records_received=summary.records_read + player_summary.records_read,
        rows_inserted=summary.inserted + player_summary.players_inserted + player_summary.mappings_inserted + player_summary.associations_inserted,
        rows_updated=summary.updated + player_summary.players_updated + player_summary.associations_updated,
        rows_unchanged=summary.unchanged + player_summary.unchanged,
        rows_written=summary.inserted + summary.updated + player_summary.rows_written,
        result_status=status, result_detail=player_summary.status,
        fallback_allowed=False, fallback_occurred=False,
        diagnostic_count=len(result.player_diagnostics), season_id=result.season_id,
        audit_id=audit["run_id"],
    )
    print(_diagnostic_output(diagnostic, details=details, pretty=args.print_json))


def handle_sync_afl_season(args):
    from afl_json import AflJsonClient
    from afl_json.season_sync import SeasonSynchronizer, SeasonSyncOptions
    from db.connection import get_db_connection

    conn = get_db_connection()
    try:
        with AflJsonClient() as client:
            result = SeasonSynchronizer(client, conn).run(
                season=args.sync_afl_season,
                competition_code=args.afl_competition_code,
                competition_provider_id=args.afl_competition_provider_id,
                raw_directory=args.afl_raw_directory,
                options=SeasonSyncOptions(
                    round_number=args.round, round_from=args.round_from,
                    round_to=args.round_to, match_ids=tuple(args.match_id),
                    refresh_complete=args.refresh_complete,
                ),
            )
    finally:
        conn.close()
    diagnostic = CollectionDiagnostic(
        operation="sync_afl_season", domain="season_sync",
        source_family="public_afl_json+cfs_json",
        collector="SeasonSynchronizer+MatchPlayerStatsCollector", mode="persistent",
        database_opened=True,
        persistence_target="afl_metadata,canonical_players,player_seasons,cfs_player_stats",
        persistence_action="upsert", records_received=result.total_matches_discovered,
        rows_inserted=result.statistic_rows_inserted,
        rows_updated=result.statistic_rows_updated,
        rows_unchanged=result.statistic_rows_unchanged,
        rows_written=result.statistic_rows_inserted + result.statistic_rows_updated,
        result_status=result.outcome, fallback_allowed=False, fallback_occurred=False,
        season_id=result.season_id, audit_id=result.audit_id,
        correlation_id=result.correlation_id,
    )
    envelope_fields = diagnostic.to_dict().keys()
    details = {key: value for key, value in result.to_dict().items()
               if key not in envelope_fields}
    print(_diagnostic_output(diagnostic, details=details, pretty=args.print_json))
    if result.outcome != "success":
        raise SystemExit(1)


def handle_collect_afl_metadata(args):
    from afl_json import AflJsonClient, PublicAflCollector
    with AflJsonClient() as client:
        result = PublicAflCollector(client, raw_directory=args.afl_raw_directory).collect(
            competition_code=args.afl_competition_code,
            competition_provider_id=args.afl_competition_provider_id, season=args.afl_season)
    output = {"competition": result.competition, "season": result.season,
              "rounds": result.rounds, "teams": result.teams, "matches": result.matches}
    received = 2 + len(result.rounds) + len(result.teams) + len(result.matches)
    diagnostic = CollectionDiagnostic(
        operation="collect_afl_metadata", domain="metadata",
        source_family="public_afl_json", collector="PublicAflCollector",
        mode="read_only", database_opened=False, persistence_target="none",
        records_received=received, records_normalised=received, result_status="success",
        fallback_allowed=False, fallback_occurred=False,
        season_id=result.season.get("afl_id"),
    )
    if args.print_json:
        print(_diagnostic_output(diagnostic, details=output, pretty=True))
    else:
        print(_diagnostic_output(diagnostic, details={"competition": result.competition["name"],
                          "season": result.season["name"], "rounds": len(result.rounds),
                          "teams": len(result.teams), "matches": len(result.matches)}))


HANDLERS = {
    "collect_afl_data": handle_collect_afl_data,
    "scrape_club": handle_scrape_club, "scrape_clubs": handle_scrape_clubs,
    "enrich_club": handle_enrich_club, "enrich_clubs": handle_enrich_clubs,
    "scrape_enrich_all": handle_scrape_enrich_all,
    "scrape_injuries": handle_scrape_injuries, "scrape_lineups": handle_scrape_lineups,
    "import_clubs": handle_import_clubs, "export_clubs": handle_export_clubs,
    "scrape_round": handle_scrape_round, "scrape_all_rounds": handle_scrape_all_rounds,
    "scrape_match": handle_scrape_match,
    "collect_match_player_stats": handle_collect_match_player_stats,
    "collect_match_rosters": handle_collect_match_rosters,
    "bootstrap_afl_season": handle_bootstrap_afl_season,
    "sync_afl_season": handle_sync_afl_season,
    "collect_afl_metadata": handle_collect_afl_metadata,
}


def dispatch(args):
    """Invoke the one selected operation after CLI validation has completed."""
    from cli import OPERATION_FLAGS
    selected = [name for name in OPERATION_FLAGS if name != "version"
                and getattr(args, name) is not None and getattr(args, name) is not False]
    if len(selected) != 1:
        raise ValueError("dispatch requires exactly one validated operation")
    HANDLERS[selected[0]](args)
