# scraper/scrape_afl_fixtures.py

from bs4 import BeautifulSoup
from utils.http_utils import load_page_with_playwright
from utils.log import log
from utils.afl_urls import get_fixture_url
from db.import_to_db import save_rounds_to_db
from db.connection import get_db_connection


def _parse_int_attribute(element, attribute_name: str) -> int | None:
    value = element.get(attribute_name)
    if value in (None, ""):
        log(f"Missing fixture metadata attribute: {attribute_name}", "WARN")
        return None

    try:
        return int(value)
    except ValueError:
        log(f"Invalid integer for fixture metadata attribute {attribute_name}: {value}", "WARN")
        return None



def parse_fixtures_metadata(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    fixture_div = soup.find("div", class_="js-react-fixtures")

    if not fixture_div:
        log("Could not find fixture metadata div.", "WARN")
        return {}

    metadata = {
        "season_pid": fixture_div.get("data-season-pid"),
        "season_id": _parse_int_attribute(fixture_div, "data-season-id"),
        "competition_id": _parse_int_attribute(fixture_div, "data-competition-id"),
        "default_round_id": _parse_int_attribute(fixture_div, "data-no-filter-round"),
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
        if not round_id:
            log("Skipping round entry without data-round-id.", "WARN")
            continue

        try:
            parsed_round_id = int(round_id)
        except ValueError:
            log(f"Skipping round entry with invalid data-round-id: {round_id}", "WARN")
            continue

        label_button = item.select_one("button")
        round_label = label_button.text.strip() if label_button else "?"
        log(f"Round '{round_label}' — ID: {parsed_round_id}", "DEBUG")
        rounds.append({
            "round_id": parsed_round_id,
            "round_label": round_label
        })

    return rounds


def update_fixture_cache():
    log("Starting AFL fixture scrape...", "INFO")
    url = get_fixture_url()

    html = load_page_with_playwright(url)
    if not html:
        log("Failed to load AFL fixture page.", "ERROR")
        return

    metadata = parse_fixtures_metadata(html)
    rounds = parse_round_list(html)

    if rounds and metadata:
        conn = get_db_connection()
        try:
            save_rounds_to_db(rounds, metadata, conn)
        finally:
            conn.close()
    else:
        log("Skipping DB save due to missing data.", "WARN")


if __name__ == "__main__":
    update_fixture_cache()
