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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            afl_id INTEGER UNIQUE NOT NULL,
            full_name TEXT,
            first_name TEXT,
            last_name TEXT,
            nickname TEXT,
            formatted_nickname TEXT,
            formatted_last_name TEXT,
            club TEXT,
            guernsey INTEGER,
            position TEXT,
            club_profile_url TEXT,
            image_url TEXT,
            club_player_id INTEGER,
            afl_url TEXT,
            champion_data_id TEXT,
            source TEXT,
            scraped_at TEXT,
            resolved_at TEXT,
            last_updated TEXT
        )
    """)

    enriched_files = sorted(f for f in DATA_DIR.glob("players-*.json") if "-raw" not in f.stem)
    total_imported = 0

    for file in enriched_files:
        with file.open() as f:
            players = json.load(f)

        for player in players:
            if not player.get("afl_id"):
                log(f"⚠️ Skipping player without afl_id: {player.get('full_name', 'Unknown')} in file {file.name}", "WARN")
                continue
            cursor.execute("""
                INSERT INTO players (
                    afl_id, full_name, first_name, last_name,
                    nickname, formatted_nickname, formatted_last_name,
                    club, guernsey, position, club_profile_url, image_url,
                    club_player_id, afl_url, champion_data_id,
                    source, scraped_at, resolved_at, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(afl_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    nickname = excluded.nickname,
                    formatted_nickname = excluded.formatted_nickname,
                    formatted_last_name = excluded.formatted_last_name,
                    club = excluded.club,
                    guernsey = excluded.guernsey,
                    position = excluded.position,
                    club_profile_url = excluded.club_profile_url,
                    image_url = excluded.image_url,
                    club_player_id = excluded.club_player_id,
                    afl_url = excluded.afl_url,
                    champion_data_id = excluded.champion_data_id,
                    source = excluded.source,
                    scraped_at = excluded.scraped_at,
                    resolved_at = excluded.resolved_at,
                    last_updated = excluded.last_updated
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
                player.get("club_id") or player.get("club_player_id"),
                player.get("afl_url"),
                player.get("champion_data_id"),
                player.get("source"),
                player.get("scraped_at"),
                player.get("resolved_at"),
                datetime.now(timezone.utc).isoformat()
            ))

        total_imported += len(players)
        log(f"📥 Imported {len(players)} players from {file.name}", "INFO")

    conn.commit()
    conn.close()
    log(f"✅ Total players processed: {total_imported}", "SUCCESS")

def save_injuries_to_db(data: dict, conn: sqlite3.Connection):
    """
    Saves the scraped injury data to the 'injuries' table in the database.
    Expects data as { "ADE": { "updated": "Date", "players": [ ... ] }, ... }
    """
    cur = conn.cursor()

    # Create the table if it doesn't exist
    cur.execute("""
        CREATE TABLE IF NOT EXISTS injuries (
            afl_id INTEGER NOT NULL,
            club TEXT NOT NULL,
            player_name TEXT NOT NULL,
            injury TEXT,
            return_info TEXT,
            updated TEXT,
            first_updated TEXT,
            source TEXT,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
            current INTEGER DEFAULT 1,
            UNIQUE(afl_id, updated)
        )
    """)

    # Track all currently listed injuries
    currently_listed_ids = set()

    for team in data["teams"]:
        club = team["club"]
        updated = team.get("updated", "")
        for player in team["players"]:
            if not player["afl_id"]:
                raise ValueError(f"Missing AFL ID for player {player['name']} from {club}")

            afl_id = player["afl_id"]
            currently_listed_ids.add(afl_id)

            cur.execute("""
                INSERT INTO injuries (
                    afl_id, club, player_name, injury, return_info, updated, first_updated, source, scraped_at, current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(afl_id, updated) DO UPDATE SET
                    club = excluded.club,
                    player_name = excluded.player_name,
                    injury = excluded.injury,
                    return_info = excluded.return_info,
                    source = excluded.source,
                    scraped_at = excluded.scraped_at,
                    current = 1
            """, (
                player["afl_id"],
                club,
                player["name"],
                player["injury"],
                player["return"],
                updated,
                updated,  # first_updated = same as updated if new
                data["source"],
                data["scraped_at"],
            ))

    # Mark previous entries as no longer current
    if currently_listed_ids:
        placeholders = ",".join("?" for _ in currently_listed_ids)
        cur.execute(f"""
            UPDATE injuries
            SET current = 0
            WHERE current = 1 AND afl_id NOT IN ({placeholders})
        """, tuple(currently_listed_ids))

    conn.commit()
    log(f"💾 Injury data saved for {len(data['teams'])} teams", "INFO")

def save_lineups_to_db(players: list[dict], conn: sqlite3.Connection, round_number: int):
    """
    Saves scraped lineup data to the 'lineups' table in the database.
    Expects a flat list of player dicts, each with match_id, afl_id, etc.
    """
    cur = conn.cursor()

    # Create table if it doesn't exist
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lineups (
            round_number INTEGER NOT NULL,
            match_id TEXT NOT NULL,
            afl_id INTEGER NOT NULL,
            first_name TEXT,
            surname TEXT,
            team TEXT,
            position_group TEXT,
            champion_id TEXT,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (match_id, afl_id)
        )
    """)

    now = datetime.now(timezone.utc).isoformat()

    inserted = 0
    for player in players:
        try:
            cur.execute("""
                INSERT INTO lineups (
                    round_number, match_id, afl_id, first_name, surname,
                    team, position_group, champion_id, scraped_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(match_id, afl_id) DO UPDATE SET
                    round_number = excluded.round_number,
                    first_name = excluded.first_name,
                    surname = excluded.surname,
                    team = excluded.team,
                    position_group = excluded.position_group,
                    champion_id = excluded.champion_id,
                    scraped_at = excluded.scraped_at
            """, (
                round_number,
                player["match_id"],
                player["afl_id"],
                player.get("first_name"),
                player.get("surname"),
                player.get("team"),
                player.get("position_group"),
                player.get("champion_id"),
                now
            ))
            inserted += 1
        except Exception as e:
            log(f"❌ Failed to insert player {player['first_name']} {player['surname']}: {e}", "ERROR")

    conn.commit()
    log(f"💾 Saved {inserted} player lineups to DB", "SUCCESS")

if __name__ == "__main__":
    import_players()
