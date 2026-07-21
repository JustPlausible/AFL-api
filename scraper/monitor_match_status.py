# scraper/monitor_match_status.py

import time
import random
import sqlite3
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup
from utils.http_utils import load_page_with_playwright
from utils.afl_urls import get_fixture_url_for_round
from utils.club_lookup import resolve_club_code
from utils.log import log
from scraper.afl_selectors import MATCH_CARD_SELECTORS

MATCH_ID_TO_TRACK = 7041
ROUND_ID = 1155
LOG_FILE = Path("logs/match_7041_status.log")


def extract_status_for_match(html: str, match_id: int) -> tuple[str, str]:
    """Return (status, label) for the specified match ID."""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select(MATCH_CARD_SELECTORS.DATE_HEADER_OR_MATCH_CARD)

    current_date = None
    for element in content:
        if element.name == "h2" and MATCH_CARD_SELECTORS.DATE_HEADER_CLASS in element.get("class", []):
            current_date = element.get_text(strip=True)

        elif element.name == "div" and MATCH_CARD_SELECTORS.MATCH_CARD_CLASS in element.get("class", []):
            if int(element.get("data-match-id", 0)) != match_id:
                continue

            status = element.get("data-match-status")
            label = ""

            if status == "LIVE":
                # Look for the quarter and match clock
                time_container = element.select_one(MATCH_CARD_SELECTORS.MATCH_TIME)
                if time_container:
                    raw_quarter = time_container.contents[0].strip()  # e.g. 'Q3'
                    clock = time_container.select_one(MATCH_CARD_SELECTORS.LIVE_CLOCK)
                    match_clock = clock.text.strip() if clock else ""
                    label = f"{raw_quarter} {match_clock}".strip()
            else:
                # Fallback to generic label
                label_div = element.select_one(MATCH_CARD_SELECTORS.STATUS_LABEL)
                label = label_div.get_text(strip=True) if label_div else ""

            return status, label

    return "UNKNOWN", "Not Found"

def append_status_log(status: str, label: str):
    now = datetime.now().isoformat()
    with LOG_FILE.open("a") as f:
        f.write(f"[{now}] Status: {status} — Label: {label}\n")
    log(f"📌 [{now}] {status} — {label}", "INFO")


def monitor():
    log(f"🕵️ Monitoring match {MATCH_ID_TO_TRACK} in round {ROUND_ID}...", "INFO")
    previous_status = None
    previous_label = None

    while True:
        try:
            url = get_fixture_url_for_round(ROUND_ID)
            html = load_page_with_playwright(url)

            if not html:
                log("❌ Failed to load fixture page.", "ERROR")
            else:
                status, label = extract_status_for_match(html, MATCH_ID_TO_TRACK)

            if status != previous_status or label != previous_label:
                append_status_log(status, label)
                previous_status = status
                previous_label = label

            if status == "COMPLETED":
                log("✅ Match is now completed. Monitoring stopped.", "SUCCESS")
                break

        except Exception as e:
            log(f"💥 Unexpected error: {e}", "ERROR")

        wait_time = random.randint(120, 240)
        log(f"⏱ Sleeping for {wait_time} seconds...", "DEBUG")
        time.sleep(wait_time)

if __name__ == "__main__":
    monitor()
