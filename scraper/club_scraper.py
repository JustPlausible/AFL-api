from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from pathlib import Path
from utils.log import log
from merge.helpers import extract_champion_data_id_from_html
from datetime import datetime
import json
import re

def scrape_club_players(club: dict) -> list[dict]:
    club_name = club["name"]
    slug = club["slug"]
    url = club["squad_url"]
    website = club["website"]

    log(f"🌐 Scraping squad for: {club_name}", "INFO")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)

        try:
            page.wait_for_selector(".squad-list__item", timeout=15000)
        except PlaywrightTimeout:
            log(f"[!] No '.squad-list__item' on page for {club_name} — dumping HTML", "DEBUG")
            log(page.content(), "DEBUG")
            browser.close()
            return []

        # Force lazy-load content to render
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

        players = []
        cards = page.query_selector_all(".squad-list__item")
        scrape_time = datetime.utcnow().isoformat()

        for card in cards:
            try:
                link = card.query_selector("a.player-item")
                href = link.get_attribute("href") if link else None
                club_profile_url = f"{website}{href}" if href else None
                club_profile_url = club_profile_url.strip() if club_profile_url else None

                # Extract Club ID from profile URL
                club_id = None
                if club_profile_url:
                    match = re.search(r"/players/(\d+)/", club_profile_url)
                    if match:
                        club_id = int(match.group(1))

                first_name_el = card.query_selector("h1.player-item__name")
                last_name_el = card.query_selector(".player-item__last-name")
                position_el = card.query_selector(".player-item__position")
                guernsey_el = card.query_selector(".player-item__jumper-number")


                if first_name_el:
                    raw_html = first_name_el.inner_html()
                    clean_html = re.sub(r"<span.*?</span>", "", raw_html).strip()
                    first_name = clean_html
                else:
                    first_name = ""

                last_name = last_name_el.inner_text().strip() if last_name_el else ""

                full_name = f"{first_name} {last_name}".strip()
                short_name = f"{first_name[0]}. {last_name}".strip() if first_name and last_name else full_name
                formatted_nickname = first_name.upper()
                formatted_last_name = last_name.upper()

                position = position_el.inner_text().strip().upper() if position_el else ""
                guernsey = int(guernsey_el.inner_text().strip()) if guernsey_el else None

                # Image extraction
                image_el = card.query_selector("img.picture__img") or card.query_selector("picture img")
                raw_image_url = None
                if image_el:
                    raw_image_url = (
                        image_el.get_attribute("src")
                        or image_el.get_attribute("data-src")
                        or image_el.get_attribute("srcset")
                    )

                image_url = None
                champion_data_id = None

                html = card.inner_html()
                champion_data_id, image_url = extract_champion_data_id_from_html(html)

                if not image_url:
                    log(f"[!] Missing image for {full_name} — HTML: {html[:500]}", "DEBUG")

                players.append({
                    "full_name": full_name,
                    "short_name": short_name,
                    "first_name": first_name,
                    "last_name": last_name,
                    "nickname": full_name,
                    "formatted_nickname": formatted_nickname,
                    "formatted_last_name": formatted_last_name,
                    "club": club_name,
                    "guernsey": guernsey,
                    "position": position,
                    "club_profile_url": club_profile_url,
                    "image_url": image_url,
                    "champion_data_id": champion_data_id,
                    "club_id": club_id,
                    "scraped_at": scrape_time
                })

            except Exception as e:
                log(f"[!] Error parsing player card: {e}", "WARN")
                try:
                    log(card.inner_html()[:500], "DEBUG")
                except Exception:
                    log("[!] Could not fetch inner HTML for failed card.", "DEBUG")

        browser.close()
        return players


def save_club_players_to_json(club: dict, skip_existing=False):
    slug = club["slug"]
    output_file = Path(f"data/players-{slug}-raw.json")

    if skip_existing and output_file.exists():
        log(f"Skipping {club['name']} (raw file exists)", "DEBUG")
        return

    players = scrape_club_players(club)

    # Validation checks
    missing_image = [p for p in players if not p.get("image_url")]
    missing_champion_id = [p for p in players if not p.get("champion_data_id")]
    missing_club_id = [p for p in players if not p.get("club_id")]

    if missing_image:
        log(f"[!] {len(missing_image)} player(s) missing image_url in {club['name']}:", "WARN")
        for p in missing_image:
            log(f"  - {p['full_name']} ({p.get('club_profile_url', 'no profile')})", "DEBUG")

    if missing_champion_id:
        log(f"[!] {len(missing_champion_id)} player(s) missing champion_data_id in {club['name']}:", "WARN")
        for p in missing_champion_id:
            log(f"  - {p['full_name']}", "DEBUG")

    if missing_club_id:
        log(f"[!] {len(missing_club_id)} player(s) missing club_id in {club['name']}:", "WARN")
        for p in missing_club_id:
            log(f"  - {p['full_name']} ({p.get('club_profile_url', 'no profile')})", "DEBUG")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w") as f:
        json.dump(players, f, indent=2)

    log(f"✓ Saved {len(players)} players for {club['name']} → {output_file}", "INFO")
    
    summary = {
        "club": club["name"],
        "total": len(players),
        "missing_image": len(missing_image),
        "missing_champion_id": len(missing_champion_id),
        "missing_club_id": len(missing_club_id)
    }

    return summary