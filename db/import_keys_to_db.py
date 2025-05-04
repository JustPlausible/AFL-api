import sqlite3
from pathlib import Path

# Define the path to the SQLite database
db_path = Path("data/afl_players.db")
db_path.parent.mkdir(parents=True, exist_ok=True)

# Connect to the SQLite database (will create if it doesn't exist)
conn = sqlite3.connect(db_path)

# Create the `api_keys` table with label, key, and optional metadata
conn.execute("""
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    api_key TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    is_active INTEGER DEFAULT 1
)
""")

# Insert some sample API keys
sample_keys = [
    ("gas-script", "abc123"),
    ("internal-test", "def456")
]

# Insert sample keys only if they don't exist
for label, key in sample_keys:
    conn.execute("""
    INSERT OR IGNORE INTO api_keys (label, api_key)
    VALUES (?, ?)
    """, (label, key))

# Commit and close connection
conn.commit()
conn.close()
