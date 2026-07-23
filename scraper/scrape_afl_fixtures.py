# scraper/scrape_afl_fixtures.py

from bs4 import BeautifulSoup
import sqlite3
from utils.http_utils import load_page_with_playwright
from utils.log import log
from utils.afl_urls import get_fixture_url
from db.import_to_db import save_rounds_to_db
from db.scrape_runs import audited_scrape_run
import config
from scraper.afl_selectors import FIXTURE_SELECTORS


def parse_fixtures_metadata(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    fixture_div = soup.find("div", class_=FIXTURE_SELECTORS.METADATA_ROOT_CLASS)

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
    round_items = soup.select(FIXTURE_SELECTORS.ROUND_LIST_ITEMS)

    if not round_items:
        log("No rounds found in round list.", "WARN")
        return []

    log("Available Rounds:", "INFO")
    rounds = []
    for item in round_items:
        round_id = item.get("data-round-id")
        label_button = item.select_one(FIXTURE_SELECTORS.ROUND_LABEL_BUTTON)
        round_label = label_button.text.strip() if label_button else "?"
        log(f"Round '{round_label}' — ID: {round_id}", "DEBUG")
        rounds.append({
            "round_id": int(round_id),
            "round_label": round_label
        })

    return rounds


def update_fixture_cache(trigger_source: str | None = None, correlation_id: str | None = None):
    with audited_scrape_run("fixture", target_type="fixture_index", trigger_source=trigger_source, correlation_id=correlation_id) as audit:
        return _update_fixture_cache(audit)

def _update_fixture_cache(audit=None):
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
        if audit is not None:
            audit["rows_read"] = len(rounds)
            audit["rows_written"] = len(rounds)
        conn.close()
    else:
        log("Skipping DB save due to missing data.", "WARN")


if __name__ == "__main__":
    update_fixture_cache()
