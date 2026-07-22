# SQLite database migrations

AFL-api uses a lightweight repository-owned migration runner in `db/migration_runner.py`.
Migration files live in `db/migrations/` and are named `NNNN_description.py`; the numeric identifier controls deterministic ordering. Each file declares `MIGRATION_ID`, `DESCRIPTION`, and `migrate(conn)`. Applied migration files must never be edited; create a new migration instead.

The runner records applied migrations in `schema_migrations` with the ordered identifier, description, SHA-256 checksum, and timestamp. The checksum covers the executable Python file bytes plus the declared identifier and description, so changing code or metadata for an already-applied migration causes startup and CLI migration to fail clearly.

Run migrations manually with:

```bash
python -m db.migrate
```

The existing command remains supported:

```bash
python -m db.init_db
```

Both commands honour the configured `DB_PATH`, create a fresh database, upgrade a supported existing database, and exit non-zero on migration failure.

## v0.3.0 baseline strategy

For a database with no `schema_migrations` table, the runner distinguishes:

* empty database: no application tables exist, so all migrations run from scratch;
* recognised v0.3.0-compatible database: all ten expected application tables exist with the exact required baseline columns and only documented importer-compatible extras, so migration `0001` is recorded without replaying its `CREATE TABLE` statements and later migrations run normally;
* unexpected or partial database: missing tables, extra tables, missing columns, or unexpected columns fail before any baseline is recorded.

The baseline signature is the v0.3.0 `init_db()` schema for `api_keys`, `clubs`, `players`, `rounds`, `matches`, `injuries`, `lineups`, `player_stats`, `scrape_log`, and `scrape_summary`. Importer-era extra columns accepted during baseline are `players.id`, `players.source`, `players.scraped_at`, `players.resolved_at`, and `matches.match_time_label`.

Legacy API-key plaintext schemas are upgraded by migration `0003` in a transaction. Plaintext keys are hashed, prefixes are stored, and `api_key` is nulled idempotently without deleting rows.

## Creating a migration

1. Add `db/migrations/NNNN_short_description.py` with the next identifier.
2. Declare `MIGRATION_ID`, `DESCRIPTION`, and `migrate(conn)`.
3. Use individual `conn.execute(...)` statements; do not use `executescript()`.
4. Make safe data transformations explicit in comments/docstrings.
5. Add focused tests for fresh and upgraded databases.

## Production Compose deployment order

1. Stop writers or otherwise ensure no concurrent scraper/import activity.
2. Back up the SQLite database and relevant WAL/SHM state safely; do not copy only the main `.db` file from a live WAL-mode database and assume it is complete.
3. Update or pull the new application image.
4. Run `python -m db.migrate` once.
5. Start the application services.
6. Verify health and migration status.
