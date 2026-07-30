"""Populate the legacy clubs table from the canonical validated bootstrap seed."""

from db.club_seed import upsert_club_seed

MIGRATION_ID = "0008"
DESCRIPTION = "Upsert canonical AFL club bootstrap seed"


def migrate(conn):
    upsert_club_seed(conn)
