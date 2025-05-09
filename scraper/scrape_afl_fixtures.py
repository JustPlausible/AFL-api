# scraper/scrape_afl_fixtures.py

from bs4 import BeautifulSoup
import sqlite3
from utils.http_utils import load_page_with_playwright
from utils.log import log
from utils.afl_urls import get_fixture_url
from db.import_to_db import save_rounds_to_db
import config


def parse_fixtures_metadata(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    fixture_div = soup.find("div", class_="js-react-fixtures")

    if not fixture_div:
        log("Could not find fixture metadata div.", "WARN")
        return {}

    metadata = {
        "season_pid": fixture_div.get("data-season-pid"),
        "season_id": int(fixture_div.get("data-season-id")),
        "competition_id": int(fixture_div.get("data-competition-id")),
        "default_round_id": int(fixture_div.get("data-no-filter-round")),
        "special_round": fixture_div.get("data-special-round"),
    }

    log("Fixture Season Metadata:", "INFO")
    for key, value in metadata.items():
        log(f"{key}: {value}", "DEBUG")
    return metadata


def parse_round_list(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    round_items = soup.select("ul.competition-nav__round-list > li")

    if not round_items:
        log("No rounds found in round list.", "WARN")
        return []

    log("Available Rounds:", "INFO")
    rounds = []
    for item in round_items:
        round_id = item.get("data-round-id")
        label_button = item.select_one("button")
        round_label = label_button.text.strip() if label_button else "?"
        log(f"Round '{round_label}' — ID: {round_id}", "DEBUG")
        rounds.append({
            "round_id": int(round_id),
            "round_label": round_label
        })

    return rounds


def run():
    log("Starting AFL fixture scrape...", "INFO")
    url = get_fixture_url()

    html = load_page_with_playwright(url)
    if not html:
        log("Failed to load AFL fixture page.", "ERROR")
        return

    metadata = parse_fixtures_metadata(html)
    rounds = parse_round_list(html)

    if rounds and metadata:
        conn = sqlite3.connect("data/afl_players.db")
        save_rounds_to_db(rounds, metadata, conn)
        conn.close()
    else:
        log("Skipping DB save due to missing data.", "WARN")


if __name__ == "__main__":
    run()
