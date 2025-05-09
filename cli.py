import json
import argparse
import sqlite3
from pathlib import Path
from utils.log import log
from scraper.club_scraper import save_club_players_to_json
from scraper.scrape_injuries import scrape_injury_list, save_injuries_to_db
from scraper.scrape_afl_lineups import scrape_team_lineups
from merge.helpers import resolve_players_for_club
from utils.club_lookup import load_clubs, get_club
from db.import_to_db import import_players, save_lineups_to_db, import_clubs_to_db

DB_PATH = Path("data/afl_players.db")

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    data = scrape_injury_list(conn)
    save_injuries_to_db(data, conn)
    if print_json:
        print(json.dumps(data, indent=2))
    conn.close()

def scrape_lineups_to_db(round_number: int, print_json: bool = False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    log(f"🧹 Scraping and importing lineups for Round {round_number}", "INFO")
    players = scrape_team_lineups(round_number=round_number)
    save_lineups_to_db(players, conn, round_number)

    if print_json:
        import json
        print(json.dumps(players, indent=2))

    conn.close()


def handle_args():
    parser = argparse.ArgumentParser(description="AFL Club Scraper and Enricher")
    parser.add_argument("--import-clubs", action="store_true", help="Import clubs from data/clubs.json into DB")
    parser.add_argument("--export-clubs", action="store_true", help="Export clubs from DB to data/clubs-bak.json")
    parser.add_argument("--scrape", metavar="club_name", help="Scrape one club")
    parser.add_argument("--scrape_all", action="store_true", help="Run scrape for all clubs")
    parser.add_argument("--enrich", metavar="club_name", help="Enrich one club")
    parser.add_argument("--enrich_all", action="store_true", help="Enrich all clubs")
    parser.add_argument("--all", action="store_true", help="Scrape + enrich all clubs + import to DB")
    parser.add_argument("--skip-existing", action="store_true", help="Skip clubs if output file already exists")
    parser.add_argument("--scrape-injuries", action="store_true", help="Scrape injury list and store to DB")
    parser.add_argument("--print-json", action="store_true", help="Print scraped JSON to stdout")
    parser.add_argument("--scrape-lineups", type=int, metavar="ROUND", help="Scrape team lineups for a given round and import to DB")
    return parser.parse_args()

def main():
    args = handle_args()

    if args.scrape:
        club = get_club(args.scrape.lower())
        if club:
            save_club_players_to_json(club)
        else:
            log(f"[!] Unknown club: {args.scrape}", "ERROR")

    elif args.scrape_all:
        scrape_all_clubs(skip_existing=args.skip_existing)

    elif args.enrich:
        resolve_players_for_club(args.enrich.lower())

    elif args.enrich_all:
        enrich_all_clubs(skip_existing=args.skip_existing)

    elif args.scrape_injuries:
        scrape_injuries_to_db(print_json=args.print_json)

    elif args.all:
        scrape_all_clubs(skip_existing=args.skip_existing)
        enrich_all_clubs(skip_existing=args.skip_existing)
        import_players()
    
    elif args.scrape_lineups is not None:
        round_number = args.scrape_lineups
        log(f"🧹 Starting scrape for Round {round_number}", "INFO")
        scrape_lineups_to_db(round_number=round_number, print_json=args.print_json)

    elif args.import_clubs:
        log("📥 Importing clubs from JSON to DB...", "INFO")
        import_clubs_to_db()

    elif args.export_clubs:
        log("📤 Exporting clubs from DB to backup JSON...", "INFO")
        from db.import_to_db import export_clubs_from_db
        export_clubs_from_db()

    else:
        log("❓ No valid argument supplied. Use --help for options.", "WARN")

if __name__ == "__main__":
    main()
