import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone
from utils.log import log
from utils.club_lookup import load_clubs

DB_FILE = Path("data/afl_players.db")
DATA_DIR = Path("data")

def resolve_club_code_from_name(club_name: str) -> str | None:
    """
    Given a full club name (e.g. 'Gold Coast Suns'), return the short club code (e.g. 'GCS').
    """
    clubs = load_clubs()
    name_clean = club_name.strip().lower()

    for club in clubs:
        if club['name'].strip().lower() == name_clean:
            return club['code']
    
    return None

def import_players():
    if not DB_FILE.exists():
        log("❌ Database not found. Run init_db.py first.", "ERROR")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    enriched_files = sorted(f for f in DATA_DIR.glob("players-*.json") if "-raw" not in f.stem)
    total_imported = 0

    for file in enriched_files:
        with file.open() as f:
            players = json.load(f)

        for player in players:
            cursor.execute("""
                INSERT INTO players (
                    afl_id, full_name, first_name, last_name,
                    nickname, formatted_nickname, formatted_last_name,
                    club, guernsey, position, club_profile_url, image_url,
                    club_player_id, afl_url, champion_data_id, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(afl_id) DO UPDATE SET
                    full_name=excluded.full_name,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    nickname=excluded.nickname,
                    formatted_nickname=excluded.formatted_nickname,
                    formatted_last_name=excluded.formatted_last_name,
                    club=excluded.club,
                    guernsey=excluded.guernsey,
                    position=excluded.position,
                    club_profile_url=excluded.club_profile_url,
                    image_url=excluded.image_url,
                    club_player_id=excluded.club_player_id,
                    afl_url=excluded.afl_url,
                    champion_data_id=excluded.champion_data_id,
                    last_updated=excluded.last_updated
            """, (
                player.get("afl_id"),
                player.get("full_name"),
                player.get("first_name"),
                player.get("last_name"),
                player.get("nickname"),
                player.get("formatted_nickname"),
                player.get("formatted_last_name"),
                resolve_club_code_from_name(player.get("club")),
                player.get("guernsey"),
                player.get("position"),
                player.get("club_profile_url"),
                player.get("image_url"),
                player.get("club_player_id"),
                player.get("afl_url"),
                player.get("champion_data_id"),
                datetime.now(timezone.utc).isoformat()
            ))

        total_imported += len(players)
        log(f"📥 Imported {len(players)} players from {file.name}", "INFO")

    conn.commit()
    conn.close()
    log(f"✅ Total players processed: {total_imported}", "SUCCESS")

if __name__ == "__main__":
    import_players()
