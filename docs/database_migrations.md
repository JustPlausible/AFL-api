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

## Database paths in collection code

`DB_PATH` is the supported SQLite location for the API, CLI, scheduler, and active
scrapers. Its default is `data/afl_players.db`, resolved relative to the repository
root; deployments may set an absolute path (for example through `.env`). Active
collection code must resolve this setting at runtime and normally open the database
with `db.connection.get_db_connection()`, which also preserves named `sqlite3.Row`
access and the existing missing-database check.

Tests should monkeypatch `config.DB_PATH` to a database below `tmp_path` and initialise
that database before invoking collection code. Hard-coded repository-relative SQLite
paths are not permitted in active scraper or scraper-adjacent code. A direct
`sqlite3.connect()` is appropriate only when the shared helper's missing-file or row
factory behaviour is unsuitable; it must still use `db.connection.get_db_path()` at
call time and preserve the required cleanup and transaction behaviour.

## v0.3.0 baseline strategy

For a database with no `schema_migrations` table, the runner distinguishes:

* empty database: no application tables exist, so all migrations run from scratch;
* recognised v0.3.0-compatible database: all ten expected application tables exist with the exact required baseline columns and only documented importer-compatible extras, so migration `0001` is recorded without replaying its `CREATE TABLE` statements and later migrations run normally;
* unexpected or partial database: missing tables, extra tables, missing columns, or unexpected columns fail before any baseline is recorded.

The baseline signature is the v0.3.0 `init_db()` schema for `api_keys`, `clubs`, `players`, `rounds`, `matches`, `injuries`, `lineups`, `player_stats`, `scrape_log`, and `scrape_summary`. Importer-era extra columns accepted during baseline are `players.id`, `players.source`, `players.scraped_at`, `players.resolved_at`, and `matches.match_time_label`.

Legacy API-key plaintext schemas are upgraded by migration `0003` in a transaction. Plaintext keys are hashed, prefixes are stored, and `api_key` is nulled idempotently without deleting rows.

## Canonical club seed

`bootstrap/clubs.json` is the single, versioned source of truth for AFL club
identity and editorial aliases. The shared loader validates `schema_version` and
the required club fields before mapping canonical seed names onto the older
database interface: `canonicalCode` to `code`, `clubSiteUrl` to `website`,
`squadUrl` to `squad_url`, and `editorialAliases` to `aliases`. Canonical codes
remain stable internal identifiers and are not replaced by AFL abbreviations.

Migration `0008` loads the canonical seed and upserts clubs by `code`. Fresh
installs therefore begin with all canonical clubs, while upgrades refresh those
rows without deleting unrelated rows. Re-running the migration/upsert produces
the same rows. Runtime lookup, the club import CLI, and player-stat alias loading
all use the same validated loader rather than `data/clubs.json`.

Migration `0009` adds `canonical_players` plus the provider-namespaced
`player_provider_ids` crosswalk. Provider values are stored as opaque text and
are unique inside their namespace; contradictory mappings fail instead of
being reassigned. `competition_season_players` retains one canonical player
membership per AFL competition season, with an optional composite foreign key
to `afl_team_seasons`. The minimal team-season table prevents historical player
membership from depending on the mutable `afl_teams.season_id` compatibility
column. Existing `players`, `player_stats`, and `cfs_player_stats` behavior is
preserved; CFS statistic observations gain a nullable canonical-player link.

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
