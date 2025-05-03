from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from pathlib import Path
from utils.log import log
import json
import re

def scrape_club_players(club_name: str, url: str) -> list[dict]:
    log(f"🌐 Scraping squad for: {club_name.title()}", "INFO")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        try:
            page.wait_for_selector(".squad-list__item", timeout=15000)
        except PlaywrightTimeout:
            log(f"[!] No '.squad-list__item' on page for {club_name} — dumping HTML", "DEBUG")
            log(page.content(), "DEBUG")
            browser.close()
            return []

        players = []
        cards = page.query_selector_all(".squad-list__item")

        for card in cards:
            link = card.query_selector("a.player-item")
            href = link.get_attribute("href") if link else None
            profile_url = f"https://www.{club_name.lower()}fc.com.au{href}" if href else None

            first_name_el = card.query_selector("h1.player-item__name")
            last_name_el = card.query_selector(".player-item__last-name")
            position_el = card.query_selector(".player-item__position")
            guernsey_el = card.query_selector(".player-item__jumper-number")
            id_div = card.query_selector(".js-player-image")

            first_name = first_name_el.inner_text().split()[0].strip() if first_name_el else ""
            last_name = last_name_el.inner_text().strip() if last_name_el else ""
            position = position_el.inner_text().strip() if position_el else ""
            guernsey = int(guernsey_el.inner_text().strip()) if guernsey_el else None
            afl_id = id_div.get_attribute("data-player") if id_div else None

            full_name = f"{first_name} {last_name}".strip()

            players.append({
                "full_name": full_name,
                "nickname": full_name,
                "club": club_name.title(),
                "guernsey": guernsey,
                "position": position,
                "profile_url": profile_url,
                "afl_id": int(afl_id) if afl_id else None
            })

        browser.close()
        return players

def save_club_players_to_json(club_name: str, url: str, skip_existing=False):

    output_file = Path(f"data/players-{club_name.lower()}-raw.json")
    if skip_existing and output_file.exists():
        log(f"Skipping {club_name.title()} (raw file exists)", "DEBUG")
        return

    players = scrape_club_players(club_name, url)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w") as f:
        json.dump(players, f, indent=2)

    log(f"✓ Saved {len(players)} players for {club_name.title()} → {output_file}", "INFO")
