import json
from pathlib import Path
from utils.log import log
import argparse
import sqlite3
from scraper.club_scraper import save_club_players_to_json
from scraper.scrape_injuries import scrape_injury_list, save_injuries_to_db
from merge.helpers import resolve_players_for_club
from utils.club_lookup import load_clubs, get_club

def scrape_club(club_name):
    club = get_club(club_name)
    if not club:
        log(f"[!] Unknown club: {club_name}", "ERROR")
        return
    save_club_players_to_json(club)

def enrich_club(club_name):
    resolve_players_for_club(club_name)

def scrape_all(skip_existing=False):
    clubs = load_clubs()
    for club in clubs:
        save_club_players_to_json(club, skip_existing=skip_existing)

def enrich_all(skip_existing=False):
    data_dir = Path("data")
    raw_files = data_dir.glob("players-*-raw.json")
    for path in sorted(raw_files):
        club_name = path.stem.replace("players-", "").replace("-raw", "")
        resolve_players_for_club(club_name)

def main():
    parser = argparse.ArgumentParser(description="AFL Club Scraper and Enricher")
    parser.add_argument("--scrape", help="Scrape one club", metavar="club_name")
    parser.add_argument("--enrich", help="Enrich one club", metavar="club_name")
    parser.add_argument("--all", help="Run scrape + enrich for all clubs", action="store_true")
    parser.add_argument("--skip-existing", help="Skip clubs if output file already exists", action="store_true")
    parser.add_argument("--scrape-injuries", help="Run the injury scraper and store data to DB", action="store_true")
    parser.add_argument("--print-json", help="Print scraped JSON to stdout", action="store_true")

    args = parser.parse_args()

    if args.scrape:
        scrape_club(args.scrape.lower())
    elif args.enrich:
        enrich_club(args.enrich.lower())
    elif args.all:
        scrape_all(skip_existing=args.skip_existing)
        enrich_all(skip_existing=args.skip_existing)
    elif args.scrape_injuries:
        conn = sqlite3.connect("data/afl_players.db")
        conn.row_factory = sqlite3.Row
        data = scrape_injury_list(conn)
        save_injuries_to_db(data, conn)
        if args.print_json:
            print(json.dumps(data, indent=2))
        conn.close()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()