"""Add structured fields for season-sync decision audit records."""

MIGRATION_ID = "0012"
DESCRIPTION = "Add structured season sync decision audit fields"


def migrate(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scrape_runs)")}
    additions = {
        "reason_code": "TEXT",
        "decision_class": "TEXT CHECK (decision_class IN ('safe', 'material') OR decision_class IS NULL)",
        "canonical_match_id": "INTEGER",
        "provider_match_id": "TEXT",
        "round_identifier": "TEXT",
        "diagnostic_summary": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE scrape_runs ADD COLUMN {name} {declaration}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrape_runs_reason_started "
        "ON scrape_runs(reason_code, started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrape_runs_canonical_match_started "
        "ON scrape_runs(canonical_match_id, started_at DESC)"
    )
