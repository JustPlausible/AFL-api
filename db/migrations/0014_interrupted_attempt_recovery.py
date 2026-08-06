"""Add durable evidence and terminal classifications for interrupted attempts."""

MIGRATION_ID = "0014"
DESCRIPTION = "Add interrupted polling attempt recovery evidence"


def _dependent_objects(conn, table):
    """Capture every explicit index/trigger and dependent view before rebuild."""
    rows = conn.execute(
        """SELECT type,name,sql FROM sqlite_master
           WHERE sql IS NOT NULL AND ((type IN ('index','trigger') AND tbl_name=?)
             OR (type='view' AND instr(lower(sql), lower(?))>0))
           ORDER BY CASE type WHEN 'view' THEN 0 WHEN 'trigger' THEN 1 ELSE 2 END,name""",
        (table, table),
    ).fetchall()
    for kind, name, _ in rows:
        if kind in {"view", "trigger"}:
            conn.execute(f'DROP {kind.upper()} "{name}"')
    return [row[2] for row in rows]


def migrate(conn):
    registry_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(scheduler_job_registry)")
    }
    if "recovery_run_id" not in registry_columns:
        registry_objects = _dependent_objects(conn, "scheduler_job_registry")
        conn.execute(
            "ALTER TABLE scheduler_job_registry RENAME TO scheduler_job_registry_old"
        )
        conn.execute("""
            CREATE TABLE scheduler_job_registry (
                job_id TEXT PRIMARY KEY, job_type TEXT NOT NULL, match_id INTEGER,
                round_id TEXT, scheduled_run_time TEXT,
                status TEXT NOT NULL CHECK(status IN
                  ('pending','running','succeeded','failed','skipped','interrupted')),
                last_attempt_time TEXT, last_success_time TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0, last_error_summary TEXT,
                func_ref TEXT, args_json TEXT NOT NULL DEFAULT '[]',
                trigger_type TEXT NOT NULL DEFAULT 'date', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, window_id TEXT, attempt_id TEXT,
                scrape_run_id TEXT, lease_generation INTEGER, lease_token TEXT,
                scheduler_instance_id TEXT, recovery_at TEXT, recovery_run_id TEXT,
                recovery_reason TEXT, attempt_persistence_evidence TEXT CHECK(
                  attempt_persistence_evidence IN ('committed','uncommitted','unknown') OR
                  attempt_persistence_evidence IS NULL), superseded_by_attempt_id TEXT,
                match_authoritative_evidence TEXT CHECK(match_authoritative_evidence IN
                  ('absent','partial_live','authoritative_final') OR match_authoritative_evidence IS NULL)
            )
        """)
        conn.execute("""INSERT INTO scheduler_job_registry (
            job_id,job_type,match_id,round_id,scheduled_run_time,status,
            last_attempt_time,last_success_time,attempt_count,last_error_summary,
            func_ref,args_json,trigger_type,created_at,updated_at)
            SELECT job_id,job_type,match_id,round_id,scheduled_run_time,status,
            last_attempt_time,last_success_time,attempt_count,last_error_summary,
            func_ref,args_json,trigger_type,created_at,updated_at
            FROM scheduler_job_registry_old""")
        conn.execute("DROP TABLE scheduler_job_registry_old")
        for sql in registry_objects:
            conn.execute(sql)
        conn.execute(
            "CREATE INDEX idx_scheduler_registry_attempt ON scheduler_job_registry(attempt_id)"
        )
        conn.execute(
            "CREATE INDEX idx_scheduler_registry_window ON scheduler_job_registry(window_id)"
        )

    scrape_columns = {row[1] for row in conn.execute("PRAGMA table_info(scrape_runs)")}
    if "recovery_run_id" not in scrape_columns:
        scrape_objects = _dependent_objects(conn, "scrape_runs")
        conn.execute("ALTER TABLE scrape_runs RENAME TO scrape_runs_old")
        conn.execute("""
            CREATE TABLE scrape_runs (
                run_id TEXT PRIMARY KEY, scrape_type TEXT NOT NULL, target_type TEXT,
                target_identifier TEXT, trigger_source TEXT NOT NULL CHECK(trigger_source IN
                  ('cli','scheduler','admin_manual','startup_recovery')),
                status TEXT NOT NULL CHECK(status IN
                  ('running','completed','partial','failed','interrupted')),
                started_at TEXT NOT NULL, finished_at TEXT, duration_ms INTEGER,
                rows_read INTEGER, rows_written INTEGER, error_class TEXT,
                error_summary TEXT, correlation_id TEXT, reason_code TEXT,
                decision_class TEXT CHECK(decision_class IN ('safe','material') OR decision_class IS NULL),
                canonical_match_id INTEGER, provider_match_id TEXT,
                round_identifier TEXT, diagnostic_summary TEXT,
                window_id TEXT, attempt_id TEXT, scheduler_job_id TEXT,
                lease_generation INTEGER, lease_token TEXT, scheduler_instance_id TEXT,
                response_received_at TEXT, persistence_committed_at TEXT,
                rows_inserted INTEGER, rows_updated INTEGER, rows_unchanged INTEGER,
                recovery_at TEXT, recovery_run_id TEXT, recovery_reason TEXT,
                attempt_persistence_evidence TEXT CHECK(attempt_persistence_evidence IN
                  ('committed','uncommitted','unknown') OR attempt_persistence_evidence IS NULL),
                superseded_by_attempt_id TEXT,
                match_authoritative_evidence TEXT CHECK(match_authoritative_evidence IN
                  ('absent','partial_live','authoritative_final') OR match_authoritative_evidence IS NULL)
            )
        """)
        old = [row[1] for row in conn.execute("PRAGMA table_info(scrape_runs_old)")]
        names = ",".join(old)
        conn.execute(
            f"INSERT INTO scrape_runs ({names}) SELECT {names} FROM scrape_runs_old"
        )
        conn.execute("DROP TABLE scrape_runs_old")
        for sql in scrape_objects:
            conn.execute(sql)
        conn.execute("CREATE INDEX idx_scrape_runs_attempt ON scrape_runs(attempt_id)")
        conn.execute("CREATE INDEX idx_scrape_runs_window ON scrape_runs(window_id)")

    window_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(match_stat_windows)")
    }
    additions = {
        "last_attempt_id": "TEXT",
        "last_scheduler_job_id": "TEXT",
        "last_scrape_run_id": "TEXT",
        "recovery_at": "TEXT",
        "recovery_run_id": "TEXT",
        "recovery_reason": "TEXT",
        "recovered_attempt_id": "TEXT",
        "superseded_attempt_id": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in window_columns:
            conn.execute(
                f"ALTER TABLE match_stat_windows ADD COLUMN {name} {declaration}"
            )

    conn.execute("""CREATE TABLE IF NOT EXISTS scheduler_runtime_instances (
        instance_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
        last_heartbeat_at TEXT NOT NULL, stopped_at TEXT,
        shutdown_kind TEXT CHECK(shutdown_kind IN ('graceful','unclean') OR shutdown_kind IS NULL)
    )""")
