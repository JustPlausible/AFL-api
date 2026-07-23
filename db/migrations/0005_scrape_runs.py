"""Add unified scrape run audit records."""

MIGRATION_ID = "0005"
DESCRIPTION = "Add scrape run audit table"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scrape_runs (
            run_id TEXT PRIMARY KEY,
            scrape_type TEXT NOT NULL,
            target_type TEXT,
            target_identifier TEXT,
            trigger_source TEXT NOT NULL CHECK(trigger_source IN ('cli','scheduler','admin_manual','startup_recovery')),
            status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            duration_ms INTEGER,
            rows_read INTEGER,
            rows_written INTEGER,
            error_class TEXT,
            error_summary TEXT,
            correlation_id TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scrape_runs_started_at ON scrape_runs(started_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scrape_runs_type_status_started ON scrape_runs(scrape_type, status, started_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scrape_runs_status_started ON scrape_runs(status, started_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scrape_runs_correlation_id ON scrape_runs(correlation_id)")
