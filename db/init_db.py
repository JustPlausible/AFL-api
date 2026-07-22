# db/init_db.py
import sqlite3

from api_key_security import api_key_prefix, hash_api_key, is_hashed_api_key
from db.connection import get_db_path, validate_db_parent
from db.migration_runner import MigrationError, migrate_database
from utils.log import log


def create_api_keys_table(cursor):
    """Compatibility helper for existing callers; delegates schema upgrades to migrations.

    Tests and scripts historically imported this helper directly. Keep the helper
    focused on the api_keys table for in-memory/special-case callers, while normal
    application initialisation uses migrate_database().
    """
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
    for key_id, plaintext_key, stored_hash in rows:
        if plaintext_key and not is_hashed_api_key(stored_hash):
            cursor.execute(
                "UPDATE api_keys SET key_hash = ?, key_prefix = ?, api_key = NULL WHERE id = ?",
                (hash_api_key(plaintext_key), api_key_prefix(plaintext_key), key_id),
            )


def init_db():
    db_path = validate_db_parent(get_db_path())
    log(f"🧱 Migrating SQLite DB: {db_path}", "INFO")
    ran = migrate_database(db_path)
    if ran:
        log(f"✅ Applied SQLite migrations: {', '.join(ran)}", "SUCCESS")
    else:
        log("✅ SQLite database already up to date", "SUCCESS")


if __name__ == "__main__":
    try:
        init_db()
    except (MigrationError, OSError) as exc:
        log(f"❌ SQLite database initialisation failed: {exc}", "ERROR")
        raise SystemExit(1) from exc
