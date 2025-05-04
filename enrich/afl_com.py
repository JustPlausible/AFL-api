from playwright.sync_api import sync_playwright
from utils.log import log
from datetime import datetime
import re
import time

BASE_URL = "https://www.afl.com.au/players"

def enrich_from_afl_profile(afl_id: int) -> dict:
    url = f"{BASE_URL}/{afl_id}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            header_el = page.query_selector("header.stats-player-header")
            name_el = page.query_selector("h1.stats-player-header__name")

            first_name = last_name = full_name = ""
            if name_el:
                first = name_el.text_content().split()[0].strip()
                last = name_el.query_selector("span").inner_text().strip()
                full_name = f"{first} {last}"
                first_name = first
                last_name = last

            cd_id = header_el.get_attribute("data-player-provider-id") if header_el else None

        finally:
            browser.close()

    return {
        "afl_id": afl_id,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "champion_data_id": cd_id,
        "afl_url": url
    }

def search_afl_id_by_name(name: str) -> dict:
    url = "https://www.afl.com.au/stats"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=20000)
            search_input = page.wait_for_selector("#statsPlayerSearch", timeout=8000)
            search_input.fill(name)
            page.wait_for_timeout(2000)

            result_link = page.query_selector("a[href^='/players/']")
            if not result_link:
                log(f"[✗] No result for: {name}", "WARN")
                return None

            href = result_link.get_attribute("href")
            match = re.search(r"/players/(\d+)", href)
            afl_id = int(match.group(1)) if match else None
            label = result_link.inner_text().strip()
            full_url = f"https://www.afl.com.au{href}"

            return {
                "afl_id": afl_id,
                "full_name": label,
                "afl_url": full_url
            }
        finally:
            browser.close()

def extract_club_player_id(url: str) -> int:
    match = re.search(r"/players/(\d+)", url)
    return int(match.group(1)) if match else None

def resolve_player(player: dict) -> dict | None:
    full_name = player["full_name"]
    log(f"🔍 Resolving: {full_name}", "INFO")

    club_id = extract_club_player_id(player["profile_url"])
    afl_id = None
    afl_data = None

    if club_id:
        afl_data = enrich_from_afl_profile(club_id)
        if afl_data and afl_data["full_name"].upper() == full_name.upper():
            afl_id = afl_data["afl_id"]
            log(f"✅ Matched via club ID: {afl_id}", "DEBUG")
        else:
            afl_data = None

    if not afl_id:
        log("↪️ Falling back to name search...", "DEBUG")
        search_result = search_afl_id_by_name(full_name)
        if search_result:
            afl_id = search_result["afl_id"]
            log(f"✅ Matched via search: {afl_id}", "DEBUG")
            afl_data = enrich_from_afl_profile(afl_id)
        else:
            log(f"❌ Could not resolve AFL ID for {full_name}", "ERROR")
            return None

    formatted_last_name = afl_data['last_name'].strip().title()
    formatted_nickname = f"{afl_data['first_name'].strip().title()} {formatted_last_name}"

    return {
        "full_name": full_name,
        "first_name": afl_data["first_name"].strip().title(),
        "last_name": afl_data["last_name"].strip().upper(),
        "formatted_last_name": formatted_last_name,
        "nickname": player["nickname"],
        "formatted_nickname": formatted_nickname,
        "club": player["club"],
        "guernsey": player["guernsey"],
        "position": player["position"],
        "club_profile_url": player["profile_url"],
        "image_url": player.get("image_url"),
        "club_player_id": club_id,
        "afl_id": afl_id,
        "afl_url": afl_data["afl_url"],
        "champion_data_id": afl_data["champion_data_id"],
        "source": "afl.com.au",
        "resolved_at": datetime.utcnow().isoformat() + "Z"
    }
