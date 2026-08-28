"""Operator-reviewed exceptions to normal per-match dataset expectations."""

MIGRATION_ID = "0026"
DESCRIPTION = "Add auditable reviewed match dataset exceptions"


def migrate(conn):
    conn.execute("""
        CREATE TABLE match_data_exceptions (
            match_id INTEGER NOT NULL,
            provider_match_id TEXT,
            exception_type TEXT NOT NULL CHECK(exception_type IN ('stats_not_expected')),
            reason_code TEXT NOT NULL CHECK(reason_code IN
              ('abandoned','cancelled','forfeit','not_played','historical_data_unavailable',
               'provider_data_unavailable','other')),
            display_reason TEXT NOT NULL,
            evidence_url TEXT,
            evidence_note TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revoked_at TEXT,
            PRIMARY KEY(match_id, exception_type),
            FOREIGN KEY(match_id) REFERENCES matches(match_id)
        )
    """)
    conn.execute("""
        CREATE TABLE match_data_exception_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            exception_type TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('created','updated','revoked')),
            reason_code TEXT NOT NULL,
            display_reason TEXT NOT NULL,
            evidence_url TEXT,
            evidence_note TEXT,
            actor TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY(match_id) REFERENCES matches(match_id)
        )
    """)
    conn.execute("CREATE INDEX idx_match_data_exceptions_active ON match_data_exceptions(exception_type,revoked_at)")
    conn.execute("CREATE INDEX idx_match_data_exception_audit_match ON match_data_exception_audit(match_id,occurred_at)")
