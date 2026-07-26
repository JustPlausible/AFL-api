from __future__ import annotations

import sqlite3

import pytest

from afl_json import CollectionResult, persist_afl_metadata
from db.migration_runner import migrate_database


def collected(*, venue="MCG", status="SCHEDULED", provider_ids=True):
    provider = (lambda value: value) if provider_ids else (lambda _value: None)
    return CollectionResult(
        competition={"afl_id": 1, "provider_id": provider("CD_C014"), "code": "AFL",
                     "name": "AFL Premiership", "metadata": None, "source": {"id": 1}},
        season={"afl_id": 85, "provider_id": provider("CD_S2026014"), "name": "2026 AFL",
                "short_name": "2026", "year": 2026, "current": True,
                "current_round_number": 0, "start_time": None, "end_time": None,
                "metadata": None, "source": {"id": 85}},
        teams=[{"afl_id": 10, "provider_id": provider("CD_T10"), "name": "Home",
                "abbreviation": "HOM", "nickname": None, "displayName": "Home",
                "shortName": None, "team_type": None, "metadata": None, "club": None,
                "source": {"id": 10}}],
        rounds=[{"afl_id": 100, "provider_id": provider("CD_R100"), "name": "Opening Round",
                 "abbreviation": "OR", "round_number": 0, "start_time": None, "end_time": None,
                 "byes": [{"id": 10}], "metadata": None, "source": {"id": 100}}],
        matches=[{"afl_id": 1000, "provider_id": provider("CD_M1000"), "status": status,
                  "round": {"id": 100}, "home": {"team": {"id": 10, "abbreviation": "HOM"},
                                                     "score": {"totalScore": 12}},
                  "away": {"team": {"id": 11, "abbreviation": "AWY"},
                             "score": {"totalScore": 8}},
                  "home_score": {"totalScore": 12}, "away_score": {"totalScore": 8},
                  "venue": {"name": venue}, "utc_start_time": "2026-03-01T08:00:00Z",
                  "metadata": None, "source": {"id": 1000}}],
    )


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "bootstrap.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    conn.close()


def test_clean_bootstrap_persists_hierarchy_and_opening_round(database):
    summary = persist_afl_metadata(database, collected())

    assert (summary.records_read, summary.inserted, summary.updated, summary.unchanged, summary.failed) == (5, 5, 0, 0, 0)
    assert database.execute("SELECT provider_id, year FROM afl_seasons").fetchone() == ("CD_S2026014", 2026)
    assert database.execute("SELECT round_number, byes_json FROM rounds").fetchone() == (0, '[{"id":10}]')
    assert database.execute("SELECT match_provider_id, venue, score_home, status FROM matches").fetchone() == (
        "CD_M1000", "MCG", 12, "SCHEDULED")


def test_repeat_is_unchanged_and_fixture_change_is_updated(database):
    persist_afl_metadata(database, collected())
    same = persist_afl_metadata(database, collected())
    changed = persist_afl_metadata(database, collected(venue="Docklands", status="CONCLUDED"))

    assert (same.inserted, same.updated, same.unchanged) == (0, 0, 5)
    assert (changed.inserted, changed.updated, changed.unchanged) == (0, 1, 4)
    assert database.execute("SELECT venue, status FROM matches").fetchone() == ("Docklands", "CONCLUDED")
    assert database.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1


def test_missing_optional_provider_identifiers_are_retained_as_null(database):
    summary = persist_afl_metadata(database, collected(provider_ids=False))

    assert summary.inserted == 5
    assert database.execute("SELECT provider_id FROM afl_competitions").fetchone()[0] is None
    assert database.execute("SELECT match_provider_id FROM matches").fetchone()[0] is None


def test_persistence_failure_rolls_back_entire_hierarchy(database):
    broken = collected()
    broken.matches[0]["round"] = {"id": None}

    with pytest.raises(sqlite3.IntegrityError):
        persist_afl_metadata(database, broken)
    assert database.execute("SELECT COUNT(*) FROM afl_competitions").fetchone()[0] == 0
