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


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


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
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(summary, default=_json_default))
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
    displayed = outcome if args.print_json else outcome.details
    print(json.dumps(displayed, default=_json_default, indent=2 if args.print_json else None))


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
        if args.print_json:
            print(json.dumps(players, indent=2))
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
    log(f"📊 Explicit legacy player-stat collection for match_id {args.scrape_match}: "
        "source_family=html collector=scraper.scrape_afl_player_stats "
        "persistence_target=player_stats fallback_occurred=false", "INFO")
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
        "source_family": "cfs_json", "collector": "MatchPlayerStatsCollector",
        "persistence_target": "cfs_player_stats", "fallback_occurred": False,
        "fallback_reason": None, "match_provider_id": result.match_provider_id,
        "status": result.status.value, "endpoint_source_status": result.endpoint_source_status,
        "stored_canonical_status": reconciliation.stored_status,
        "direct_match_detail_status": reconciliation.direct_status,
        "resolved_match_status": result.resolved_match_status,
        "status_resolution": status_resolution,
        "canonical_match_refreshed": reconciliation.canonical_refreshed,
        "afl_match_id": reconciliation.afl_match_id, "collected_at": result.collected_at,
        "source_endpoint": result.source_endpoint, "records_collected": len(result.records),
        "rows_written": rows_written, "rejected_records": result.rejected_records,
        "diagnostics": [*reconciliation.diagnostics, *result.diagnostics], "records": result.records,
    }
    if args.print_json:
        print(json.dumps(output, indent=2, default=_json_default))
    else:
        output.pop("records")
        output["diagnostics"] = [diagnostic.code for diagnostic in output["diagnostics"]]
        print(json.dumps(output, default=_json_default))


def handle_collect_match_rosters(args):
    from afl_json import AflJsonClient, MatchRosterCollector
    with AflJsonClient() as client:
        result = MatchRosterCollector(client, raw_directory=args.afl_raw_directory).collect(
            args.collect_match_rosters)
    output = {
        "source_family": "cfs_json", "collector": "MatchRosterCollector",
        "persistence_target": None, "persistence_performed": False,
        "fallback_occurred": False, "fallback_reason": None,
        "round_provider_id": result.round_provider_id, "status": result.status.value,
        "publication_state": result.publication_state,
        "provider_timestamp": result.provider_timestamp,
        "provider_version": result.provider_version, "rosters": result.rosters,
        "selections": result.selections,
    }
    if args.print_json:
        print(json.dumps(output, indent=2))
    else:
        output.pop("rosters")
        output["selections"] = len(result.selections)
        print(json.dumps(output))


def handle_bootstrap_afl_season(args):
    from afl_json import (AflJsonClient, PublicAflCollector, persist_afl_metadata,
                          persist_player_seasons)
    from db.connection import get_db_connection
    from db.scrape_runs import audited_scrape_run

    with AflJsonClient() as client:
        collector = PublicAflCollector(client, raw_directory=args.afl_raw_directory)
        result = collector.collect(competition_code=args.afl_competition_code,
                                   competition_provider_id=args.afl_competition_provider_id,
                                   season=args.bootstrap_afl_season)
        player_result = collector.collect_players(result.season["provider_id"])
    conn = get_db_connection()
    try:
        with audited_scrape_run("afl_metadata_bootstrap", target_type="season",
                                target_identifier=args.bootstrap_afl_season, conn=conn) as audit:
            summary = persist_afl_metadata(conn, result)
            player_summary = persist_player_seasons(
                conn, player_result, provider_season_id=result.season["provider_id"])
            audit["rows_read"] = summary.records_read + player_summary.records_read
            audit["rows_written"] = summary.inserted + summary.updated + player_summary.rows_written
    finally:
        conn.close()
    print(json.dumps({
        "competition": result.competition["name"], "season": result.season["name"],
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
        "player_diagnostics": [diagnostic.code for diagnostic in player_result.diagnostics],
    }))


def handle_collect_afl_metadata(args):
    from afl_json import AflJsonClient, PublicAflCollector
    with AflJsonClient() as client:
        result = PublicAflCollector(client, raw_directory=args.afl_raw_directory).collect(
            competition_code=args.afl_competition_code,
            competition_provider_id=args.afl_competition_provider_id, season=args.afl_season)
    output = {"competition": result.competition, "season": result.season,
              "rounds": result.rounds, "teams": result.teams, "matches": result.matches}
    if args.print_json:
        print(json.dumps(output, indent=2))
    else:
        print(json.dumps({"competition": result.competition["name"],
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
