from utils.log import log
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

def fetch_injury_list_html():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://www.afl.com.au/matches/injury-list", wait_until="domcontentloaded")
        html_content = page.content()
        browser.close()
    return html_content

def parse_injury_list(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    injury_data = []

    # Assuming each team's injury list is within a specific container
    teams = soup.select('.injury-list__team')  # Adjust selector based on actual HTML structure

    for team in teams:
        club_name = team.select_one('.injury-list__team-name').get_text(strip=True)
        players = team.select('.injury-list__player')  # Adjust selector based on actual HTML structure

        for player in players:
            player_name = player.select_one('.injury-list__player-name').get_text(strip=True)
            injury_detail = player.select_one('.injury-list__player-injury').get_text(strip=True)
            return_date = player.select_one('.injury-list__player-return').get_text(strip=True)

            injury_data.append({
                'club': club_name,
                'player_name': player_name,
                'injury': injury_detail,
                'return_date': return_date
            })
    return injury_data

def match_players(injury_data, player_db):
    matched_injuries = []

    for injury in injury_data:
        for player in player_db:
            if (injury['player_name'].lower() == player['full_name'].lower() and
                injury['club'].lower() == player['club'].lower()):
                matched_injuries.append({
                    'afl_id': player['afl_id'],
                    'club': injury['club'],
                    'player_name': injury['player_name'],
                    'injury': injury['injury'],
                    'return_date': injury['return_date']
                })
                break
    return matched_injuries

def main():
    # Fetch the injury list HTML
    html_content = fetch_injury_list_html()
    if not html_content:
        log("[!] Failed to fetch injury list HTML", "ERROR")
        return

    # Parse the HTML to extract injury data
    injury_data = parse_injury_list(html_content)
    if not injury_data:
        log("[!] No injury data found", "ERROR")
        return
    print(f"Parsed {len(injury_data)} injuries")

    # Load player database (this should be replaced with actual player data loading logic)
#    player_db = [
#        {'afl_id': 1, 'full_name': 'Player One', 'club': 'Club A'},
#        {'afl_id': 2, 'full_name': 'Player Two', 'club': 'Club B'},
#        # Add more players as needed
#    ]

    # Match players with injury data
#    matched_injuries = match_players(injury_data, player_db)

    # Print or save the matched injuries
#    for injury in matched_injuries:
#        print(injury)
