# scraper/scrape_afl_matches.py
import sqlite3
import sys
from bs4 import BeautifulSoup
import re
from utils.http_utils import load_page_with_playwright
from utils.log import setup_logger
from utils.afl_urls import get_fixture_url_for_round
from utils.club_lookup import resolve_club_code
from db.import_to_db import save_matches_to_db
from utils.match_time import parse_match_time
import config

log = setup_logger("match_scraper", "scrape_afl_matches.log")


def load_existing_matches(conn: sqlite3.Connection) -> dict[int, dict]:
    cursor = conn.cursor()
    cursor.execute("SELECT match_id, start_time_utc, status FROM matches")
    existing = {}
    for match_id, start_time, status in cursor.fetchall():
        existing[match_id] = {
            "start_time_utc": start_time,
            "status": status,
        }
    return existing


def extract_season_year(html: str) -> int | None:
    from bs4 import BeautifulSoup
    import re

    log.debug("🔍 Parsing fixture page for season year...")
    soup = BeautifulSoup(html, "html.parser")

    label = soup.select_one("div.competition-nav__season-select .select__current-text")
    if not label:
        log.error("❌ Could not find season label in fixture page")
        return None

    log.debug(f"🧾 Found label text: '{label.text.strip()}'")

    match = re.search(r"(20\d{2})", label.text)
    if match:
        year = int(match.group(1))
        log.info(f"📅 Detected season year: {year}")
        return year

    log.warning("⚠️ Could not extract year using regex from label")
    return None


class FixtureParseError(ValueError):
    """Raised when fixture HTML is missing selectors required by the parser."""


def _required_text(element, selector: str, field_name: str) -> str:
    selected = element.select_one(selector)
    if selected is None:
        match_id = element.get("data-match-id", "unknown")
        raise FixtureParseError(
            f"Match {match_id} is missing required field '{field_name}' using selector '{selector}'"
        )
    text = selected.get_text(strip=True)
    if not text:
        match_id = element.get("data-match-id", "unknown")
        raise FixtureParseError(
            f"Match {match_id} has empty required field '{field_name}' using selector '{selector}'"
        )
    return text


def _required_int_attr(element, attr_name: str, field_name: str) -> int:
    value = element.get(attr_name)
    if value in (None, ""):
        raise FixtureParseError(
            f"Fixture match item is missing required attribute '{attr_name}' for field '{field_name}'"
        )
    try:
        return int(value)
    except ValueError as exc:
        raise FixtureParseError(
            f"Fixture match item has invalid integer attribute '{attr_name}' for field '{field_name}': {value!r}"
        ) from exc


def extract_match_data(div, season_year, existing_match=None):
    home_name = _required_text(div, ".fixtures__match-team--home span", "home_team")
    away_name = _required_text(div, ".fixtures__match-team--away span", "away_team")

    home_code = resolve_club_code(home_name)
    away_code = resolve_club_code(away_name)

    match = {
        "match_id": _required_int_attr(div, "data-match-id", "match_id"),
        "match_provider_id": div.get("data-match-provider-id"),
        "round_id": _required_int_attr(div, "data-round-id", "round_id"),
        "status": div.get("data-match-status"),
        "home_team": home_code,
        "away_team": away_code,
        "venue": _required_text(div, ".fixtures__match-venue", "venue"),
        "start_time_utc": None,
        "score_home": None,
        "score_away": None,
        "match_time_label": None,
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
            time_part = match_time_match.group(2).upper().replace(" ", "")
            match["start_time_utc"] = parse_match_time(date_part, time_part)

    # Extract match-time label (live or post-game)
    time_div = div.select_one(".fixtures__match-time")
    log.debug(f"🔍 Extracting match time label: {time_div}")
    if time_div and time_div.contents:
        match["match_time_label"] = time_div.contents[0].strip()
        log.debug(f"🕒 Match time label found: {match['match_time_label']}")
    else:
        # Fallback for COMPLETED matches
        label_div = div.select_one(".fixtures__status-label")
        if label_div:
            match["match_time_label"] = label_div.get_text(strip=True)

    # Extract score if match is completed
    score_divs = div.select(".fixtures__match-score-total")
    if len(score_divs) == 2:
        try:
            match["score_home"] = int(score_divs[0].text.strip())
            match["score_away"] = int(score_divs[1].text.strip())
        except ValueError:
            pass

    if not match["start_time_utc"] and existing_match:
        # Only override if we're scraping a LIVE or undefined match with missing time
        match["start_time_utc"] = existing_match.get("start_time_utc")

    return match


def parse_matches_from_html(
    html: str, existing_matches: dict[int, dict] | None = None
) -> list[dict]:
    existing_matches = existing_matches or {}
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select("h2.fixtures__date-header, div.fixtures__item")

    matches = []
    season_year = extract_season_year(html)

    for element in content:
        if element.name == "div" and "fixtures__item" in element.get("class", []):
            mid = _required_int_attr(element, "data-match-id", "match_id")
            match = extract_match_data(
                element, season_year, existing_match=existing_matches.get(mid)
            )
            matches.append(match)

    if not matches:
        raise FixtureParseError("No fixture match items found using selector 'div.fixtures__item'")

    return matches


def scrape_round(round_id: int, conn: sqlite3.Connection):
    url = get_fixture_url_for_round(round_id)
    existing_matches = load_existing_matches(conn)
    log.info(f"🔄 Scraping round {round_id} from {url}")

    html = load_page_with_playwright(url)
    if not html:
        log.error(f"❌ Failed to load page for round {round_id}")
        return

    try:
        matches = parse_matches_from_html(html, existing_matches)
    except FixtureParseError as exc:
        log.error(f"❌ Failed to parse fixture page for round {round_id}: {exc}")
        return

    if matches:
        save_matches_to_db(matches, conn)
        log.info(f"✅ Saved {len(matches)} matches for round {round_id}")
        for m in matches:
            status = m["status"]
            teams = f"{m['home_team']} vs {m['away_team']}"
            time = m['start_time_utc'] or "no time"
            log.debug(f"🕒 {round_id} | {teams} | {time} | {status}")
    else:
        log.warning(f"⚠️ No matches found for round {round_id}")


def run(round_id: int | None = None):
    conn = sqlite3.connect("data/afl_players.db")

    if round_id:
        scrape_round(round_id, conn)
    else:
        log.info("🔁 No round ID provided, scraping all rounds from DB...")
        cursor = conn.cursor()
        cursor.execute("SELECT round_id FROM rounds ORDER BY round_id ASC")
        for (rid,) in cursor.fetchall():
            scrape_round(rid, conn)

    conn.close()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    run(int(arg) if arg and arg.isdigit() else None)
