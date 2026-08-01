import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
from db.import_to_db import save_injuries_to_db
from db.scrape_runs import audited_scrape_run
from db.connection import get_db_connection

from bs4 import BeautifulSoup, Comment

from utils.log import setup_logger
from merge.helpers import build_canonical_injury_player_resolver
from utils.club_lookup import get_canonical_club, load_clubs, resolve_club_code
from utils.dictionary import CLUB_SLUG_ALIASES
from scraper.afl_selectors import INJURY_SELECTORS
from scraper.injuries.acquisition import InjuryAcquirer

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
    filename = Path(unquote(urlsplit(img_src).path)).name
    slug_raw = Path(filename).stem

    # Current editorial artwork names contain useful identifiers among unrelated
    # production tokens. Match each separator-delimited token independently; do
    # not turn the full, release-specific artwork name into a persistent alias.
    for token in re.split(r"[_\-\s]+", slug_raw):
        club = get_canonical_club(token)
        if club:
            log.debug(f"🆔 Matched image filename token: {token} → {club['code']}")
            return club
    
    # Strip only recognised trailing suffixes
    slug_cleaned = re.sub(r"(-(?:sdnr-)?(?:strap|logo|banner)(?:-[\d]{4})?)$", "", slug_raw, flags=re.IGNORECASE)
    slug_cleaned = re.sub(r"[^a-z]", "", slug_cleaned.lower())

    if slug_raw != slug_cleaned:
        log.debug(f"🧽 Cleaned slug: '{slug_raw}' → '{slug_cleaned}'")

    club = get_canonical_club(slug_cleaned)
    if club:
        log.debug(f"🆔 Matched using whole-slug fallback: {slug_cleaned} → {club['code']}")
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

def scrape_injury_list(db_conn, trigger_source: str | None = None, correlation_id: str | None = None) -> dict:
    with audited_scrape_run("injury", target_type="injury_list", trigger_source=trigger_source, correlation_id=correlation_id, conn=db_conn) as audit:
        result = _scrape_injury_list(db_conn)
        audit["rows_read"] = sum(team.get("player_count", 0) for team in result.get("teams", []))
        return result

def parse_injuries_html(html: str, db_conn=None, *, club_resolver=extract_and_match_club,
                        player_resolver=None) -> list[dict]:
    """Parse rendered injury-list HTML without acquiring a browser page."""
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select_one(INJURY_SELECTORS.ARTICLE_BODY):
        raise ValueError(
            f"Injury source contract missing article body '{INJURY_SELECTORS.ARTICLE_BODY}'"
        )
    team_blocks = soup.select(INJURY_SELECTORS.TEAM_BLOCKS)
    if not team_blocks:
        raise ValueError(
            f"Injury source contract contains no team blocks '{INJURY_SELECTORS.TEAM_BLOCKS}'"
        )

    results = []
    indexed_resolver = None
    for index, block in enumerate(team_blocks):
        comment = block.find(string=lambda text: isinstance(text, Comment))
        image_soup = BeautifulSoup(comment, "html.parser") if comment else None
        img = image_soup.find("img", class_=INJURY_SELECTORS.PROMO_IMAGE_CLASS) if image_soup else None
        if not img or not img.get("src"):
            raise ValueError(f"Injury team block {index} is missing its commented promo image")
        club = club_resolver(img["src"], img.get("alt", "").strip())
        if not club:
            raise ValueError(f"Injury team block {index} has an unrecognised club image")

        table_wrapper = block.find_next_sibling()
        if table_wrapper and "table" not in table_wrapper.get("class", []):
            table_wrapper = None
        table = table_wrapper.find("table") if table_wrapper else None
        if table is None:
            raise ValueError(f"Injury team block {index} ({club['code']}) is missing its table")

        players, updated_text = [], ""
        for row_index, row in enumerate(table.find_all("tr")[1:], start=1):
            cols = row.find_all("td")
            if len(cols) >= 3:
                name = cols[0].get_text(" ", strip=True)
                if player_resolver is None:
                    if indexed_resolver is None:
                        indexed_resolver = build_canonical_injury_player_resolver(db_conn)
                    resolution = indexed_resolver.resolve(name, club["code"])
                else:
                    resolution = player_resolver(name, club["code"], db_conn)
                players.append({
                    "name": name,
                    "injury": cols[1].get_text(" ", strip=True),
                    "return": cols[2].get_text(" ", strip=True),
                    "afl_id": resolution.afl_id,
                    "canonical_player_id": resolution.canonical_player_id,
                    "resolution_status": resolution.status,
                    "resolution_reason": resolution.reason,
                })
            elif len(cols) == 1 and "updated:" in cols[0].get_text().lower():
                match = re.search(r"updated:\s*(.+)", cols[0].get_text(" ", strip=True), re.I)
                updated_text = match.group(1).strip() if match else ""
            elif cols:
                raise ValueError(
                    f"Injury table for {club['code']} has unexpected row {row_index} with {len(cols)} cells"
                )
        results.append({"club": club["code"], "updated": updated_text,
                        "player_count": len(players), "players": players})
    return results

def _scrape_injury_list(db_conn) -> dict:
    document = InjuryAcquirer().acquire()
    results = parse_injuries_html(document.html, db_conn)
    return {
        "source": document.source_url,
        "scraped_at": document.acquired_at,
        "teams": results
    }

if __name__ == "__main__":
    db_conn = get_db_connection()
    try:
        result = scrape_injury_list(db_conn)
    finally:
        db_conn.close()
    print(json.dumps(result, indent=2))
