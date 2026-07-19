#db/init_db.py
import sqlite3
from pathlib import Path
from utils.log import log
from api_key_security import api_key_prefix, hash_api_key, is_hashed_api_key

DB_FILE = Path("data/afl_players.db")

def create_api_keys_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            api_key TEXT UNIQUE,
            key_hash TEXT UNIQUE,
            key_prefix TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )
    """)

    table_info = cursor.execute("PRAGMA table_info(api_keys)").fetchall()
    columns = {row[1] for row in table_info}
    api_key_column = next((row for row in table_info if row[1] == "api_key"), None)
    if api_key_column and api_key_column[3]:
        cursor.execute("ALTER TABLE api_keys RENAME TO api_keys_legacy_plaintext")
        cursor.execute("""
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                api_key TEXT UNIQUE,
                key_hash TEXT UNIQUE,
                key_prefix TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)
        legacy_columns = {row[1] for row in cursor.execute("PRAGMA table_info(api_keys_legacy_plaintext)").fetchall()}
        created_expr = "created_at" if "created_at" in legacy_columns else "CURRENT_TIMESTAMP"
        active_expr = "is_active" if "is_active" in legacy_columns else "1"
        cursor.execute(
            f"INSERT INTO api_keys (id, label, api_key, created_at, is_active) "
            f"SELECT id, label, api_key, {created_expr}, {active_expr} FROM api_keys_legacy_plaintext"
        )
        cursor.execute("DROP TABLE api_keys_legacy_plaintext")
        table_info = cursor.execute("PRAGMA table_info(api_keys)").fetchall()
        columns = {row[1] for row in table_info}
    if "key_hash" not in columns:
        cursor.execute("ALTER TABLE api_keys ADD COLUMN key_hash TEXT")
    if "key_prefix" not in columns:
        cursor.execute("ALTER TABLE api_keys ADD COLUMN key_prefix TEXT")
    if "is_active" not in columns:
        cursor.execute("ALTER TABLE api_keys ADD COLUMN is_active INTEGER DEFAULT 1")
    if "created_at" not in columns:
        cursor.execute("ALTER TABLE api_keys ADD COLUMN created_at TEXT")

    rows = cursor.execute("SELECT id, api_key, key_hash FROM api_keys").fetchall()
    for row in rows:
        key_id, plaintext_key, stored_hash = row
        if plaintext_key and not is_hashed_api_key(stored_hash):
            cursor.execute(
                "UPDATE api_keys SET key_hash = ?, key_prefix = ?, api_key = NULL WHERE id = ?",
                (hash_api_key(plaintext_key), api_key_prefix(plaintext_key), key_id),
            )

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
            jumper_number INTEGER,
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
