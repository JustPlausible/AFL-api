"""Add extensible consumer capabilities and optional per-key rate-limit metadata."""

from api_key_capabilities import STANDARD_READ

MIGRATION_ID = "0015"
DESCRIPTION = "Add API key capabilities and optional rate-limit metadata"


def migrate(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(api_keys)")}
    if "rate_limit_per_minute" not in columns:
        conn.execute("ALTER TABLE api_keys ADD COLUMN rate_limit_per_minute INTEGER")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_key_capabilities (
            api_key_id INTEGER NOT NULL,
            capability TEXT NOT NULL,
            granted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (api_key_id, capability),
            FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
        )
    """)
    # Preserve current read access while defaulting all upgraded credentials
    # away from privileged/advanced metadata.
    conn.execute(
        "INSERT OR IGNORE INTO api_key_capabilities (api_key_id, capability) "
        "SELECT id, ? FROM api_keys",
        (STANDARD_READ,),
    )
