"""Add the scheduler job registry for application-defined scheduler metadata."""

MIGRATION_ID = "0004"
DESCRIPTION = "Add scheduler job registry"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduler_job_registry (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            match_id INTEGER,
            round_id TEXT,
            scheduled_run_time TEXT,
            status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')),
            last_attempt_time TEXT,
            last_success_time TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error_summary TEXT,
            func_ref TEXT,
            args_json TEXT NOT NULL DEFAULT '[]',
            trigger_type TEXT NOT NULL DEFAULT 'date',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduler_registry_status_time ON scheduler_job_registry(status, scheduled_run_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduler_registry_match ON scheduler_job_registry(match_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduler_registry_round ON scheduler_job_registry(round_id)")
