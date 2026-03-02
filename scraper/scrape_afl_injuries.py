import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from db.import_to_db import save_injuries_to_db

from bs4 import BeautifulSoup, Comment
from playwright.sync_api import sync_playwright

from utils.log import setup_logger
from merge.helpers import match_injury_player_to_db
from utils.club_lookup import get_club_by_slug, load_clubs, resolve_club_code
from utils.dictionary import CLUB_SLUG_ALIASES

log = setup_logger("injury_scraper", "scrape_afl_injuries.log")

def extract_and_match_club(img_src: str, alt_text: str = "") -> dict | None:
    """Extract a club match using alt text, then fallback to slug matching."""
    clubs = load_clubs()

    # First try: resolve from alt text (e.g. 'Kuwarna', 'Narrm')
    if alt_text:
        log.debug(f"🎯 Attempting match from alt text: '{alt_text}'")
        club_code = resolve_club_code(alt_text)
        club = next((c for c in clubs if c["code"] == club_code), None)
        if club:
            log.debug(f"✅ Matched via alt text '{alt_text}' → {club_code}")
            return club
        else:
            log.warning(f"⚠️ Alt text '{alt_text}' did not match any known club")

    # Fallback: extract and normalise from img src
    log.debug(f"🖼 Image src: {img_src}")
    filename = img_src.split("/")[-1].split("?")[0]  # e.g. 'kuwarna-strap-2024-logo.jpg'
    slug_raw = filename.replace(".jpg", "")
    
    # Strip only recognised trailing suffixes
    slug_cleaned = re.sub(r"(-(?:sdnr-)?(?:strap|logo|banner)(?:-[\d]{4})?)$", "", slug_raw, flags=re.IGNORECASE)
    slug_cleaned = re.sub(r"[^a-z]", "", slug_cleaned.lower())

    if slug_raw != slug_cleaned:
        log.debug(f"🧽 Cleaned slug: '{slug_raw}' → '{slug_cleaned}'")

    if slug_cleaned in [club["code"].lower() for club in clubs]:
        club = next(c for c in clubs if c["code"].lower() == slug_cleaned)
        log.debug(f"🆔 Matched using club code fallback: {slug_cleaned} → {club['code']}")
        return club

    # Try match against slug or aliases
    for club in clubs:
        slug_key = re.sub(r"[^a-z]", "", club["slug"].lower())
        if slug_cleaned == slug_key:
            return club

        aliases = club.get("aliases") or []
        if isinstance(aliases, str):
            try:
                aliases = json.loads(aliases)
            except json.JSONDecodeError:
                aliases = []

        for alias in aliases:
            alias_clean = re.sub(r"[^a-z]", "", alias.lower())
            if slug_cleaned == alias_clean:
                log.debug(f"🔁 Matched using alias: {slug_cleaned} → {club['code']}")
                return club

    log.warning(f"[!] ❓ Could not match normalised slug: {slug_cleaned}")
    return None

def scrape_injury_list(db_conn) -> dict:
    url = "https://www.afl.com.au/matches/injury-list"
    log.info(f"🌐 Fetching injury list from: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=60000)
        page.wait_for_selector("div.article__body", timeout=15000)
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        team_blocks = soup.select("div.articleWidget.full-width")

        log.debug("✅ Page rendered")
        log.debug(f"🔍 Found {len(team_blocks)} team blocks")

        results = []
        for i, block in enumerate(team_blocks):
            club = None

            comment = block.find(string=lambda text: isinstance(text, Comment))
            if comment:
                image_soup = BeautifulSoup(comment, "html.parser")
                img = image_soup.find("img", class_="promo-image__image")
                if img and img.get("src"):
                    alt_text = img.get("alt", "").strip()
                    club = extract_and_match_club(img["src"], alt_text)

            club_slug = club["slug"] if club else None
            club_code = club["code"] if club else "???"

            log.debug(f"🧩 [{i}] Club: {club_code} ({club_slug})")
            if not club_slug:
                log.warning(f"[!] No club slug for block {i}")
                continue

            # Table sits in next sibling div
            table_wrapper = block.find_next_sibling("div", class_="table")
            if not table_wrapper:
                log.warning(f"[!] No table wrapper for {club_code}")
                continue

            table = table_wrapper.find("table")
            if not table:
                log.warning(f"[!] No table found for {club_code}")
                continue

            rows = table.find_all("tr")[1:]  # Skip header
            players = []
            for row in rows:
                cols = row.find_all("td")
                if len(cols) >= 3:
                    name = cols[0].text.strip()
                    afl_id = match_injury_player_to_db(name, club_code, conn=db_conn)
                    if afl_id:
                        log.debug(f"✅ Matched player '{name}' to AFL ID {afl_id}")
                    else:
                        log.warning(f"❌ No match for player '{name}' ({club_code})")
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
                        log.debug(f"🗓️  Injury list updated: {updated_text} ({club_code})")

            log.info(f"📦 {club_code}: {len(players)} players")

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
