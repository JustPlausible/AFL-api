"""Upgrade legacy API key schemas and hash stored plaintext keys.

Temporary compatibility note: this migration accepts legacy plaintext schemas from
pre-hashing releases so deployments can upgrade in place. It is transactional and
idempotent; future API-key schema changes should be new migrations.
"""
from api_key_security import api_key_prefix, hash_api_key, is_hashed_api_key

MIGRATION_ID = "0003"
DESCRIPTION = "Upgrade legacy API key schema and hash plaintext keys"


def _table_info(conn):
    return conn.execute("PRAGMA table_info(api_keys)").fetchall()


def _columns(conn):
    return {row[1] for row in _table_info(conn)}


def migrate(conn):
    table_info = _table_info(conn)
    api_key_column = next((row for row in table_info if row[1] == "api_key"), None)
    if api_key_column and api_key_column[3]:
        conn.execute("ALTER TABLE api_keys RENAME TO api_keys_legacy_plaintext")
        conn.execute("""
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
        legacy_columns = {row[1] for row in conn.execute("PRAGMA table_info(api_keys_legacy_plaintext)")}
        created_expr = "created_at" if "created_at" in legacy_columns else "CURRENT_TIMESTAMP"
        active_expr = "is_active" if "is_active" in legacy_columns else "1"
        conn.execute(
            f"INSERT INTO api_keys (id, label, api_key, created_at, is_active) "
            f"SELECT id, label, api_key, {created_expr}, {active_expr} FROM api_keys_legacy_plaintext"
        )
        conn.execute("DROP TABLE api_keys_legacy_plaintext")
    cols = _columns(conn)
    if "key_hash" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN key_hash TEXT")
    if "key_prefix" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN key_prefix TEXT")
    if "is_active" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN is_active INTEGER DEFAULT 1")
    if "created_at" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN created_at TEXT")
    for key_id, plaintext_key, stored_hash in conn.execute("SELECT id, api_key, key_hash FROM api_keys").fetchall():
        if plaintext_key and not is_hashed_api_key(stored_hash):
            conn.execute(
                "UPDATE api_keys SET key_hash = ?, key_prefix = ?, api_key = NULL WHERE id = ?",
                (hash_api_key(plaintext_key), api_key_prefix(plaintext_key), key_id),
            )
