import json
import sqlite3

import pytest

from db.club_seed import ClubSeedError, load_club_seed, upsert_club_seed
from db.migration_runner import migrate_database


def _seed_document():
    return {
        "schema_version": 1,
        "clubs": [{
            "name": "Test Club", "slug": "test", "canonicalCode": "TST",
            "teamId": 1, "clubId": 2, "providerId": "CD_T1",
            "abbreviation": "TC", "clubSiteUrl": "https://club.test/",
            "squadUrl": "https://club.test/team", "editorialAliases": ["testing"],
        }],
    }


def test_loader_maps_canonical_fields_without_discarding_them(tmp_path):
    path = tmp_path / "clubs.json"
    path.write_text(json.dumps(_seed_document()))

    club = load_club_seed(path)[0]

    assert club["canonicalCode"] == club["code"] == "TST"
    assert club["abbreviation"] == "TC"
    assert club["teamId"] == 1 and club["clubId"] == 2
    assert club["providerId"] == "CD_T1"
    assert club["clubSiteUrl"] == club["website"]
    assert club["squadUrl"] == club["squad_url"]
    assert club["editorialAliases"] == club["aliases"] == ["testing"]


@pytest.mark.parametrize("version", [None, 0, 2, "1"])
def test_loader_rejects_unsupported_schema_versions(tmp_path, version):
    document = _seed_document()
    document["schema_version"] = version
    path = tmp_path / "clubs.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ClubSeedError, match="schema_version"):
        load_club_seed(path)


def test_loader_rejects_missing_fields_and_duplicate_identity(tmp_path):
    document = _seed_document()
    del document["clubs"][0]["providerId"]
    path = tmp_path / "missing.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ClubSeedError, match="providerId"):
        load_club_seed(path)

    document = _seed_document()
    document["clubs"].append({**document["clubs"][0], "name": "Duplicate"})
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ClubSeedError, match="duplicate"):
        load_club_seed(path)


def test_upsert_is_idempotent_and_preserves_unrelated_clubs():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE clubs (code TEXT PRIMARY KEY, name TEXT, slug TEXT, website TEXT, squad_url TEXT, aliases TEXT)")
    conn.execute("INSERT INTO clubs(code, name) VALUES ('LOCAL', 'Local Club')")
    clubs = load_club_seed()

    assert upsert_club_seed(conn, clubs) == 18
    first = conn.execute("SELECT * FROM clubs ORDER BY code").fetchall()
    assert upsert_club_seed(conn, clubs) == 18
    second = conn.execute("SELECT * FROM clubs ORDER BY code").fetchall()

    assert second == first
    assert len(second) == 19
    assert conn.execute("SELECT name FROM clubs WHERE code='LOCAL'").fetchone() == ("Local Club",)


def test_fresh_install_and_upgrade_receive_canonical_clubs(tmp_path):
    fresh = tmp_path / "fresh.db"
    migrate_database(fresh)
    conn = sqlite3.connect(fresh)
    assert conn.execute("SELECT COUNT(*) FROM clubs").fetchone() == (18,)
    # Internal code is deliberately distinct from the provider abbreviation.
    assert conn.execute("SELECT code FROM clubs WHERE name='Port Adelaide Power'").fetchone() == ("PTA",)
    conn.close()

    upgraded = tmp_path / "upgraded.db"
    migrate_database(upgraded)
    conn = sqlite3.connect(upgraded)
    conn.execute("UPDATE clubs SET name='Old name' WHERE code='ADE'")
    conn.execute("DELETE FROM schema_migrations WHERE migration_id='0008'")
    conn.commit()
    conn.close()
    migrate_database(upgraded)
    conn = sqlite3.connect(upgraded)
    assert conn.execute("SELECT name FROM clubs WHERE code='ADE'").fetchone() == ("Adelaide Crows",)


def test_identifier_refresh_migration_updates_brisbane_and_preserves_unrelated_rows(tmp_path):
    database = tmp_path / "already-migrated.db"
    migrate_database(database)
    conn = sqlite3.connect(database)
    conn.execute("UPDATE clubs SET aliases = ? WHERE code = 'BRI'", (json.dumps(["brisbanelions"]),))
    conn.execute("INSERT INTO clubs(code, name, aliases) VALUES ('LOCAL', 'Local Club', '[]')")
    conn.execute("DELETE FROM schema_migrations WHERE migration_id = '0010'")
    conn.commit()
    conn.close()

    assert migrate_database(database) == ["0010"]

    conn = sqlite3.connect(database)
    brisbane_aliases = json.loads(
        conn.execute("SELECT aliases FROM clubs WHERE code = 'BRI'").fetchone()[0]
    )
    assert "BRIS" in brisbane_aliases
    assert conn.execute("SELECT name FROM clubs WHERE code = 'LOCAL'").fetchone() == ("Local Club",)
    conn.close()
