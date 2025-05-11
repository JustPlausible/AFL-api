from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import re
from utils.log import log
from utils.afl_urls import get_lineups_url

def extract_afl_id(href: str) -> int | None:
    match = re.search(r"/players/(\d+)/", href)
    return int(match.group(1)) if match else None

def scrape_team_lineups(round_number: int = 0):
    url = f"{get_lineups_url()}?GameWeeks={round_number}"
    log(f"🎭 Launching Playwright browser to scrape AFL Team Line-ups for Round {round_number}...")

    all_players = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=60000)

        page.wait_for_selector("div.match-list-alt__item", timeout=15000)
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    match_blocks = soup.select("div.match-list-alt__item")
    log(f"🔍 Found {len(match_blocks)} match blocks")

    for match in match_blocks:
        match_id = match.get("data-match-provider-id")
        match_slug = match.get("id")
        if not match_id or not match_slug:
            log("⚠️ Skipping match with missing match_id or slug")
            continue

        # Extract full team names from span elements
        team_names = match.select("span.team-lineups__team-name")
        if len(team_names) < 2:
            log("⚠️ Skipping match with missing team name labels")
            continue

        home_team = team_names[0].get_text(strip=True)
        away_team = team_names[1].get_text(strip=True)
        log(f"📊 Match {match_id}: {home_team} vs {away_team}")

        # 🔁 Scrape pre-lineup IN/OUT/SUB players from the summary block
        content_section = match.select_one("div.match-list-alt__content")
        if content_section:
            for row in content_section.select("div.team-lineups__row"):
                label_span = row.select_one("div.team-lineups__meta span.team-lineups__meta-label")
                if not label_span:
                    continue

                status_label = label_span.get_text(strip=True).upper()
                if status_label not in ["IN", "OUT", "SUB"]:
                    continue  # skip other rows (MILESTONE, etc.)

                players_home = row.select("div.team-lineups__players--home a.team-lineups__link")
                players_away = row.select("div.team-lineups__players:not(.team-lineups__players--home) a.team-lineups__link")

                log(f"📌 {status_label} – {len(players_home)} home, {len(players_away)} away")

                for tag in players_home:
                    player = {
                        "match_id": match_id,
                        "afl_id": extract_afl_id(tag['href']),
                        "champion_id": tag.get('data-player-id'),
                        "first_name": tag.get('data-first-name'),
                        "surname": tag.get('data-surname'),
                        "team": home_team,
                        "position_group": status_label
                    }
                    all_players.append(player)
                    log(f"  🔵 {player['first_name']} {player['surname']} ({status_label})")

                for tag in players_away:
                    player = {
                        "match_id": match_id,
                        "afl_id": extract_afl_id(tag['href']),
                        "champion_id": tag.get('data-player-id'),
                        "first_name": tag.get('data-first-name'),
                        "surname": tag.get('data-surname'),
                        "team": away_team,
                        "position_group": status_label
                    }
                    all_players.append(player)
                    log(f"  ⚫ {player['first_name']} {player['surname']} ({status_label})")

        # Store previously named players by match and surname
        named_players_by_match = {}

        # Go through structured positional rows
        for row in match.select("div.team-lineups__positions-row"):
            containers = row.select("div.team-lineups__positions-players-container")

            for container in containers:
                is_home = "team-lineups__positions-players-container--home" in container.get("class", [])
                team_name = home_team if is_home else away_team

                pos_label = container.select_one("span.team-lineups__position-meta-label")
                position_group = pos_label.get_text(strip=True) if pos_label else "UNKNOWN"

                player_tags = container.select("div.team-lineups__positions-players a.team-lineups__link")
                log(f"🎯 {team_name} - {position_group}: {len(player_tags)} players")

                for tag in player_tags:
                    surname = tag.get("data-surname")
                    first_name = tag.get("data-first-name")
                    player = {
                        "match_id": match_id,
                        "afl_id": extract_afl_id(tag['href']),
                        "champion_id": tag.get('data-player-id'),
                        "first_name": first_name,
                        "surname": surname,
                        "team": team_name,
                        "position_group": position_group
                    }
                    all_players.append(player)
                    log(f"  ✅ {player['first_name']} {player['surname']} ({player['position_group']}, AFL ID: {player['afl_id']})")

                    # Add to lookup by match and surname
                    if match_id not in named_players_by_match:
                        named_players_by_match[match_id] = {}
                    named_players_by_match[match_id][surname.upper()] = player  # uppercase for matching

                # Check for LATE OUTS in team-lineups__meta--late-changes
                late_change_sections = content_section.select("div.team-lineups__meta--late-changes")
                for late_block in late_change_sections:
                    player_spans = late_block.select("div.team-lineups__players span.team-lineups__player")
                    for player_span in player_spans:
                        text = player_span.get_text(strip=True)
                        if "OUTS:" in text:
                            outs_match = re.search(r"OUTS:\s*(.+)", text)
                            if outs_match:
                                outs_list = outs_match.group(1)
                                names = re.split(r",\s*|\s+and\s+", outs_list)

                                for shortname in names:
                                    shortname = re.sub(r"\(.*?\)", "", shortname).strip()  # Remove (Injured), etc.
                                    if not shortname:
                                        continue
                                    parts = shortname.split(".")
                                    if len(parts) != 2:
                                        continue  # Unexpected format

                                    initial = parts[0].strip().upper()
                                    surname = parts[1].strip().upper()

                                    if surname in named_players_by_match.get(match_id, {}):
                                        player = named_players_by_match[match_id][surname]
                                        player["position_group"] = "OUT (Late)"
                                        log(f"🟠 Late OUT flagged: {player['first_name']} {player['surname']} ({match_id})")
                                    else:
                                        log(f"⚠️ Late OUT '{shortname}' not matched to known line-up in match {match_id}", "WARN")


    log(f"🏁 Finished scrape. Total players extracted: {len(all_players)}")
    return all_players

if __name__ == "__main__":
    import sys

    round_number = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    players = scrape_team_lineups(round_number=round_number)

    log(f"🧾 Sample player: {players[0] if players else 'No players found.'}")
