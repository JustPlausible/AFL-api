import argparse
import sqlite3
from pathlib import Path

from api_key_security import api_key_prefix, generate_api_key, hash_api_key, verify_api_key_hash
from db.init_db import create_api_keys_table

DB_PATH = Path("data/afl_players.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    create_api_keys_table(conn.cursor())
    conn.commit()
    return conn


def add_api_key(label: str):
    full_key = generate_api_key()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO api_keys (label, api_key, key_hash, key_prefix) VALUES (?, NULL, ?, ?)",
            (label, hash_api_key(full_key), api_key_prefix(full_key)),
        )
        conn.commit()
        print(f"✅ Added API key for '{label}'")
        print("Copy this API key now. It will not be shown again:")
        print(full_key)
    except sqlite3.IntegrityError:
        print(f"⚠️ API key already exists for label '{label}' or generated key is not unique")
    finally:
        conn.close()


def list_api_keys():
    conn = get_connection()
    cursor = conn.execute("SELECT id, label, key_prefix, is_active FROM api_keys ORDER BY id")
    rows = cursor.fetchall()
    if not rows:
        print("ℹ️ No API keys found.")
    else:
        print("🔑 Registered API Keys:")
        for row in rows:
            status = "active" if row[3] else "inactive"
            print(f"  [{row[0]}] {row[1]} → prefix:{row[2] or 'unavailable'} ({status})")
    conn.close()


def remove_api_key(key: str):
    conn = get_connection()
    rows = conn.execute("SELECT id, key_hash FROM api_keys").fetchall()
    matching_id = next((row[0] for row in rows if verify_api_key_hash(key, row[1])), None)
    if matching_id is not None:
        cursor = conn.execute("DELETE FROM api_keys WHERE id = ?", (matching_id,))
    else:
        cursor = conn.execute("DELETE FROM api_keys WHERE label = ?", (key,))
    conn.commit()
    if cursor.rowcount:
        print("🗑️ Removed API key")
    else:
        print("⚠️ API key not found")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Manage AFL API keys")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", metavar="LABEL", help="Add new API key and show it once")
    group.add_argument("--remove", metavar="KEY_OR_LABEL", help="Remove API key by presented key or label")
    group.add_argument("--list", action="store_true", help="List all API key prefixes")

    args = parser.parse_args()

    if args.add:
        add_api_key(args.add)
    elif args.remove:
        remove_api_key(args.remove)
    elif args.list:
        list_api_keys()


if __name__ == "__main__":
    main()
