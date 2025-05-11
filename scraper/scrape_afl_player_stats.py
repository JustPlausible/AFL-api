# scraper/scrape_live_player_stats.py

import time
import random
import re
import argparse
import sqlite3
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from utils.log import log
from utils.http_utils import load_page_with_playwright
from db.import_to_db import save_player_stats_to_db, log_scrape_event
from merge.helpers import extract_club_player_id, extract_champion_id
from db.connection import get_db_connection

def get_match_status_from_header(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    label = soup.select_one("span.mc-header__status-label")
    if label:
        label_text = label.get_text(strip=True).upper()
        if "FULL TIME" in label_text:
            return "COMPLETED"
        elif "Q1" in label_text or "Q2" in label_text or "Q3" in label_text or "Q4" in label_text:
            return "LIVE"
        elif "LIVE" in label_text:
            return "LIVE"
    return "LIVE"  # Safe fallback

def parse_live_stats(html: str, match_id: int, round_id: int | None, status: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    stats_table = soup.select_one("table.stats-table__table")

    if not stats_table:
        log("❌ Could not find player stats table.", "ERROR")
        return []

    headers = [
        th.text.strip()
        for th in stats_table.select("thead tr.stats-table__header-row th")
    ]
    log(f"📋 Detected headers: {headers}", "DEBUG")

    rows = stats_table.select("tbody.stats-table__body-row, tr.stats-table__body-row")
    players = []

    for row in rows:
        cells = row.find_all(["th", "td"])
        if not cells or len(cells) < len(headers):
            continue

        try:
            profile_link = row.select_one("a.mc-player-stats-table__player")
            player_name = profile_link.get_text(" ", strip=True)
            profile_url = profile_link["href"]
            afl_id = extract_club_player_id(profile_url)

            img_url = row.select_one("img.mc-player-stats-table__headshot")["src"]
            champion_id = extract_champion_id(img_url)

            jumper_span = row.select_one("span.mc-player-stats-table__jumper-number")
            team_code = jumper_span["class"][-1].upper() if jumper_span else "UNK"

            # Build full cell map using header → cell text
            cell_map = {
                headers[i]: cells[i].text.strip().replace("%", "")
                for i in range(min(len(headers), len(cells)))
            }

            field_map = {
                "AF": "af_score", "G": "goals", "B": "behinds", "D": "disposals",
                "K": "kicks", "H": "handballs", "M": "marks", "T": "tackles",
                "HO": "hitouts", "CLR": "clearances", "MG": "metres_gained",
                "GA": "goal_assists", "ToG%": "time_on_ground_pct"
            }

            data = {
                "match_id": match_id,
                "round_id": round_id,
                "afl_id": afl_id,
                "champion_id": champion_id,
                "player_name": player_name,
                "team_code": team_code,
                "status": status,
            }

            for header, key in field_map.items():
                raw = cell_map.get(header, "")
                try:
                    val = float(raw) if key == "time_on_ground_pct" else int(raw or 0)
                except ValueError:
                    log(f"⚠️ Could not convert {raw} for field {key}", "WARN")
                    val = None
                data[key] = val

            players.append(data)

        except Exception as e:
            log(f"⚠️ Error parsing row: {e}", "WARN")

    log(f"✅ Parsed {len(players)} player stat rows", "DEBUG")
    return players

def run_scraper(match_id: int, once: bool = False):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    log(f"🟢 Starting stat scrape for match {match_id} (once={once})", "INFO")

    cursor = conn.cursor()
    cursor.execute("SELECT round_id FROM matches WHERE match_id = ?", (match_id,))
    row = cursor.fetchone()
    round_id = row[0] if row else None
    if not round_id:
        log(f"❌ Could not resolve round_id for match {match_id}", "ERROR")
        return

    url = f"https://www.afl.com.au/afl/matches/{match_id}#player-stats"

    if once:
        html = load_page_with_playwright(url)
        match_status = get_match_status_from_header(html)
        stats = parse_live_stats(html, match_id, round_id, match_status)
        if stats:
            save_player_stats_to_db(stats, conn)
    else:
        while True:
            html = load_page_with_playwright(url)
            match_status = get_match_status_from_header(html)
            stats = parse_live_stats(html, match_id, round_id, match_status)
            if stats:
                save_player_stats_to_db(stats, conn)
                log_scrape_event(conn, match_id, round_id, match_status)

            if match_status == "COMPLETED":
                log(f"📆 Scraping and saving stats at: {now}", "DEBUG")
                log("✅ Match is completed. Stopping stat scraping.", "SUCCESS")
                break

            wait = random.randint(120, 240)
            log(f"⏱ Waiting {wait} seconds before next scrape...", "DEBUG")
            time.sleep(wait)

        conn.close()

def main():
    parser = argparse.ArgumentParser(description="Scrape live or completed AFL player stats.")
    parser.add_argument("--match-id", type=int, help="AFL match ID to scrape")
    parser.add_argument("--round-id", type=int, help="Round ID to scrape all matches")
    parser.add_argument("--once", action="store_true", help="Scrape only once (for completed match stats)")
    args = parser.parse_args()

    if not args.match_id and not args.round_id:
        log("❌ Must specify either --match-id or --round-id", "ERROR")
        return

    if args.match_id:
        run_scraper(args.match_id, once=args.once)
    elif args.round_id:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT match_id FROM matches
            WHERE round_id = ? AND start_time_utc IS NOT NULL
        """, (args.round_id,))
        matches = [row["match_id"] for row in cursor.fetchall()]
        conn.close()

        if not matches:
            log(f"⚠️ No matches found for round {args.round_id}", "WARN")
            return

        log(f"🔁 Scraping {len(matches)} matches for round {args.round_id}", "INFO")
        for mid in matches:
            run_scraper(mid, once=args.once)

if __name__ == "__main__":
    main()
