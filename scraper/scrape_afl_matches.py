# scraper/scrape_afl_matches.py
import sqlite3
import sys
from bs4 import BeautifulSoup
import re
from utils.http_utils import load_page_with_playwright
from utils.log import log
from utils.afl_urls import get_fixture_url_for_round
from utils.club_lookup import load_clubs, resolve_club_code
from db.import_to_db import save_matches_to_db
from utils.match_time import parse_match_time
import config

club_lookup = {c["name"].lower(): c["code"] for c in load_clubs()}

def extract_season_year(html: str) -> int | None:
    from bs4 import BeautifulSoup
    import re

    log("🔍 Parsing fixture page for season year...", "DEBUG")
    soup = BeautifulSoup(html, "html.parser")

    label = soup.select_one("div.competition-nav__season-select .select__current-text")
    if not label:
        log("❌ Could not find season label in fixture page", "ERROR")
        return None

    log(f"🧾 Found label text: '{label.text.strip()}'", "DEBUG")

    match = re.search(r"(20\d{2})", label.text)
    if match:
        year = int(match.group(1))
        log(f"📅 Detected season year: {year}", "INFO")
        return year

    log("⚠️ Could not extract year using regex from label", "WARN")
    return None

def extract_match_data(div, season_year):
    home_name = div.select_one(".fixtures__match-team--home span").text.strip()
    away_name = div.select_one(".fixtures__match-team--away span").text.strip()

    home_code = resolve_club_code(home_name)
    away_code = resolve_club_code(away_name)

    match = {
        "match_id": int(div.get("data-match-id")),
        "match_provider_id": div.get("data-match-provider-id"),
        "round_id": int(div.get("data-round-id")),
        "status": div.get("data-match-status"),
        "home_team": home_code,
        "away_team": away_code,
        "venue": div.select_one(".fixtures__match-venue").text.strip(),
        "start_time_utc": None,
        "score_home": None,
        "score_away": None
    }

    # Try to extract scheduled match time from aria-label
    details_link = div.select_one("a.fixtures__absolute-link")
    if details_link and details_link.has_attr("aria-label"):
        label = details_link["aria-label"]
        match_time_match = re.search(
            r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
            r"([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?\s+\d{4}),\s+"
            r"(\d{1,2}:\d{2}\s*[ap]m\s*[A-Z]+)", label
        )
        if match_time_match:
            date_part = match_time_match.group(1)
            # Remove ordinal suffixes
            date_part = re.sub(r"(st|nd|rd|th)", "", date_part)
            time_part = match_time_match.group(2).upper().replace(" ", "")
            match["start_time_utc"] = parse_match_time(date_part, time_part)

    # Extract score if match is completed
    score_divs = div.select(".fixtures__match-score-total")
    if len(score_divs) == 2:
        try:
            match["score_home"] = int(score_divs[0].text.strip())
            match["score_away"] = int(score_divs[1].text.strip())
        except ValueError:
            pass

    return match

def scrape_round(round_id: int, conn: sqlite3.Connection):
    url = get_fixture_url_for_round(round_id)
    log(f"🔄 Scraping round {round_id} from {url}", "INFO")

    html = load_page_with_playwright(url)
    if not html:
        log(f"❌ Failed to load page for round {round_id}", "ERROR")
        return

    soup = BeautifulSoup(html, "html.parser")
    content = soup.select("h2.fixtures__date-header, div.fixtures__item")

    current_date = None
    matches = []
    season_year = extract_season_year(html)

    for element in content:
        if element.name == "h2" and "fixtures__date-header" in element.get("class", []):
            current_date = element.get_text(strip=True)
        elif element.name == "div" and "fixtures__item" in element.get("class", []):
            match = extract_match_data(element, season_year)
            matches.append(match)

    if matches:
        save_matches_to_db(matches, conn)
    else:
        log(f"⚠️ No matches found for round {round_id}", "WARN")


def run(round_id: int | None = None):
    conn = sqlite3.connect("data/afl_players.db")

    if round_id:
        scrape_round(round_id, conn)
    else:
        log("🔁 No round ID provided, scraping all rounds from DB...", "INFO")
        cursor = conn.cursor()
        cursor.execute("SELECT round_id FROM rounds ORDER BY round_id ASC")
        for (rid,) in cursor.fetchall():
            scrape_round(rid, conn)

    conn.close()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    run(int(arg) if arg and arg.isdigit() else None)
