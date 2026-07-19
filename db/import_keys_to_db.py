import sqlite3
from pathlib import Path

from api_key_security import api_key_prefix, generate_api_key, hash_api_key
from db.init_db import create_api_keys_table

# Define the path to the SQLite database
db_path = Path("data/afl_players.db")
db_path.parent.mkdir(parents=True, exist_ok=True)

# Connect to the SQLite database (will create if it doesn't exist)
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
create_api_keys_table(cursor)

# Create a sample development key only if no keys exist. The full key is shown once.
existing_count = cursor.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
if existing_count == 0:
    full_key = generate_api_key()
    cursor.execute(
        "INSERT INTO api_keys (label, api_key, key_hash, key_prefix) VALUES (?, NULL, ?, ?)",
        ("development", hash_api_key(full_key), api_key_prefix(full_key)),
    )
    print("Created development API key. Copy it now; it will not be shown again:")
    print(full_key)

# Commit and close connection
conn.commit()
conn.close()
