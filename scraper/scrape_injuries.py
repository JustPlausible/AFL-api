import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from db.import_to_db import save_injuries_to_db

from bs4 import BeautifulSoup, Comment
from playwright.sync_api import sync_playwright

from utils.log import log
from merge.helpers import match_injury_player_to_db
from utils.club_lookup import get_club_by_slug, load_clubs
from utils.dictionary import CLUB_SLUG_ALIASES

def extract_and_match_club(img_src: str) -> dict | None:
    """Extract slug from image URL and match it to a known club."""
    log(f"🖼 Image src: {img_src}", "DEBUG")
    filename = img_src.split("/")[-1].split("?")[0]
    slug_raw = filename.replace(".jpg", "")

    slug_cleaned = re.sub(r"(-strap.*|-afl-banner.*|-new-logo.*|-logo.*)", "", slug_raw)
    slug_cleaned = re.sub(r"[^a-z]", "", slug_cleaned.lower())

    clubs = load_clubs()

    for club in clubs:
        club_key = re.sub(r"[^a-z]", "", club["slug"].lower())
        if slug_cleaned == club_key:
            return club

    if slug_cleaned in CLUB_SLUG_ALIASES:
        fallback_slug = CLUB_SLUG_ALIASES[slug_cleaned]
        club = get_club_by_slug(fallback_slug)
        if club:
            log(f"🔁 Matched using alias: {slug_cleaned} → {fallback_slug}", "DEBUG")
            return club

    log(f"[!] ❓ Could not match normalised slug: {slug_cleaned}", "WARN")
    return None

def scrape_injury_list(db_conn) -> dict:
    url = "https://www.afl.com.au/matches/injury-list"
    log(f"🌐 Fetching injury list from: {url}", "INFO")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=60000)
        page.wait_for_selector("div.article__body", timeout=15000)
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        team_blocks = soup.select("div.articleWidget.full-width")

        log("✅ Page rendered", "DEBUG")
        log(f"🔍 Found {len(team_blocks)} team blocks", "DEBUG")

        results = []
        for i, block in enumerate(team_blocks):
            club = None

            comment = block.find(string=lambda text: isinstance(text, Comment))
            if comment:
                image_soup = BeautifulSoup(comment, "html.parser")
                img = image_soup.find("img", class_="promo-image__image")
                if img and img.get("src"):
                    club = extract_and_match_club(img["src"])

            club_slug = club["slug"] if club else None
            club_code = club["code"] if club else "???"

            log(f"🧩 [{i}] Club: {club_code} ({club_slug})", "DEBUG")
            if not club_slug:
                log(f"[!] No club slug for block {i}", "WARN")
                continue

            # Table sits in next sibling div
            table_wrapper = block.find_next_sibling("div", class_="table")
            if not table_wrapper:
                log(f"[!] No table wrapper for {club_code}", "WARN")
                continue

            table = table_wrapper.find("table")
            if not table:
                log(f"[!] No table found for {club_code}", "WARN")
                continue

            rows = table.find_all("tr")[1:]  # Skip header
            players = []
            for row in rows:
                cols = row.find_all("td")
                if len(cols) >= 3:
                    name = cols[0].text.strip()
                    afl_id = match_injury_player_to_db(name, club_code, conn=db_conn)
                    if afl_id:
                        log(f"✅ Matched player '{name}' to AFL ID {afl_id}", "DEBUG")
                    else:
                        log(f"❌ No match for player '{name}' ({club_code})", "WARN")
                    players.append({
                        "name": name,
                        "injury": cols[1].text.strip(),
                        "return": cols[2].text.strip(),
                        "afl_id": afl_id
                    })
                elif len(cols) == 1 and "updated:" in cols[0].text.lower():
                    # Extract the update date text
                    updated_text = None
                    match = re.search(r"updated:\s*(.+)", cols[0].text.strip(), re.IGNORECASE)
                    if match:
                        updated_text = match.group(1).strip()
                        log(f"🗓️  Injury list updated: {updated_text} ({club_code})", "DEBUG")

            log(f"📦 {club_code}: {len(players)} players", "INFO")

            results.append({
                "club": club_code,
                "updated": updated_text or "",
                "player_count": len(players),
                "players": players
            })

        browser.close()

    return {
        "source": url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "teams": results
    }

if __name__ == "__main__":
    db_conn = sqlite3.connect("data/afl_players.db")
    db_conn.row_factory = sqlite3.Row
    result = scrape_injury_list(db_conn)
    db_conn.close()
    print(json.dumps(result, indent=2))
