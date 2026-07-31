"""Allow scrape audits to represent successful runs with partial row failures."""

MIGRATION_ID = "0011"
DESCRIPTION = "Add partial scrape-run outcome"


def migrate(conn):
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='scrape_runs'"
    ).fetchone()[0]
    if "'partial'" in sql:
        return
    conn.execute("ALTER TABLE scrape_runs RENAME TO scrape_runs_old")
    conn.execute("""
        CREATE TABLE scrape_runs (
            run_id TEXT PRIMARY KEY,
            scrape_type TEXT NOT NULL,
            target_type TEXT,
            target_identifier TEXT,
            trigger_source TEXT NOT NULL CHECK(trigger_source IN ('cli','scheduler','admin_manual','startup_recovery')),
            status TEXT NOT NULL CHECK(status IN ('running','completed','partial','failed')),
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
    conn.execute("INSERT INTO scrape_runs SELECT * FROM scrape_runs_old")
    conn.execute("DROP TABLE scrape_runs_old")
    conn.execute("CREATE INDEX idx_scrape_runs_started_at ON scrape_runs(started_at DESC)")
    conn.execute("CREATE INDEX idx_scrape_runs_type_status_started ON scrape_runs(scrape_type, status, started_at DESC)")
    conn.execute("CREATE INDEX idx_scrape_runs_status_started ON scrape_runs(status, started_at DESC)")
    conn.execute("CREATE INDEX idx_scrape_runs_correlation_id ON scrape_runs(correlation_id)")
