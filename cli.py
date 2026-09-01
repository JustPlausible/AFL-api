import argparse
import re
import sys
from pathlib import Path
from version import __version__

# Keep the conventional version probe independent of the operational CLI's
# scraper, browser, configuration, and database imports below.  The argparse
# action remains registered as well so generated help documents the surface.
if __name__ == "__main__" and sys.argv[1:] == ["--version"]:
    print(__version__)
    raise SystemExit(0)

from config import AFL_COMPETITION_CODE, AFL_COMPETITION_PROVIDER_ID, AFL_SEASON_YEAR

OPERATION_FLAGS = {
    "version": "--version",
    "scrape_club": "--scrape-club",
    "scrape_clubs": "--scrape-clubs",
    "enrich_club": "--enrich-club",
    "enrich_clubs": "--enrich-clubs",
    "scrape_enrich_all": "--scrape-enrich-all",
    "scrape_injuries": "--scrape-injuries",
    "scrape_lineups": "--scrape-lineups",
    "scrape_round": "--scrape-round",
    "scrape_all_rounds": "--scrape-all-rounds",
    "scrape_match": "--scrape-match",
    "collect_afl_metadata": "--collect-afl-metadata",
    "collect_afl_data": "--collect-afl-data",
    "bootstrap_afl_season": "--bootstrap-afl-season",
    "sync_afl_season": "--sync-afl-season",
    "sync_match_rosters": "--sync-match-rosters",
    "report_afl_season": "--report-afl-season",
    "report_stats_absence_candidates": "--report-stats-absence-candidates",
    "review_stats_not_expected": "--review-stats-not-expected",
    "revoke_stats_not_expected": "--revoke-stats-not-expected",
    "collect_match_rosters": "--collect-match-rosters",
    "collect_match_player_stats": "--collect-match-player-stats",
    "collect_statspro_season": "--collect-statspro-season",
    "collect_statspro_round": "--collect-statspro-round",
    "build_player_stat_summaries": "--build-player-stat-summaries",
    "import_player_movements": "--import-player-movements",
    "import_clubs": "--import-clubs",
    "export_clubs": "--export-clubs",
    "add_api_key": "--add-api-key",
    "list_api_keys": "--list-api-keys",
    "remove_api_key": "--remove-api-key",
    "grant_api_key_capability": "--grant-api-key-capability",
    "revoke_api_key_capability": "--revoke-api-key-capability",
}

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


def non_negative_round(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("round must be zero or greater")
    return number


def positive_match_id(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("match ID must be a positive integer")
    return number


def _add_operation_argument(container, destination, **kwargs):
    """Register an operation using its authoritative public flag name."""
    action = container.add_argument(
        OPERATION_FLAGS[destination], dest=destination, **kwargs
    )
    action.is_top_level_operation = True
    return action

def create_parser() -> argparse.ArgumentParser:
    """Build the flag-based CLI parser without loading runtime scraper data."""
    parser = argparse.ArgumentParser(
        description=("AFL operator CLI: preferred AFL/CFS JSON collection and "
                     "explicit legacy HTML tools"),
        epilog=("Season sync exits: 0 = all currently actionable matches collected or already "
                "complete (scheduled, live/postgame, and recognised future placeholders may "
                "be safely skipped); 1 = requested or actionable work incomplete or failed "
                "(unavailable, empty, partial, rejected, unknown, missing-provider, or "
                "unsatisfied explicit/bounded selection); 2 = invalid CLI usage or argument "
                "combination."),
    )
    _add_operation_argument(parser, "version", action="store_true",
                            help="Print the AFL-api version and exit")

    # 🔹 Club-related arguments
    club_group = parser.add_argument_group("Club import, export, HTML scrape, and enrichment")
    _add_operation_argument(club_group, "scrape_club", metavar="CLUB_NAME", help="Legacy HTML: scrape one club's players to a raw JSON file")
    _add_operation_argument(club_group, "scrape_clubs", action="store_true", help="Legacy HTML: scrape all clubs to raw JSON files")
    _add_operation_argument(club_group, "enrich_club", metavar="CLUB_NAME", help="Enrich one existing raw club JSON file locally")
    _add_operation_argument(club_group, "enrich_clubs", action="store_true", help="Enrich all existing raw club JSON files locally")
    _add_operation_argument(club_group, "scrape_enrich_all", action="store_true", help="Legacy HTML: scrape and enrich every club, then persist players")
    club_group.add_argument("--skip-existing", action="store_true", help="(Club-only) Skip if output file already exists")

    # 🔹 Match + fixture scraping
    match_group = parser.add_argument_group("Explicit legacy AFL HTML collection")
    _add_operation_argument(match_group, "scrape_injuries", action="store_true", help="Legacy HTML: collect the AFL injury list and persist injuries")
    _add_operation_argument(match_group, "scrape_lineups", type=int, metavar="ROUND", help="Explicit legacy HTML lineup scrape (persists legacy lineup tables)")
    _add_operation_argument(match_group, "scrape_round", type=int, metavar="ROUND_ID", help="Explicit legacy HTML match scrape for a round_id")
    _add_operation_argument(match_group, "scrape_all_rounds", action="store_true", help="Explicit legacy HTML match scrape for all database rounds")
    _add_operation_argument(match_group, "scrape_match", type=int, metavar="MATCH_ID", help="Explicit legacy HTML player-stat scrape; persists to player_stats (not fallback or dual-write)")

    metadata_group = parser.add_argument_group("Preferred AFL public JSON")
    _add_operation_argument(metadata_group, "collect_afl_metadata", action="store_true",
                                help="Collect competition, season, rounds, teams and matches without database writes")
    _add_operation_argument(metadata_group, "collect_afl_data", action="store_true",
                                help="Run the full modular JSON pipeline to files only (never writes the database)")
    _add_operation_argument(metadata_group, "bootstrap_afl_season", metavar="SEASON",
                                help="Persist AFL metadata plus CFS players and season membership")
    _add_operation_argument(metadata_group, "sync_afl_season", metavar="SEASON",
                                help="Bootstrap and synchronise concluded CFS match statistics for a season")
    _add_operation_argument(metadata_group, "sync_match_rosters", type=int, metavar="YEAR",
                                help="Persist canonical CFS rosters for selected rounds of an existing season")
    _add_operation_argument(metadata_group, "report_afl_season", type=int, metavar="YEAR",
                                help="Read-only completeness report for a persisted canonical AFL season")
    _add_operation_argument(metadata_group, "report_stats_absence_candidates", type=int, metavar="YEAR",
                            help="Report concluded matches with suspiciously absent authoritative statistics")
    review_group = parser.add_argument_group("Human-reviewed match dataset dispositions")
    _add_operation_argument(review_group, "review_stats_not_expected", type=positive_match_id,
                            metavar="AFL_MATCH_ID", help="Create or update a reviewed stats-not-expected disposition")
    _add_operation_argument(review_group, "revoke_stats_not_expected", type=positive_match_id,
                            metavar="AFL_MATCH_ID", help="Revoke a reviewed stats-not-expected disposition")
    review_group.add_argument("--reason-code", choices=("abandoned", "cancelled", "forfeit",
                              "not_played", "historical_data_unavailable",
                              "provider_data_unavailable", "other"))
    review_group.add_argument("--display-reason")
    review_group.add_argument("--evidence-url")
    review_group.add_argument("--evidence-note")
    review_group.add_argument("--reviewed-by", default="cli-operator")
    metadata_group.add_argument("--afl-season", default=AFL_SEASON_YEAR, metavar="SEASON",
                                help="Select a season by year, AFL ID, provider ID or exact name")
    metadata_group.add_argument("--afl-competition-code", default=AFL_COMPETITION_CODE, metavar="CODE",
                                help="Stable Premiership competition code")
    metadata_group.add_argument("--afl-competition-provider-id", default=AFL_COMPETITION_PROVIDER_ID, metavar="PROVIDER_ID",
                                help="Stable Premiership provider ID")
    cfs_group = parser.add_argument_group("Preferred Champion Data/CFS JSON")
    _add_operation_argument(cfs_group, "collect_match_rosters", metavar="ROUND_PROVIDER_ID",
                                type=cfs_round_provider_id,
                                help="Collect CFS selections read-only; requires CD_R... (example: CD_R202601421)")
    _add_operation_argument(cfs_group, "collect_match_player_stats", metavar="MATCH_PROVIDER_ID",
                                type=cfs_match_provider_id,
                                help="Collect CFS stats into cfs_player_stats; requires CD_M... (example: CD_M20260142001)")
    _add_operation_argument(cfs_group, "collect_statspro_season", type=int, metavar="YEAR",
                            help="Collect and persist finals-inclusive StatsPro SEASON_TOTAL for a persisted season")
    _add_operation_argument(cfs_group, "collect_statspro_round", type=cfs_round_provider_id,
                            metavar="ROUND_PROVIDER_ID",
                            help="Collect and persist StatsPro LEAGUE_ROUND_TOTAL for a persisted round")
    _add_operation_argument(cfs_group, "build_player_stat_summaries", type=int, metavar="YEAR",
                            help="Build finalized local player summaries for a persisted season")
    movement_group = parser.add_argument_group("Supplemental AFL editorial player movements")
    _add_operation_argument(movement_group, "import_player_movements", type=int, metavar="PREVIOUS_SEASON", help="Import supplemental retirements/delistings/trades evidence")
    movement_group.add_argument("--movement-source-file", type=Path)
    movement_group.add_argument("--movement-source-url")
    movement_group.add_argument("--source-archived-at")
    cfs_group.add_argument("--scope", choices=("home_and_away",),
                           help="With --build-player-stat-summaries: required summary scope")
    cfs_group.add_argument("--source-status", metavar="STATUS",
                           help="With --collect-match-player-stats: explicit canonical status fallback")
    cfs_group.add_argument("--afl-match-id", type=int, metavar="AFL_MATCH_ID",
                           help="With --collect-match-player-stats: numeric AFL ID for canonical status resolution")
    sync_group = parser.add_argument_group("Whole-season persistent synchronisation")
    sync_group.add_argument("--round", type=non_negative_round, metavar="ROUND",
                            help="With --sync-afl-season or --sync-match-rosters: process one round")
    sync_group.add_argument("--round-from", type=non_negative_round, metavar="ROUND",
                            help="With a season sync command: first round in an inclusive range")
    sync_group.add_argument("--round-to", type=non_negative_round, metavar="ROUND",
                            help="With a season sync command: last round in an inclusive range")
    sync_group.add_argument("--match-id", type=positive_match_id, action="append", default=[], metavar="AFL_MATCH_ID",
                            help=("With --sync-afl-season: process a canonical AFL match ID "
                                  "(repeatable; intersects round filters)"))
    sync_group.add_argument("--refresh-complete", action="store_true",
                            help="With --sync-afl-season: reconsider concluded authoritative snapshots")

    output_group = parser.add_argument_group("Output and JSON diagnostics")
    output_group.add_argument("--print-json", action="store_true",
                              help=("With --sync-afl-season or --sync-match-rosters: emit the complete machine readable result "
                                    "including match details; with --bootstrap-afl-season: emit the full "
                                    "structured bootstrap result instead of the default concise human summary; "
                                    "with --report-afl-season emit the complete structured report; "
                                    "persistence is unchanged"))
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
    _add_operation_argument(db_group, "import_clubs", action="store_true", help="Persist the canonical club seed to the database")
    _add_operation_argument(db_group, "export_clubs", action="store_true", help="Export clubs from DB to backup JSON")

    # 🔹 API key management
    api_key_group = parser.add_argument_group("API key management")
    _add_operation_argument(api_key_group, "add_api_key", metavar="LABEL",
                            help="Add a new API key for LABEL and display the full key once; it is stored only as a hash")
    _add_operation_argument(api_key_group, "list_api_keys", action="store_true",
                            help="List API key labels, prefixes, and active status; full keys are never shown")
    _add_operation_argument(api_key_group, "remove_api_key", metavar="KEY_OR_LABEL",
                            help="Remove an API key by its presented full key or its label")
    _add_operation_argument(api_key_group, "grant_api_key_capability", nargs=2,
                            metavar=("LABEL", "CAPABILITY"),
                            help="Grant a supported read capability to the API key LABEL")
    _add_operation_argument(api_key_group, "revoke_api_key_capability", nargs=2,
                            metavar=("LABEL", "CAPABILITY"),
                            help="Revoke a supported read capability from the API key LABEL")

    return parser


def selected_operation_flags(args):
    """Return selected operations in the authoritative, stable display order."""
    return [
        flag for destination, flag in OPERATION_FLAGS.items()
        if getattr(args, destination) is not None and getattr(args, destination) is not False
    ]


def handle_args(argv=None):
    parser = create_parser()
    args = parser.parse_args(argv)
    selected = selected_operation_flags(args)
    if len(selected) > 1:
        parser.error(
            "Only one operation may be selected per invocation.\n"
            f"Conflicting operations: {', '.join(selected)}"
        )
    if (args.source_status or args.afl_match_id is not None) and not args.collect_match_player_stats:
        parser.error("--source-status and --afl-match-id require --collect-match-player-stats CD_M...")
    if args.build_player_stat_summaries and args.scope != "home_and_away":
        parser.error("--build-player-stat-summaries requires --scope home_and_away")
    if args.scope and not args.build_player_stat_summaries:
        parser.error("--scope requires --build-player-stat-summaries")
    if args.import_player_movements and not (args.movement_source_file or args.movement_source_url):
        parser.error("--import-player-movements requires --movement-source-file or --movement-source-url")
    if (args.movement_source_file or args.movement_source_url or args.source_archived_at) and not args.import_player_movements:
        parser.error("movement source options require --import-player-movements")
    collection_options = (args.collection_output is not None or args.collection_round
                          or args.collection_match or args.collection_endpoints
                          or args.collection_overwrite or args.collection_resume or args.no_database)
    if collection_options and not args.collect_afl_data:
        parser.error("collection output/filter options require --collect-afl-data")
    if args.collect_afl_data and args.collection_output is None:
        parser.error("--collect-afl-data requires --collection-output PATH")
    round_options = args.round is not None or args.round_from is not None or args.round_to is not None
    if round_options and not (args.sync_afl_season or args.sync_match_rosters):
        parser.error("--round and --round-from/--round-to require --sync-afl-season or --sync-match-rosters")
    if (args.match_id or args.refresh_complete) and not args.sync_afl_season:
        parser.error("--match-id and --refresh-complete require --sync-afl-season")
    if args.round is not None and (args.round_from is not None or args.round_to is not None):
        parser.error("--round cannot be combined with --round-from or --round-to")
    if (args.round_from is None) != (args.round_to is None):
        parser.error("--round-from and --round-to must be supplied together")
    if args.round_from is not None and args.round_from > args.round_to:
        parser.error("--round-from cannot be greater than --round-to")
    review_options = any((args.reason_code, args.display_reason, args.evidence_url,
                          args.evidence_note))
    if args.review_stats_not_expected and (not args.reason_code or not args.display_reason):
        parser.error("--review-stats-not-expected requires --reason-code and --display-reason")
    if review_options and not args.review_stats_not_expected:
        parser.error("review evidence options require --review-stats-not-expected")
    args.match_id = list(dict.fromkeys(args.match_id))
    return args

def main(argv=None):
    args = handle_args(argv)

    if args.version:
        print(__version__)
        return

    selected = selected_operation_flags(args)
    # Preserve the legacy truthiness-based dispatch behavior for this numeric
    # operation: argparse accepts 0, but it historically fell through to the
    # no-operation warning rather than invoking the match scraper.
    if not selected or args.scrape_match == 0:
        from utils.log import log
        log("❓ No valid argument supplied. Use --help for options.", "WARN")
        return

    # This is the runtime boundary: parsing and all pure validation have
    # completed, and exactly one operation is ready to dispatch.
    from cli_runtime import dispatch
    dispatch(args)

if __name__ == "__main__":
    main()
