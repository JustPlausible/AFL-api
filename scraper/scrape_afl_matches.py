# scraper/scrape_afl_matches.py
import sqlite3
import sys
from bs4 import BeautifulSoup
from utils.http_utils import load_page_with_playwright
from utils.log import log
from utils.afl_urls import get_fixture_url_for_round
from utils.club_lookup import load_clubs, resolve_club_code
from db.import_to_db import save_matches_to_db
import config

club_lookup = {c["name"].lower(): c["code"] for c in load_clubs()}

def extract_match_data(div, current_date):
    home_name = div.select_one(".fixtures__match-team--home span").text.strip()
    away_name = div.select_one(".fixtures__match-team--away span").text.strip()

    home_code = resolve_club_code(home_name)
    away_code = resolve_club_code(away_name)

    match = {
        "match_id": int(div.get("data-match-id")),
        "match_provider_id": div.get("data-match-provider-id"),
        "round_id": int(div.get("data-round-id")),
        "status": div.get("data-match-status"),
        "match_date_label": current_date,
        "home_team": home_code,
        "away_team": away_code,
        "venue": div.select_one(".fixtures__match-venue").text.strip(),
        "start_time_text": "",  # optional fallback for upcoming
        "score_home": None,
        "score_away": None
    }

    # Optional: try to extract score if match is completed
    score_divs = div.select(".fixtures__match-score-total")
    if len(score_divs) == 2:
        try:
            match["score_home"] = int(score_divs[0].text.strip())
            match["score_away"] = int(score_divs[1].text.strip())
        except ValueError:
            pass

    # Optional: extract scheduled start time for UPCOMING
    if match["status"] == "UPCOMING":
        time_div = div.select_one(".fixtures__status-label > div")
        if time_div:
            match["start_time_text"] = time_div.get_text(strip=True)

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

    for element in content:
        if element.name == "h2" and "fixtures__date-header" in element.get("class", []):
            current_date = element.get_text(strip=True)
        elif element.name == "div" and "fixtures__item" in element.get("class", []):
            match = extract_match_data(element, current_date)
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
