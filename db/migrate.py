"""Explicit migration-only CLI: python -m db.migrate."""
from db.migration_runner import MigrationError, migrate_database
from utils.log import log


def main() -> None:
    try:
        ran = migrate_database()
    except (MigrationError, OSError) as exc:
        log(f"❌ SQLite migration failed: {exc}", "ERROR")
        raise SystemExit(1) from exc
    if ran:
        log(f"✅ Applied SQLite migrations: {', '.join(ran)}", "SUCCESS")
    else:
        log("✅ SQLite database already up to date", "SUCCESS")


if __name__ == "__main__":
    main()
