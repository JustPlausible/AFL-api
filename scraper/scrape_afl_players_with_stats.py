# scraper/scrape_afl_players_with_stats.py

from playwright.sync_api import sync_playwright
from utils.log import setup_logger
from merge.helpers import extract_champion_data_id_from_html
import csv
import re
from pathlib import Path
from scraper.afl_selectors import STATS_LEADERS_SELECTORS
from scraper.afl_selectors import STATS_LEADERS_SELECTORS

log = setup_logger("bbbffl_stats_scraper", "scrape_afl_players_with_stats.log")

OUTPUT_FILE = Path("data/bbbffl_player_stats.csv")

TOTALS_URL = "https://www.afl.com.au/stats/leaders?dataType=totals"
AVERAGES_URL = "https://www.afl.com.au/stats/leaders?dataType=averages"

BBBFFL_STATS = ["Goals", "Disposals", "Hitouts", "Marks", "Tackles"]


def load_all_rows(page):
    max_clicks = 50
    for _ in range(max_clicks):
        button = page.query_selector(STATS_LEADERS_SELECTORS.LOAD_MORE_BUTTON)
        if button and button.is_visible():
            button.click()
            page.wait_for_timeout(1000)
        else:
            break


def scrape_table(page):
    players = {}
    rows = page.query_selector_all(STATS_LEADERS_SELECTORS.BODY_ROWS)
    for row in rows:
        try:
            name_link = row.query_selector(STATS_LEADERS_SELECTORS.PLAYER_NAME_LINK)
            if not name_link:
                continue

            name = name_link.inner_text().strip()
            profile_url = name_link.get_attribute("href")
            afl_id = int(re.search(r"/players/(\d+)", profile_url).group(1))
            html = row.inner_html()
            champ_id, _ = extract_champion_data_id_from_html(html)

            stat_buttons = row.query_selector_all(STATS_LEADERS_SELECTORS.STAT_BUTTONS)
            stats = {}
            for btn in stat_buttons:
                title = btn.get_attribute("title") or ""
                match = re.match(r".*: ([^.]+)\.", title)
                if match:
                    stat_name = match.group(1).strip()
                    if stat_name in BBBFFL_STATS:
                        stats[stat_name] = btn.inner_text().strip()

            if len(stats) < len(BBBFFL_STATS):
                continue

            players[afl_id] = {
                "Full Name": name,
                "AFL ID": afl_id,
                "Champion Data ID": champ_id,
                **stats
            }
        except Exception as e:
            log.warning(f"⚠️ Error parsing row: {e}")
    return players


def scrape_afl_stats():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(TOTALS_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        load_all_rows(page)
        totals = scrape_table(page)

        page.goto(AVERAGES_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        load_all_rows(page)
        averages = scrape_table(page)

        browser.close()

    merged_players = []
    for afl_id, total_data in totals.items():
        avg_data = averages.get(afl_id, {})
        row = {
            "Full Name": total_data["Full Name"],
            "AFL ID": afl_id,
            "Champion Data ID": total_data.get("Champion Data ID")
        }
        for stat in BBBFFL_STATS:
            try:
                total = float(total_data.get(stat, 0))
                avg = float(avg_data.get(stat, 0))
            except ValueError:
                total, avg = 0, 0
            row[stat] = total
            row[f"{stat}.avg"] = avg

        try:
            row["Games"] = int(round(row["Disposals"] / row["Disposals.avg"])) if row["Disposals.avg"] > 0 else 0
        except:
            row["Games"] = 0

        merged_players.append(row)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", newline='', encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "Full Name", "AFL ID", "Champion Data ID",
            "Goals", "Goals.avg",
            "Disposals", "Disposals.avg",
            "Hitouts", "Hitouts.avg",
            "Marks", "Marks.avg",
            "Tackles", "Tackles.avg",
            "Games"
        ])
        writer.writeheader()
        writer.writerows(merged_players)

    log.info(f"✅ SUCCESS: {len(merged_players)} players saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    scrape_afl_stats()
