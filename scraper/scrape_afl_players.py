from playwright.sync_api import sync_playwright
from utils.log import log
from merge.helpers import extract_champion_data_id_from_html
import json
import re
from pathlib import Path

OUTPUT_FILE = Path("data/afl_stats_leaderboard.json")


def scroll_leaderboard_to_load_images(page, scroll_step=1000, delay_ms=200, max_scrolls=30):
    container = page.query_selector("div.js-scrollable-container")
    if not container:
        log("⚠️ Could not find scroll container", "WARN")
        return

    for i in range(max_scrolls):
        page.evaluate("(args) => args[0].scrollBy(0, args[1])", [container, scroll_step])
        page.wait_for_timeout(delay_ms)
        log(f"↕️ Scrolled leaderboard container [{i+1}/{max_scrolls}]", "DEBUG")


def force_load_all_player_images(page):
    image_elements = page.query_selector_all("img.picture__img")
    log(f"🔍 Forcing scroll into view for {len(image_elements)} images", "DEBUG")
    for img in image_elements:
        try:
            img.scroll_into_view_if_needed()
            page.wait_for_timeout(10)
        except Exception as e:
            log(f"⚠️ Failed scrollIntoView: {e}", "WARN")


def scroll_to_bottom_and_force_final_image(page):
    container = page.query_selector("div.js-scrollable-container")
    if not container:
        log("⚠️ Could not find scroll container", "WARN")
        return

    page.evaluate("(el) => el.scrollTop = el.scrollHeight", container)
    page.wait_for_timeout(2000)

    final_row = page.query_selector("tr.stats-table__body-row:last-child")
    if final_row:
        final_row.scroll_into_view_if_needed()
        page.wait_for_timeout(300)


def load_all_stats_rows(page):
    max_clicks = 50
    for i in range(max_clicks):
        scroll_leaderboard_to_load_images(page, scroll_step=800, delay_ms=150, max_scrolls=3)

        button = page.query_selector("button.stats-table-load-more-button")
        if button and button.is_visible():
            button.click()
            log(f"🔄 Clicked 'Show more' button [{i+1}/{max_clicks}]", "DEBUG")
            page.wait_for_timeout(1000)
        else:
            log("✅ All rows loaded or button hidden.", "DEBUG")
            break

    scroll_leaderboard_to_load_images(page, scroll_step=1000, delay_ms=200, max_scrolls=10)
    force_load_all_player_images(page)
    scroll_to_bottom_and_force_final_image(page)


def parse_row(row):
    try:
        name_link = row.query_selector("a.stats-leaders-table-player__name")
        if not name_link:
            return None

        name = name_link.inner_text().strip()
        profile_url = name_link.get_attribute("href")
        afl_id = int(re.search(r"/players/(\d+)", profile_url).group(1))

        image_url = None
        headshot_div = row.query_selector(".stats-leaders-table-player__headshot")
        if headshot_div:
            img_tag = headshot_div.query_selector("img.picture__img")
            if img_tag:
                img_tag.scroll_into_view_if_needed()
                img_tag.evaluate("el => new Promise(resolve => setTimeout(resolve, 100))")
                image_url = img_tag.get_attribute("src")

        #cd_id_match = re.search(r"/(\d+)\.png", image_url or "")
        #champion_data_id = cd_id_match.group(1) if cd_id_match else None
        html = row.inner_html()
        champion_data_id, _ = extract_champion_data_id_from_html(html)

        return {
            "full_name": name,
            "afl_id": afl_id,
            "afl_url": f"https://www.afl.com.au{profile_url}",
            "champion_data_id": champion_data_id
        }
    except Exception as e:
        log(f"⚠️ Failed to parse a row: {e}", "WARN")
        return None


def scrape_afl_stats_leaderboard():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto("https://www.afl.com.au/stats/leaders", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1000)

            load_all_stats_rows(page)

            rows = page.query_selector_all("tr.stats-table__body-row")
            log(f"ℹ️ Found {len(rows)} player rows", "INFO")

            players = []
            for row in rows:
                player = parse_row(row)
                if player:
                    players.append(player)

            OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
            with OUTPUT_FILE.open("w") as f:
                json.dump(players, f, indent=2)

            log(f"✅ SUCCESS: 📄 Saved {len(players)} players to {OUTPUT_FILE}", "SUCCESS")
        finally:
            browser.close()


if __name__ == "__main__":
    scrape_afl_stats_leaderboard()
