import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from version import __version__

# Keep the conventional version probe independent of the operational CLI's
# scraper, browser, configuration, and database imports below.  The argparse
# action remains registered as well so generated help documents the surface.
if __name__ == "__main__" and sys.argv[1:] == ["--version"]:
    print(__version__)
    raise SystemExit(0)

from utils.log import log
from scraper.scrape_afl_clubs import save_club_players_to_json
from scraper.scrape_afl_injuries import scrape_injury_list, save_injuries_to_db
from scraper.scrape_afl_lineups import scrape_team_lineups
from scraper import scrape_afl_matches
from db.scrape_runs import audited_scrape_run
from merge.helpers import resolve_players_for_club
from utils.club_lookup import load_clubs, get_club
from db.import_to_db import import_players, save_lineups_to_db
from db.connection import get_db_connection
from db.club_seed import upsert_club_seed
from afl_json import (
    AflJsonClient, MatchPlayerStatsCollector, MatchRosterCollector, PublicAflCollector,
    BatchCollectionError, CollectionOrchestrator, CollectionRequest,
    later_match_status, persist_afl_metadata, persist_player_seasons,
    reconcile_match_status, upsert_player_stats,
)
from config import AFL_COMPETITION_CODE, AFL_COMPETITION_PROVIDER_ID, AFL_SEASON_YEAR, DB_PATH


def _json_default(value):
    """Keep precise validated decimals printable by diagnostic commands."""
    from dataclasses import asdict, is_dataclass
    from decimal import Decimal
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _provider_id(value: str, *, prefix: str, label: str, example: str) -> str:
    """Validate opaque Champion Data identifiers before any CFS request."""
    candidate = value.strip()
    if not re.fullmatch(rf"{re.escape(prefix)}[A-Za-z0-9_-]+", candidate):
        raise argparse.ArgumentTypeError(
            f"{label} must be a Champion Data provider ID in the form {prefix}... "
            f"(example: {example}); numeric AFL identifiers are not accepted"
        )
    return candidate


def cfs_round_provider_id(value: str) -> str:
    return _provider_id(value, prefix="CD_R", label="round provider ID",
                        example="CD_R202601421")


def cfs_match_provider_id(value: str) -> str:
    return _provider_id(value, prefix="CD_M", label="match provider ID",
                        example="CD_M20260142001")

def import_clubs_to_db():
    """Load clubs from the canonical seed and import using a shared connection."""
    conn = get_db_connection()
    count = upsert_club_seed(conn)
    conn.commit()
    conn.close()
    log(f"✅ Imported {count} canonical clubs into DB", "SUCCESS")

def scrape_all_clubs(skip_existing=False):
    clubs = load_clubs()
    summaries = []

    for club in clubs:
        summary = save_club_players_to_json(club, skip_existing=skip_existing)
        summaries.append(summary)

    print("\n📊 Scrape Summary:")
    print(f"{'Club':<30} {'Total':>5}  {'Missing Image':>14}  {'Missing CD ID':>15}  {'Missing Club ID':>15}")
    print("-" * 85)
    for s in summaries:
        print(f"{s['club']:<30} {s['total']:>5}  {s['missing_image']:>14}  {s['missing_champion_id']:>15}  {s['missing_club_id']:>15}")

def enrich_all_clubs(skip_existing=False):
    raw_files = Path("data").glob("players-*-raw.json")
    for path in sorted(raw_files):
        club_name = path.stem.replace("players-", "").replace("-raw", "")
        resolve_players_for_club(club_name)

def scrape_injuries_to_db(print_json=False):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    with audited_scrape_run("injury", target_type="injury_list", conn=conn) as audit:
        data = scrape_injury_list(conn)
        audit["rows_read"] = sum(team.get("player_count", 0) for team in data.get("teams", []))
        summary = save_injuries_to_db(data, conn)
        audit["rows_written"] = summary["rows_persisted"]
        audit["status"] = summary["status"]
        if print_json:
            print(json.dumps({**data, "summary": summary}, indent=2))
        else:
            print(json.dumps(summary))
    conn.close()

def scrape_lineups_to_db(round_number: int, print_json: bool = False):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    log(f"🧹 Scraping and importing lineups for Round {round_number}", "INFO")
    with audited_scrape_run("lineup", target_type="round", target_identifier=round_number, conn=conn) as audit:
        players = scrape_team_lineups(round_number=round_number)
        audit["rows_read"] = len(players)
        save_lineups_to_db(players, conn, round_number)
        audit["rows_written"] = len(players)

        if print_json:
            import json
            print(json.dumps(players, indent=2))

    conn.close()

def create_parser() -> argparse.ArgumentParser:
    """Build the flag-based CLI parser without loading runtime scraper data."""
    parser = argparse.ArgumentParser(
        description=("AFL operator CLI: preferred AFL/CFS JSON collection and "
                     "explicit legacy HTML tools")
    )
    parser.add_argument("--version", action="version", version=__version__,
                        help="Print the AFL-api version and exit")

    # 🔹 Club-related arguments
    club_group = parser.add_argument_group("Club import, export, HTML scrape, and enrichment")
    club_group.add_argument("--scrape-club", metavar="CLUB_NAME", help="Legacy HTML: scrape one club's players to a raw JSON file")
    club_group.add_argument("--scrape-clubs", action="store_true", help="Legacy HTML: scrape all clubs to raw JSON files")
    club_group.add_argument("--enrich-club", metavar="CLUB_NAME", help="Enrich one existing raw club JSON file locally")
    club_group.add_argument("--enrich-clubs", action="store_true", help="Enrich all existing raw club JSON files locally")
    club_group.add_argument("--scrape-enrich-all", action="store_true", help="Legacy HTML: scrape and enrich every club, then persist players")
    club_group.add_argument("--skip-existing", action="store_true", help="(Club-only) Skip if output file already exists")

    # 🔹 Match + fixture scraping
    match_group = parser.add_argument_group("Explicit legacy AFL HTML collection")
    match_group.add_argument("--scrape-injuries", action="store_true", help="Legacy HTML: collect the AFL injury list and persist injuries")
    match_group.add_argument("--scrape-lineups", type=int, metavar="ROUND", help="Explicit legacy HTML lineup scrape (persists legacy lineup tables)")
    match_group.add_argument("--scrape-round", type=int, metavar="ROUND_ID", help="Explicit legacy HTML match scrape for a round_id")
    match_group.add_argument("--scrape-all-rounds", action="store_true", help="Explicit legacy HTML match scrape for all database rounds")
    match_group.add_argument("--scrape-match", type=int, metavar="MATCH_ID", help="Explicit legacy HTML player-stat scrape; persists to player_stats (not fallback or dual-write)")

    metadata_group = parser.add_argument_group("Preferred AFL public JSON")
    metadata_group.add_argument("--collect-afl-metadata", action="store_true",
                                help="Collect competition, season, rounds, teams and matches without database writes")
    metadata_group.add_argument("--collect-afl-data", action="store_true",
                                help="Run the full modular JSON pipeline to files only (never writes the database)")
    metadata_group.add_argument("--bootstrap-afl-season", metavar="SEASON",
                                help="Persist AFL metadata plus CFS players and season membership")
    metadata_group.add_argument("--afl-season", default=AFL_SEASON_YEAR, metavar="SEASON",
                                help="Select a season by year, AFL ID, provider ID or exact name")
    metadata_group.add_argument("--afl-competition-code", default=AFL_COMPETITION_CODE, metavar="CODE",
                                help="Stable Premiership competition code")
    metadata_group.add_argument("--afl-competition-provider-id", default=AFL_COMPETITION_PROVIDER_ID, metavar="PROVIDER_ID",
                                help="Stable Premiership provider ID")
    cfs_group = parser.add_argument_group("Preferred Champion Data/CFS JSON")
    cfs_group.add_argument("--collect-match-rosters", metavar="ROUND_PROVIDER_ID",
                                type=cfs_round_provider_id,
                                help="Collect CFS selections read-only; requires CD_R... (example: CD_R202601421)")
    cfs_group.add_argument("--collect-match-player-stats", metavar="MATCH_PROVIDER_ID",
                                type=cfs_match_provider_id,
                                help="Collect CFS stats into cfs_player_stats; requires CD_M... (example: CD_M20260142001)")
    cfs_group.add_argument("--source-status", metavar="STATUS",
                           help="With --collect-match-player-stats: explicit canonical status fallback")
    cfs_group.add_argument("--afl-match-id", type=int, metavar="AFL_MATCH_ID",
                           help="With --collect-match-player-stats: numeric AFL ID for canonical status resolution")

    output_group = parser.add_argument_group("Output and JSON diagnostics")
    output_group.add_argument("--print-json", action="store_true",
                              help="Print full collected/normalised JSON; does not disable persistence")
    output_group.add_argument("--afl-raw-directory", type=Path, metavar="PATH",
                              help="Retain original JSON responses below PATH; never stores credentials")
    output_group.add_argument("--collection-output", type=Path, metavar="PATH",
                              help="Output directory required by --collect-afl-data")
    output_group.add_argument("--collection-round", type=int, action="append", default=[], metavar="ROUND",
                              help="With --collect-afl-data: select a round number (repeatable)")
    output_group.add_argument("--collection-match", action="append", default=[], metavar="MATCH",
                              help="With --collect-afl-data: select an AFL numeric or CD_M provider match ID")
    output_group.add_argument("--collection-endpoints", metavar="FAMILIES",
                              help="With --collect-afl-data: comma-separated endpoint families")
    collection_mode = output_group.add_mutually_exclusive_group()
    collection_mode.add_argument("--collection-overwrite", action="store_true",
                                 help="Atomically replace deterministic files in an existing output set")
    collection_mode.add_argument("--collection-resume", action="store_true",
                                 help="Safely complete or refresh an incomplete deterministic output set")
    output_group.add_argument("--no-database", action="store_true",
                              help="Explicit assertion for --collect-afl-data (the command is always database-free)")

    # 🔹 Backup and restore
    db_group = parser.add_argument_group("Club database tools")
    db_group.add_argument("--import-clubs", action="store_true", help="Persist the canonical club seed to the database")
    db_group.add_argument("--export-clubs", action="store_true", help="Export clubs from DB to backup JSON")

    return parser


def handle_args():
    parser = create_parser()
    args = parser.parse_args()
    if (args.source_status or args.afl_match_id is not None) and not args.collect_match_player_stats:
        parser.error("--source-status and --afl-match-id require --collect-match-player-stats CD_M...")
    collection_options = (args.collection_output is not None or args.collection_round
                          or args.collection_match or args.collection_endpoints
                          or args.collection_overwrite or args.collection_resume or args.no_database)
    if collection_options and not args.collect_afl_data:
        parser.error("collection output/filter options require --collect-afl-data")
    if args.collect_afl_data and args.collection_output is None:
        parser.error("--collect-afl-data requires --collection-output PATH")
    return args

def main():
    args = handle_args()

    if args.collect_afl_data:
        families = (tuple(item.strip() for item in args.collection_endpoints.split(",") if item.strip())
                    if args.collection_endpoints else
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

    elif args.scrape_club:
        club = get_club(args.scrape_club.lower())
        if club:
            save_club_players_to_json(club)
        else:
            log(f"❌ Unknown club: {args.scrape_club}", "ERROR")

    elif args.scrape_clubs:
        scrape_all_clubs(skip_existing=args.skip_existing)

    elif args.enrich_club:
        resolve_players_for_club(args.enrich_club.lower())

    elif args.enrich_clubs:
        enrich_all_clubs(skip_existing=args.skip_existing)

    elif args.scrape_enrich_all:
        scrape_all_clubs(skip_existing=args.skip_existing)
        enrich_all_clubs(skip_existing=args.skip_existing)
        import_players()

    elif args.scrape_injuries:
        scrape_injuries_to_db(print_json=args.print_json)

    elif args.scrape_lineups is not None:
        log(f"🧹 Scraping team lineups for Round {args.scrape_lineups}", "INFO")
        scrape_lineups_to_db(round_number=args.scrape_lineups, print_json=args.print_json)

    elif args.import_clubs:
        log("📥 Importing clubs from JSON to DB...", "INFO")
        import_clubs_to_db()

    elif args.export_clubs:
        log("📤 Exporting clubs from DB to backup JSON...", "INFO")
        from db.import_to_db import export_clubs_from_db
        export_clubs_from_db()

    elif args.scrape_round is not None:
        log(f"📥 Scraping match data for round_id {args.scrape_round}", "INFO")
        scrape_afl_matches.run(round_id=args.scrape_round)

    elif args.scrape_all_rounds:
        log("📥 Scraping all match data from DB rounds...", "INFO")
        scrape_afl_matches.run(round_id=None)

    elif args.scrape_match:
        log(
            f"📊 Explicit legacy player-stat collection for match_id {args.scrape_match}: "
            "source_family=html collector=scraper.scrape_afl_player_stats "
            "persistence_target=player_stats fallback_occurred=false",
            "INFO",
        )
        # This legacy scraper loads club aliases during import. Keep that
        # runtime-data dependency out of argument parsing and unrelated CLI
        # commands, including --help and public metadata collection.
        from scraper import scrape_afl_player_stats
        scrape_afl_player_stats.run_scraper(match_id=args.scrape_match, once=True)

    elif args.collect_match_player_stats:
        conn = get_db_connection()
        try:
            with AflJsonClient() as client:
                reconciliation = reconcile_match_status(
                    conn, client, match_provider_id=args.collect_match_player_stats,
                    afl_match_id=args.afl_match_id,
                )
                resolved_status = later_match_status(
                    reconciliation.resolved_status, args.source_status
                )
                status_resolution = ("explicit" if args.source_status == resolved_status
                                     and args.source_status != reconciliation.resolved_status
                                     else reconciliation.resolution_source)
                with audited_scrape_run(
                    "match_player_stats", target_type="match",
                    target_identifier=args.collect_match_player_stats, conn=conn,
                ) as audit:
                    result = MatchPlayerStatsCollector(
                        client, raw_directory=args.afl_raw_directory
                    ).collect(args.collect_match_player_stats,
                              afl_match_id=reconciliation.afl_match_id,
                              canonical_match_status=resolved_status)
                    # Keep persistence atomic even if a later record fails. The
                    # audit context can then safely record the failed operation
                    # without accidentally committing a partial snapshot.
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
            "source_family": "cfs_json",
            "collector": "MatchPlayerStatsCollector",
            "persistence_target": "cfs_player_stats",
            "fallback_occurred": False,
            "fallback_reason": None,
            "match_provider_id": result.match_provider_id,
            "status": result.status.value,
            "endpoint_source_status": result.endpoint_source_status,
            "stored_canonical_status": reconciliation.stored_status,
            "direct_match_detail_status": reconciliation.direct_status,
            "resolved_match_status": result.resolved_match_status,
            "status_resolution": status_resolution,
            "canonical_match_refreshed": reconciliation.canonical_refreshed,
            "afl_match_id": reconciliation.afl_match_id,
            "collected_at": result.collected_at,
            "source_endpoint": result.source_endpoint,
            "records_collected": len(result.records),
            "rows_written": rows_written,
            "rejected_records": result.rejected_records,
            "diagnostics": [*reconciliation.diagnostics, *result.diagnostics],
            "records": result.records,
        }
        if args.print_json:
            print(json.dumps(output, indent=2, default=_json_default))
        else:
            output.pop("records")
            output["diagnostics"] = [diagnostic.code for diagnostic in output["diagnostics"]]
            print(json.dumps(output, default=_json_default))

    elif args.collect_match_rosters:
        with AflJsonClient() as client:
            result = MatchRosterCollector(
                client, raw_directory=args.afl_raw_directory
            ).collect(args.collect_match_rosters)
        output = {
            "source_family": "cfs_json",
            "collector": "MatchRosterCollector",
            "persistence_target": None,
            "persistence_performed": False,
            "fallback_occurred": False,
            "fallback_reason": None,
            "round_provider_id": result.round_provider_id,
            "status": result.status.value,
            "publication_state": result.publication_state,
            "provider_timestamp": result.provider_timestamp,
            "provider_version": result.provider_version,
            "rosters": result.rosters,
            "selections": result.selections,
        }
        if args.print_json:
            print(json.dumps(output, indent=2))
        else:
            output.pop("rosters")
            output["selections"] = len(result.selections)
            print(json.dumps(output))

    elif args.bootstrap_afl_season:
        with AflJsonClient() as client:
            collector = PublicAflCollector(
                client, raw_directory=args.afl_raw_directory
            )
            result = collector.collect(
                competition_code=args.afl_competition_code,
                competition_provider_id=args.afl_competition_provider_id,
                season=args.bootstrap_afl_season,
            )
            player_result = collector.collect_players(result.season["provider_id"])
        conn = get_db_connection()
        try:
            with audited_scrape_run(
                "afl_metadata_bootstrap", target_type="season",
                target_identifier=args.bootstrap_afl_season, conn=conn,
            ) as audit:
                summary = persist_afl_metadata(conn, result)
                player_summary = persist_player_seasons(
                    conn, player_result, provider_season_id=result.season["provider_id"]
                )
                audit["rows_read"] = summary.records_read + player_summary.records_read
                audit["rows_written"] = (summary.inserted + summary.updated
                                         + player_summary.rows_written)
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

    elif args.collect_afl_metadata:
        with AflJsonClient() as client:
            collector = PublicAflCollector(client, raw_directory=args.afl_raw_directory)
            result = collector.collect(
                competition_code=args.afl_competition_code,
                competition_provider_id=args.afl_competition_provider_id,
                season=args.afl_season,
            )
        output = {
            "competition": result.competition,
            "season": result.season,
            "rounds": result.rounds,
            "teams": result.teams,
            "matches": result.matches,
        }
        if args.print_json:
            print(json.dumps(output, indent=2))
        else:
            print(json.dumps({"competition": result.competition["name"],
                              "season": result.season["name"], "rounds": len(result.rounds),
                              "teams": len(result.teams), "matches": len(result.matches)}))

    else:
        log("❓ No valid argument supplied. Use --help for options.", "WARN")

if __name__ == "__main__":
    main()
