# Add explicit SQLite schema migration runner

## Background / problem statement

The application currently evolves its SQLite schema from `db/init_db.py` and opportunistic table creation in scraper/import paths. That works for fresh databases, but it makes production upgrades risky: existing databases can silently miss new columns, constraints, or indexes unless every code path happens to run the right `ALTER TABLE` logic. The next reliability step is a small, deterministic migration layer that can be run at startup, from CI, and manually before deployments.

## Scope

Add a first-class SQLite migration system for application-owned tables and wire it into database initialisation.

## Implementation requirements

- Introduce a `schema_migrations` table that records an ordered migration identifier, description, checksum or equivalent integrity marker, and applied timestamp.
- Add a migration runner under `db/` that:
  - Discovers migration files in a deterministic order.
  - Applies only unapplied migrations.
  - Runs each migration inside an SQLite transaction where possible.
  - Fails loudly if an already-applied migration file changes unexpectedly.
- Move current incremental schema changes out of ad hoc runtime paths where practical and into migration files.
- Ensure `init_db()` invokes the migration runner after base connection setup so existing deployments upgrade automatically.
- Add a CLI entry point to run migrations explicitly without starting the API or scheduler.
- Keep migration SQL idempotent where SQLite limitations make replay-safety necessary.

## Out of scope

- Replacing SQLite with another database engine.
- Introducing a heavyweight ORM.
- Rewriting existing scrapers or route handlers beyond removing schema mutation side effects.
- Backfilling historical AFL data.

## Acceptance criteria

- A fresh database can be created from scratch and contains all expected current tables, columns, indexes, and constraints.
- An existing database created from the current `main` schema upgrades successfully without data loss.
- Re-running migrations is a no-op.
- Modifying an already-applied migration produces a clear error.
- Application startup and the explicit CLI command both use the same migration runner.

## Testing requirements

- Unit tests for migration ordering, idempotency, checksum/change detection, and transaction rollback on failure.
- Integration test that creates a representative pre-migration SQLite database and verifies it upgrades to the latest schema.
- Existing database tests continue to pass.

## Documentation updates required

- Document the migration workflow in the README or a dedicated database operations document.
- Include instructions for creating a new migration and running migrations manually.
- Note expected deployment order for production Compose users.

## Migration / backward-compatibility considerations

- Existing SQLite databases must remain readable and upgrade in place.
- The migration runner must not delete or rewrite existing rows unless a migration explicitly documents why that is safe.
- Any legacy schema-normalisation code retained for compatibility should be marked temporary and covered by tests.
