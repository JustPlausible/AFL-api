"""Refresh canonical clubs after verified editorial identifier updates."""

from db.club_seed import upsert_club_seed

MIGRATION_ID = "0010"
DESCRIPTION = "Refresh canonical AFL club identifiers"


def migrate(conn):
    upsert_club_seed(conn)
