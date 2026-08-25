"""Modular analytics/telemetry framework foundation (Issue #205).

Two bounded raw-observation tables plus their daily rollup tables --
historical/domain analytics over upstream AFL/CFS polling behaviour and
consumer ``/api/v1`` usage, deliberately separate from:

* the diagnostics evidence-capture framework (``diagnostics/``, migrations
  ``0016``/``0017``/``0018``), which is opt-in raw-evidence retention for a
  bounded investigation, never analytics reporting;
* ``scrape_runs`` (migration ``0005`` onward), which is scheduler-run audit
  history, not a factual per-poll observation stream with change semantics;
* full AFL/CFS payload persistence -- no response body is ever stored here.

``analytics_upstream_polls`` / ``analytics_consumer_requests`` hold recent
bounded raw detail (retention window: ``config.AFL_ANALYTICS_RETENTION_DAYS``,
default 14 days). ``analytics_upstream_daily_rollups`` /
``analytics_consumer_daily_rollups`` hold the same information aggregated by
day, kept indefinitely -- see ``analytics/rollup.py`` and
``docs/analytics_framework.md`` "Storage schema and retention".

Every table carries an ``observation_date`` column (the UTC calendar date of
``observed_at``) purely so the daily rollup job can group/purge by date with
a plain indexed equality/range scan rather than a per-row string-slice of
``observed_at`` -- a deliberate small denormalisation, not a second source of
truth (it is always derived from ``observed_at`` at write time).
"""

MIGRATION_ID = "0023"
DESCRIPTION = "Add modular analytics/telemetry framework tables (Issue #205)"


def migrate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analytics_upstream_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resource TEXT NOT NULL,
            match_id INTEGER,
            match_provider_id TEXT,
            observed_at TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            lifecycle_state TEXT,
            configured_interval_seconds REAL,
            actual_interval_seconds REAL,
            duration_ms REAL NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN (
                'success', 'not_published', 'unavailable', 'auth_error',
                'transport_error', 'http_error', 'invalid_response',
                'malformed_payload', 'error'
            )),
            http_status INTEGER,
            changed INTEGER CHECK(changed IN (0,1)),
            change_magnitude INTEGER,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(match_id) REFERENCES matches(match_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_upstream_polls_resource_time ON analytics_upstream_polls(resource, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_upstream_polls_match ON analytics_upstream_polls(match_id, resource, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_upstream_polls_lifecycle ON analytics_upstream_polls(resource, lifecycle_state, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_upstream_polls_date ON analytics_upstream_polls(observation_date)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS analytics_consumer_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            duration_ms REAL NOT NULL,
            status_code INTEGER NOT NULL,
            api_key_id INTEGER,
            request_mode TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_consumer_requests_route_time ON analytics_consumer_requests(route, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_consumer_requests_key ON analytics_consumer_requests(api_key_id, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_consumer_requests_date ON analytics_consumer_requests(observation_date)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS analytics_upstream_daily_rollups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resource TEXT NOT NULL,
            lifecycle_state TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            polls INTEGER NOT NULL DEFAULT 0,
            successes INTEGER NOT NULL DEFAULT 0,
            changed INTEGER NOT NULL DEFAULT 0,
            unchanged INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            total_duration_ms REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(resource, lifecycle_state, observation_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_upstream_rollups_resource ON analytics_upstream_daily_rollups(resource, observation_date)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS analytics_consumer_daily_rollups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0,
            status_2xx INTEGER NOT NULL DEFAULT 0,
            status_4xx INTEGER NOT NULL DEFAULT 0,
            status_5xx INTEGER NOT NULL DEFAULT 0,
            total_duration_ms REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(route, observation_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_consumer_rollups_route ON analytics_consumer_daily_rollups(route, observation_date)")
