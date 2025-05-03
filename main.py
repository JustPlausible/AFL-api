import json
from fastapi import FastAPI
from pathlib import Path
from utils.log import log

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "AFL Supplemental API up and running!"}

@app.get("/players")
def get_players():
    path = Path("data/players.json")
    with path.open("r") as f:
        return json.load(f)
    
import argparse
from scraper.club_scraper import save_club_players_to_json
from merge.helpers import resolve_players_for_club

def load_club_urls():
    with open("data/clubs.json") as f:
        return json.load(f)

def scrape_club(club_name):
    club_urls = load_club_urls()
    if club_name not in club_urls:
        log(f"[!] Unknown club: {club_name}", "ERROR")
        return
    url = club_urls[club_name]
    save_club_players_to_json(club_name, url)

def enrich_club(club_name):
    resolve_players_for_club(club_name)

def scrape_all(skip_existing=False):
    club_urls = load_club_urls()
    for club_name, url in club_urls.items():
        save_club_players_to_json(club_name, url)

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


    args = parser.parse_args()

    if args.scrape:
        scrape_club(args.scrape.lower())
    elif args.enrich:
        enrich_club(args.enrich.lower())
    elif args.all:
        scrape_all(skip_existing=args.skip_existing)
        enrich_all(skip_existing=args.skip_existing)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
