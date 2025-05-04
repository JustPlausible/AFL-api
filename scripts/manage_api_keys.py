import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path("data/afl_players.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            api_key TEXT NOT NULL UNIQUE
        )
    """)
    return conn

def add_api_key(label: str, key: str):
    conn = get_connection()
    try:
        conn.execute("INSERT INTO api_keys (label, api_key) VALUES (?, ?)", (label, key))
        conn.commit()
        print(f"✅ Added API key for '{label}'")
    except sqlite3.IntegrityError:
        print(f"⚠️ API key already exists for label '{label}' or key is not unique")
    finally:
        conn.close()

def list_api_keys(show_full=False):
    conn = get_connection()
    cursor = conn.execute("SELECT id, label, api_key FROM api_keys ORDER BY id")
    rows = cursor.fetchall()
    if not rows:
        print("ℹ️ No API keys found.")
    else:
        print("🔑 Registered API Keys:")
        for row in rows:
            visible_key = row[2] if show_full else row[2][:4] + "..." + row[2][-2:]
            print(f"  [{row[0]}] {row[1]} → {visible_key}")
    conn.close()

def remove_api_key(key: str):
    conn = get_connection()
    cursor = conn.execute("DELETE FROM api_keys WHERE api_key = ?", (key,))
    conn.commit()
    if cursor.rowcount:
        print(f"🗑️ Removed API key: {key}")
    else:
        print(f"⚠️ API key not found: {key}")
    conn.close()

def main():
    parser = argparse.ArgumentParser(description="Manage AFL API keys")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", nargs=2, metavar=("LABEL", "KEY"), help="Add new API key")
    group.add_argument("--remove", metavar="KEY", help="Remove API key")
    group.add_argument("--list", action="store_true", help="List all API keys")
    parser.add_argument("--show", action="store_true", help="Show full keys (only applies with --list)")

    args = parser.parse_args()

    if args.add:
        label, key = args.add
        add_api_key(label, key)
    elif args.remove:
        remove_api_key(args.remove)
    elif args.list:
        list_api_keys(show_full=args.show)

if __name__ == "__main__":
    main()
