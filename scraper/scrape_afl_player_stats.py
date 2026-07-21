# scraper/scrape_afl_player_stats.py

import time
import random
import re
import argparse
import sqlite3
import os
import sys
import socket
import json
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from utils.log import setup_logger
from utils.http_utils import load_page_with_playwright
from db.import_to_db import save_player_stats_to_db, log_scrape_event
from merge.helpers import extract_club_player_id, extract_champion_id
from db.connection import get_db_connection
from scraper.afl_selectors import PLAYER_STATS_SELECTORS

log = setup_logger("player_stats_scraper", "scrape_afl_player_stats.log")

log.debug("🔍 Environment check:")
log.debug(f"  CWD: {os.getcwd()}")
log.debug(f"  USER: {os.environ.get('USER')}")
log.debug(f"  PATH: {os.environ.get('PATH')}")
log.debug(f"  PYTHONPATH: {os.environ.get('PYTHONPATH')}")
log.debug(f"  Executable: {sys.executable}")
log.debug(f"  Args: {sys.argv}")

with open("data/clubs.json", "r", encoding="utf-8") as f:
    clubs = json.load(f)

# Build alias → code lookup map
alias_map = {}
for club in clubs:
    # Include official code
    alias_map[club["code"].upper()] = club["code"]
    
    # Include each alias
    for alias in club.get("aliases", []):
        alias_map[alias.upper()] = club["code"]

try:
    resolved = socket.gethostbyname("www.afl.com.au")
    log.info(f"🔗 DNS resolved: www.afl.com.au → {resolved}")
except socket.gaierror as e:
    log.error(f"⛔ DNS resolution failed: {e}")

def retry_load_page(url: str, retries: int = 3, delay: int = 8) -> str | None:
    for attempt in range(retries):
        try:
            html = load_page_with_playwright(url)
            if html:
                return html
        except Exception as e:
            log.warning(f"⚠️ Attempt {attempt + 1} failed: {e}")
        time.sleep(delay)
    log.error(f"❌ Failed to load page after {retries} attempts: {url}")
    return None

def get_match_status_from_header(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    label = soup.select_one(PLAYER_STATS_SELECTORS.MATCH_STATUS_LABEL)
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
    stats_table = soup.select_one(PLAYER_STATS_SELECTORS.STATS_TABLE)

    if not stats_table:
        log.error(f"[match {match_id}] ❌ Could not find player stats table.")
        return []

    headers = [
        th.text.strip().replace("%", "").replace("ToG", "ToG%")  # Normalise for field_map
        for th in stats_table.select(PLAYER_STATS_SELECTORS.HEADER_CELLS)
    ]
    log.debug(f"[match {match_id}] 📋 Detected headers: {headers}")

    # Map AFL stat labels to our database field names
    field_map = {
        "AF": "af_score",
        "G": "goals",
        "B": "behinds",
        "D": "disposals",
        "K": "kicks",
        "H": "handballs",
        "M": "marks",
        "T": "tackles",
        "HO": "hitouts",
        "CLR": "clearances",
        "MG": "metres_gained",
        "GA": "goal_assists",
        "ToG%": "time_on_ground_pct"
    }

    rows = stats_table.select(PLAYER_STATS_SELECTORS.BODY_ROWS)
    players = []

    for row in rows:
        cells = row.find_all(["th", "td"])
        if not cells or len(cells) < 2:  # must at least have jumper + name
            continue

        try:
            profile_link = row.select_one(PLAYER_STATS_SELECTORS.PLAYER_PROFILE_LINK)
            player_name = profile_link.get_text(" ", strip=True)
            profile_url = profile_link["href"]
            afl_id = extract_club_player_id(profile_url)

            img_url = row.select_one(PLAYER_STATS_SELECTORS.PLAYER_HEADSHOT)["src"]
            champion_id = extract_champion_id(img_url)

            jumper_span = row.select_one(PLAYER_STATS_SELECTORS.JUMPER_NUMBER)
            raw_team_code = jumper_span["class"][-1].upper() if jumper_span else "UNK"
            if raw_team_code not in alias_map:
                log.warning(f"[match {match_id}] ⚠️ Unknown team alias '{raw_team_code}', using raw value.")
            team_code = alias_map.get(raw_team_code, raw_team_code)  # fallback to raw_code if no match
            jumper_number = int(jumper_span.text.strip()) if jumper_span else None

            data = {
                "match_id": match_id,
                "round_id": round_id,
                "afl_id": afl_id,
                "champion_id": champion_id,
                "player_name": player_name,
                "team_code": team_code,
                "jumper_number": jumper_number,
                "status": status,
            }

            # Dynamically map only known headers
            for i in range(min(len(headers), len(cells))):
                header = headers[i]
                key = field_map.get(header)
                if not key:
                    continue

                raw = cells[i].text.strip().replace("%", "")
                try:
                    val = float(raw) if key == "time_on_ground_pct" else int(raw or 0)
                except ValueError:
                    log.warning(f"[match {match_id}] ⚠️ Could not convert '{raw}' for field {key}")
                    val = None
                data[key] = val

            players.append(data)

        except Exception as e:
            log.warning(f"[match {match_id}] ⚠️ Error parsing row: {e}")

    log.debug(f"[match {match_id}] ✅ Parsed {len(players)} player stat rows")
    return players

def run_scraper(match_id: int, once: bool = False):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    log.info(f"[match {match_id}] 🟢 Starting stat scrape for match {match_id} (once={once})")

    cursor = conn.cursor()
    cursor.execute("SELECT round_id FROM matches WHERE match_id = ?", (match_id,))
    row = cursor.fetchone()
    round_id = row[0] if row else None
    if not round_id:
        log.error(f"[match {match_id}] ❌ Could not resolve round_id for match {match_id}")
        return

    url = f"https://www.afl.com.au/afl/matches/{match_id}#player-stats"

    if once:
        html = retry_load_page(url)
        if not html:
            return
        match_status = get_match_status_from_header(html)
        stats = parse_live_stats(html, match_id, round_id, match_status)
        if stats:
            save_player_stats_to_db(stats, conn)
    else:
        while True:
            html = retry_load_page(url)
            if not html:
                return
            match_status = get_match_status_from_header(html)
            stats = parse_live_stats(html, match_id, round_id, match_status)
            if stats:
                save_player_stats_to_db(stats, conn)
                log_scrape_event(conn, match_id, round_id, match_status)

            if match_status == "COMPLETED":
                log.debug(f"[match {match_id}] 📆 Scraping and saving stats at: {now}")
                log.info("[match {match_id}] ✅ Match is completed. Stopping stat scraping.")
                break

            wait = random.randint(120, 240)
            log.debug(f"[match {match_id}] ⏱ Waiting {wait} seconds before next scrape...")
            time.sleep(wait)

        conn.close()

def main():
    parser = argparse.ArgumentParser(description="Scrape live or completed AFL player stats.")
    parser.add_argument("--match-id", type=int, help="AFL match ID to scrape")
    parser.add_argument("--round-id", type=int, help="Round ID to scrape all matches")
    parser.add_argument("--once", action="store_true", help="Scrape only once (for completed match stats)")
    args = parser.parse_args()

    if not args.match_id and not args.round_id:
        log.error("❌ Must specify either --match-id or --round-id")
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
            log.warning(f"⚠️ No matches found for round {args.round_id}")
            return

        log.info(f"🔁 Scraping {len(matches)} matches for round {args.round_id}")
        for mid in matches:
            run_scraper(mid, once=args.once)

if __name__ == "__main__":
    main()
