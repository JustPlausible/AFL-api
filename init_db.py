import sqlite3
from pathlib import Path

DB_FILE = Path("data/afl_players.db")

schema = """
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
    last_updated TEXT
);
"""

def init_database():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.executescript(schema)
    conn.commit()
    conn.close()
    print(f"✅ SQLite database initialised at: {DB_FILE}")

if __name__ == "__main__":
    init_database()
