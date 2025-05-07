import sqlite3
from pathlib import Path

# Define the path to the SQLite database
db_path = Path("data/afl_players.db")
db_path.parent.mkdir(parents=True, exist_ok=True)

# Connect to the SQLite database (will create if it doesn't exist)
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Create the `api_keys` table if it doesn't exist
cursor.execute("""
    CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        api_key TEXT NOT NULL UNIQUE,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        is_active INTEGER DEFAULT 1
    )
""")

# Insert sample keys only if they don't exist
sample_keys = [
    ("gas-script", "abc123"),
    ("internal-test", "def456")
]

for label, key in sample_keys:
    cursor.execute("""
        INSERT OR IGNORE INTO api_keys (label, api_key)
        VALUES (?, ?)
    """, (label, key))

# Commit and close connection
conn.commit()
conn.close()
