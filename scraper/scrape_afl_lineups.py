# scraper/scrape_afl_lineups.py

import argparse
import re
import sqlite3
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

from db.connection import get_db_connection
from utils.log import setup_logger

log = setup_logger("lineup_scraper", "scrape_afl_lineups.log")


class MatchRoundResolutionError(RuntimeError):
    """Raised when explicit --match mode cannot resolve a match to a round."""


def extract_afl_id(href: str) -> int | None:
    m = re.search(r"/players/(\d+)", href)
    return int(m.group(1)) if m else None

def parse_lineups_html(html, round_number):
    soup = BeautifulSoup(html, "html.parser")
    all_players = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for match in soup.select('div.team-lineups__item'):
        # Extract match_id directly from match page link
        header_link = match.select_one('a.team-lineups-header')
        match_id = None
        if header_link:
            href = header_link.get('href')
            if href:
                m = re.search(r'/matches/(\d+)', href)
                if m:
                    match_id = int(m.group(1))
        if not match_id:
            log.warning("⚠️ No match link or match_id found for this match block.")
            continue

        # --- Team/venue extraction ---
        header = match.select_one('.team-lineups-header__name')
        if not header:
            log.warning("⚠️ Could not find match header; skipping match.")
            continue
        header_text = header.get_text(" ", strip=True)
        if " v " not in header_text:
            log.warning(f"⚠️ Could not split team names: {header_text}")
            continue
        home_team, away_team = [x.strip() for x in header_text.split(" v ")]

        # IN/OUT/SUB grids (home & away)
        for status in ['in', 'out', 'sub']:
            grid_home = match.select(f'.team-lineups-ins-and-outs__grid--home .team-lineups-ins-and-outs__grid-item')
            grid_away = match.select(f'.team-lineups-ins-and-outs__grid--away .team-lineups-ins-and-outs__grid-item')
            for tag in grid_home:
                player_name = tag.select_one('.team-lineups-ins-and-outs__player-name')
                if not player_name:
                    continue
                href = tag.get('href')
                all_players.append({
                    "round_number": round_number,
                    "match_id": match_id,
                    "afl_id": extract_afl_id(href),
                    "first_name": player_name.get_text(strip=True).split()[0],
                    "surname": player_name.get_text(strip=True).split()[-1],
                    "team": home_team,
                    "position_group": status.upper(),
                    "champion_id": None,
                    "scraped_at": now_iso
                })
            for tag in grid_away:
                player_name = tag.select_one('.team-lineups-ins-and-outs__player-name')
                if not player_name:
                    continue
                href = tag.get('href')
                all_players.append({
                    "round_number": round_number,
                    "match_id": match_id,
                    "afl_id": extract_afl_id(href),
                    "first_name": player_name.get_text(strip=True).split()[0],
                    "surname": player_name.get_text(strip=True).split()[-1],
                    "team": away_team,
                    "position_group": status.upper(),
                    "champion_id": None,
                    "scraped_at": now_iso
                })

        # On-field/interchange/substitute players
        for entry in match.select('a.team-lineups__player-entry'):
            href = entry.get('href')
            name_first = entry.select_one('.team-lineups__player-entry--name-first')
            name_last = entry.select_one('.team-lineups__player-entry--name-last')
            team = home_team if 'team-lineups__player-entry--home-team' in entry.get('class', []) else away_team

            all_players.append({
                "round_number": round_number,
                "match_id": match_id,
                "afl_id": extract_afl_id(href),
                "first_name": name_first.get_text(strip=True) if name_first else None,
                "surname": name_last.get_text(strip=True) if name_last else None,
                "team": team,
                "position_group": "ONFIELD",  # or you could parse more detail from aria-label
                "champion_id": None,
                "scraped_at": now_iso
            })

    log.info(f"🏁 Finished scrape. Total players extracted: {len(all_players)}")
    return all_players

def scrape_team_lineups(round_number: int = 0):
    """
    Drop-in replacement for previous scraper.
    - Loads AFL lineups page (optionally navigates to specific round)
    - Expands all lineups
    - Scrapes player data and matches to your 'matches' table in SQLite
    - Returns: list of dicts [{match_id, afl_id, first_name, surname, team, position_group, champion_id, scraped_at}, ...]
    """
    url = "https://www.afl.com.au/matches/team-lineups"
    log.info(f"🎭 Launching Playwright browser to scrape AFL Team Line-ups for Round {round_number}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=60000)
        page.wait_for_selector('.competition-nav__round-list')

        if round_number:
            # Try to click the button for the desired round by round_id (will need to map round_number → round_id if available)
            btn = page.query_selector(f'li[data-round-id="{round_number}"] button')
            if btn:
                btn.click()
        # Expand all lineups
        try:
            page.wait_for_selector('label[for="expand-lineups-toggle"]', state='visible', timeout=10000)
            label = page.query_selector('label[for="expand-lineups-toggle"]')
            if label:
                label.click()
                log.info("Clicked expand-all label to expand all lineups.")
            else:
                log.warning("Expand all label not found or not visible.")
        except Exception as e:
            log.warning(f"⚠️ Could not click expand all label: {e}")

        # Wait for all matches to be expanded and visible
        page.wait_for_selector('.team-lineups__item', timeout=15000)
        html = page.content()
        browser.close()

    players = parse_lineups_html(html, round_number)
    if players:
        log.info(f"🧾 Sample player: {players[0]}")
    else:
        log.warning("⚠️ No players found.")
    return players


def get_round_for_match(match_id: int) -> int:
    """Return the round containing a match, or raise for explicit --match failures."""
    try:
        conn = get_db_connection()
    except (FileNotFoundError, OSError) as exc:
        raise MatchRoundResolutionError(
            f"could not open fixture database to resolve match {match_id}: {exc}"
        ) from exc

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT round_id FROM matches WHERE match_id = ?", (match_id,))
        row = cursor.fetchone()
    except sqlite3.DatabaseError as exc:
        raise MatchRoundResolutionError(
            f"could not read fixture data to resolve match {match_id}: {exc}"
        ) from exc
    finally:
        conn.close()

    if row is None:
        raise MatchRoundResolutionError(f"could not resolve match {match_id} to a round")
    return int(row[0])


def scrape_match_lineup(match_id: int):
    """Scrape lineup data for a single match only."""
    round_number = get_round_for_match(match_id)
    log.info(f"🎯 Scraping line-up for match {match_id} via Round {round_number}")
    players = scrape_team_lineups(round_number=round_number)

    match_players = [player for player in players if int(player.get("match_id")) == match_id]
    if not match_players:
        log.warning(f"⚠️ No lineup players found for match {match_id}.")
    return match_players


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer: {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m scraper.scrape_afl_lineups",
        description="Scrape AFL team lineups by round or match.",
    )
    parser.add_argument("--round", dest="round_number", type=positive_int, help="round number to scrape")
    parser.add_argument("--match", dest="match_id", type=positive_int, help="match ID to scrape")
    parser.add_argument(
        "positional_round",
        nargs="?",
        type=positive_int,
        metavar="ROUND",
        help="round number to scrape (deprecated; use --round)",
    )
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    selectors = [
        args.round_number is not None,
        args.match_id is not None,
        args.positional_round is not None,
    ]
    if sum(selectors) > 1:
        parser.error("choose only one lineup selector: --round, --match, or positional ROUND")
    return args


def run_from_args(args):
    if args.match_id is not None:
        return scrape_match_lineup(match_id=args.match_id)
    if args.round_number is not None:
        return scrape_team_lineups(round_number=args.round_number)
    if args.positional_round is not None:
        return scrape_team_lineups(round_number=args.positional_round)
    return scrape_team_lineups(round_number=0)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        run_from_args(args)
    except MatchRoundResolutionError as exc:
        log.error(f"❌ Explicit match scrape failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
