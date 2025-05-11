#db/init_db.py
import sqlite3
from pathlib import Path
from utils.log import log

DB_FILE = Path("data/afl_players.db")

def create_api_keys_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            api_key TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )
    """)

def create_clubs_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clubs (
            code TEXT PRIMARY KEY,
            name TEXT,
            slug TEXT,
            website TEXT,
            squad_url TEXT,
            aliases TEXT
        )
    """)

def create_players_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS players (
            afl_id INTEGER PRIMARY KEY,
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
        )
    """)

def create_rounds_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rounds (
            round_id INTEGER PRIMARY KEY,
            round_label TEXT,
            season_id INTEGER,
            competition_id INTEGER,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

def create_matches_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            match_id INTEGER PRIMARY KEY,
            match_provider_id TEXT,
            round_id INTEGER NOT NULL,
            home_team TEXT,
            away_team TEXT,
            venue TEXT,
            status TEXT,
            start_time_utc TEXT,
            score_home INTEGER,
            score_away INTEGER,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

def create_injuries_table(cursor):
    cursor.execute("""
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

def create_lineups_table(cursor):
    cursor.execute("""
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

def create_player_stats_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS player_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            round_id INTEGER,
            afl_id INTEGER,
            champion_id TEXT,
            player_name TEXT NOT NULL,
            team_code TEXT NOT NULL,
            af_score INTEGER,
            goals INTEGER,
            behinds INTEGER,
            disposals INTEGER,
            kicks INTEGER,
            handballs INTEGER,
            marks INTEGER,
            tackles INTEGER,
            hitouts INTEGER,
            clearances INTEGER,
            metres_gained INTEGER,
            goal_assists INTEGER,
            time_on_ground_pct REAL,
            status TEXT CHECK(status IN ('LIVE', 'COMPLETED')) NOT NULL,
            scraped_at TEXT NOT NULL,
            UNIQUE(match_id, afl_id)
        )
    """)

def create_scrape_log_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scrape_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            round_id INTEGER,
            status TEXT,
            scraped_at TEXT NOT NULL
        )
    """)

def create_scrape_summary_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scrape_summary (
            match_id INTEGER PRIMARY KEY,
            round_id INTEGER,
            total_scrapes INTEGER,
            first_scraped TEXT,
            last_scraped TEXT,
            completed_scrape BOOLEAN DEFAULT 0,
            notes TEXT
        )
    """)

def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    log("🧱 Creating tables in SQLite DB...", "INFO")

    create_players_table(cursor)
    create_injuries_table(cursor)
    create_api_keys_table(cursor)
    create_lineups_table(cursor)
    create_rounds_table(cursor)
    create_matches_table(cursor)
    create_clubs_table(cursor)
    create_player_stats_table(cursor)
    create_scrape_log_table(cursor)
    create_scrape_summary_table(cursor)
    # Add more create_*_table calls here as needed

    conn.commit()
    conn.close()
    log("✅ SQLite database initialised successfully", "SUCCESS")

if __name__ == "__main__":
    init_db()
