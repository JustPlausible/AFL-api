"""Remove the obsolete single-season attribute from canonical AFL teams."""

MIGRATION_ID = "0025"
DESCRIPTION = "Make AFL teams stable identities and team seasons authoritative"
REQUIRES_FOREIGN_KEYS_OFF = True


def migrate(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(afl_teams)")}
    if "season_id" not in columns:
        return
    # The migration runner disables enforcement for this table rebuild and
    # performs foreign_key_check before commit. Child declarations retain the
    # stable parent name throughout.
    conn.execute("""
        CREATE TABLE afl_teams_rebuilt (
            afl_id INTEGER PRIMARY KEY, provider_id TEXT UNIQUE, name TEXT,
            abbreviation TEXT, nickname TEXT, display_name TEXT, short_name TEXT,
            team_type TEXT, metadata_json TEXT, club_json TEXT,
            source_json TEXT, updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        INSERT INTO afl_teams_rebuilt
        SELECT afl_id, provider_id, name, abbreviation, nickname, display_name,
               short_name, team_type, metadata_json, club_json, source_json, updated_at
        FROM afl_teams
    """)
    conn.execute("DROP TABLE afl_teams")
    conn.execute("ALTER TABLE afl_teams_rebuilt RENAME TO afl_teams")
