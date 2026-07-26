import json
import argparse
import sqlite3
from pathlib import Path
from utils.log import log
from scraper.scrape_afl_clubs import save_club_players_to_json
from scraper.scrape_afl_injuries import scrape_injury_list, save_injuries_to_db
from scraper.scrape_afl_lineups import scrape_team_lineups
from scraper import scrape_afl_matches
from db.scrape_runs import audited_scrape_run
from merge.helpers import resolve_players_for_club
from utils.club_lookup import load_clubs, get_club
from db.import_to_db import import_players, save_lineups_to_db, save_clubs_to_db
from db.connection import get_db_connection
from afl_json import (
    AflJsonClient, MatchPlayerStatsCollector, MatchRosterCollector, PublicAflCollector,
    resolve_canonical_match_status,
)
from config import DB_PATH


def _json_default(value):
    """Keep precise validated decimals printable by diagnostic commands."""
    from dataclasses import asdict, is_dataclass
    from decimal import Decimal
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

def import_clubs_to_db():
    """Load clubs from JSON and import using shared connection."""
    path = Path("data/clubs.json")
    if not path.exists():
        log("❌ data/clubs.json not found.", "ERROR")
        return

    with path.open("r") as f:
        clubs = json.load(f)

    conn = get_db_connection()
    save_clubs_to_db(conn, clubs)
    conn.commit()
    conn.close()

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
        save_injuries_to_db(data, conn)
        audit["rows_written"] = audit["rows_read"]
        if print_json:
            print(json.dumps(data, indent=2))
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

def handle_args():
    parser = argparse.ArgumentParser(
        description="AFL CLI Tools: Scraping, Enrichment, and Data Management"
    )

    # 🔹 Club-related arguments
    club_group = parser.add_argument_group("Club Tools")
    club_group.add_argument("--scrape-club", metavar="CLUB_NAME", help="Scrape a single club's player list")
    club_group.add_argument("--scrape-clubs", action="store_true", help="Scrape all clubs")
    club_group.add_argument("--enrich-club", metavar="CLUB_NAME", help="Enrich a single club with aliases/codes")
    club_group.add_argument("--enrich-clubs", action="store_true", help="Enrich all clubs")
    club_group.add_argument("--scrape-enrich-all", action="store_true", help="Scrape + enrich all clubs and import to DB")
    club_group.add_argument("--skip-existing", action="store_true", help="(Club-only) Skip if output file already exists")

    # 🔹 Match + fixture scraping
    match_group = parser.add_argument_group("Match + Player Stat Tools")
    match_group.add_argument("--scrape-injuries", action="store_true", help="Scrape AFL injury list")
    match_group.add_argument("--print-json", action="store_true",
                             help="Print full scraped or normalised JSON to stdout (redirect to save it)")
    match_group.add_argument("--scrape-lineups", type=int, metavar="ROUND", help="Scrape team lineups for a round")
    match_group.add_argument("--scrape-round", type=int, metavar="ROUND_ID", help="Scrape AFL matches for a specific round_id (e.g. 1156)")
    match_group.add_argument("--scrape-all-rounds", action="store_true", help="Scrape AFL matches for all rounds in DB")
    match_group.add_argument("--scrape-match", type=int, metavar="MATCH_ID", help="Scrape player stats for a specific match_id")

    metadata_group = parser.add_argument_group("Public AFL Metadata")
    metadata_group.add_argument("--collect-afl-metadata", action="store_true",
                                help="Collect competition, season, rounds, teams and matches without database writes")
    metadata_group.add_argument("--afl-season", help="Select a season by year, AFL ID, provider ID or exact name")
    metadata_group.add_argument("--afl-competition-code", default="AFL", help="Stable Premiership competition code")
    metadata_group.add_argument("--afl-competition-provider-id", default="CD_C014",
                                help="Stable Premiership provider ID")
    metadata_group.add_argument("--afl-raw-directory", type=Path,
                                help="Store original per-endpoint/page API JSON below this directory")
    metadata_group.add_argument("--collect-match-rosters", metavar="ROUND_PROVIDER_ID",
                                help="Collect CFS team selections for a Champion Data round ID")
    metadata_group.add_argument("--collect-match-player-stats", metavar="MATCH_PROVIDER_ID",
                                help="Collect canonical CFS player statistics for a Champion Data match ID")
    metadata_group.add_argument("--source-status",
                                help="Canonical match status fallback for player-stat diagnostics")
    metadata_group.add_argument("--afl-match-id", type=int,
                                help="AFL match ID used to resolve canonical match metadata")

    # 🔹 Backup and restore
    db_group = parser.add_argument_group("Data Backup / Restore")
    db_group.add_argument("--import-clubs", action="store_true", help="Import clubs from JSON file into DB")
    db_group.add_argument("--export-clubs", action="store_true", help="Export clubs from DB to backup JSON")

    return parser.parse_args()

def main():
    args = handle_args()

    if args.scrape_club:
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
        log(f"📊 Scraping player stats for match_id {args.scrape_match}", "INFO")
        # This legacy scraper loads club aliases during import. Keep that
        # runtime-data dependency out of argument parsing and unrelated CLI
        # commands, including --help and public metadata collection.
        from scraper import scrape_afl_player_stats
        scrape_afl_player_stats.run_scraper(match_id=args.scrape_match, once=True)

    elif args.collect_match_player_stats:
        resolved_status = args.source_status
        status_resolution = "explicit" if resolved_status else None
        if resolved_status is None and Path(DB_PATH).is_file():
            with sqlite3.connect(DB_PATH) as metadata_conn:
                resolved_status = resolve_canonical_match_status(
                    metadata_conn, match_provider_id=args.collect_match_player_stats,
                    afl_match_id=args.afl_match_id,
                )
            if resolved_status:
                status_resolution = "canonical_match_database"
        with AflJsonClient() as client:
            result = MatchPlayerStatsCollector(
                client, raw_directory=args.afl_raw_directory
            ).collect(args.collect_match_player_stats, afl_match_id=args.afl_match_id,
                      canonical_match_status=resolved_status)
        output = {
            "match_provider_id": result.match_provider_id,
            "status": result.status.value,
            "endpoint_source_status": result.endpoint_source_status,
            "resolved_match_status": result.resolved_match_status,
            "status_resolution": status_resolution,
            "collected_at": result.collected_at,
            "source_endpoint": result.source_endpoint,
            "rejected_records": result.rejected_records,
            "diagnostics": result.diagnostics,
            "records": result.records,
        }
        if args.print_json:
            print(json.dumps(output, indent=2, default=_json_default))
        else:
            print(json.dumps({"match_provider_id": result.match_provider_id,
                              "status": result.status.value,
                              "records": len(result.records),
                              "diagnostics": len(result.diagnostics)}))

    elif args.collect_match_rosters:
        with AflJsonClient() as client:
            result = MatchRosterCollector(
                client, raw_directory=args.afl_raw_directory
            ).collect(args.collect_match_rosters)
        output = {
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
            print(json.dumps({"round_provider_id": result.round_provider_id,
                              "status": result.status.value,
                              "selections": len(result.selections)}))

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
