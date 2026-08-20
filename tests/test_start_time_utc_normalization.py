"""Regression coverage for Issue #167: a single persisted `start_time_utc`
textual representation across the canonical bootstrap write path and the
legacy scraper write path.
"""
from __future__ import annotations

import sqlite3

from afl_json import CollectionResult, persist_afl_metadata
from db.import_to_db import save_matches_to_db
from db.migration_runner import migrate_database
from utils.match_time import parse_match_time
from utils.time_format import normalize_utc_iso


def test_normalize_utc_iso_converts_offset_form_to_canonical_z_form():
    assert normalize_utc_iso("2026-03-12T08:00:00+00:00") == "2026-03-12T08:00:00Z"


def test_normalize_utc_iso_leaves_canonical_z_form_unchanged():
    assert normalize_utc_iso("2026-03-12T08:00:00Z") == "2026-03-12T08:00:00Z"


def test_normalize_utc_iso_preserves_non_utc_offsets_as_the_same_instant():
    # AWST is UTC+8, so 16:00 AWST is the same instant as 08:00Z.
    assert normalize_utc_iso("2026-03-12T16:00:00+08:00") == "2026-03-12T08:00:00Z"


def test_normalize_utc_iso_passes_through_none_and_empty():
    assert normalize_utc_iso(None) is None
    assert normalize_utc_iso("") == ""


def test_legacy_scraper_time_conversion_yields_canonical_z_form():
    iso_utc = parse_match_time("March 12 2026", "4:00pmAWST")

    assert iso_utc == "2026-03-12T08:00:00Z"


def collected_with_utc_start_time(utc_start_time: str) -> CollectionResult:
    return CollectionResult(
        competition={"afl_id": 1, "provider_id": "CD_C014", "code": "AFL",
                     "name": "AFL Premiership", "metadata": None, "source": {"id": 1}},
        season={"afl_id": 85, "provider_id": "CD_S2026014", "name": "2026 AFL",
                "short_name": "2026", "year": 2026, "current": True,
                "current_round_number": 0, "start_time": None, "end_time": None,
                "metadata": None, "source": {"id": 85}},
        teams=[{"afl_id": 10, "provider_id": "CD_T10", "name": "Home",
                "abbreviation": "HOM", "nickname": None, "displayName": "Home",
                "shortName": None, "team_type": None, "metadata": None, "club": None,
                "source": {"id": 10}}],
        rounds=[{"afl_id": 100, "provider_id": "CD_R100", "name": "Opening Round",
                 "abbreviation": "OR", "round_number": 0, "start_time": None, "end_time": None,
                 "byes": [], "metadata": None, "source": {"id": 100}}],
        matches=[{"afl_id": 1000, "provider_id": "CD_M1000", "status": "SCHEDULED",
                  "round": {"id": 100}, "home": {"team": {"id": 10, "abbreviation": "HOM"},
                                                  "score": {"totalScore": 12}},
                  "away": {"team": {"id": 11, "abbreviation": "AWY"},
                           "score": {"totalScore": 8}},
                  "home_score": {"totalScore": 12}, "away_score": {"totalScore": 8},
                  "venue": {"name": "MCG"}, "utc_start_time": utc_start_time,
                  "metadata": None, "source": {"id": 1000}}],
        current_season_afl_id=85,
    )


def test_bootstrap_persists_z_suffixed_provider_value_unchanged(tmp_path):
    path = tmp_path / "bootstrap.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")

    persist_afl_metadata(conn, collected_with_utc_start_time("2026-03-12T08:00:00Z"))

    assert conn.execute("SELECT start_time_utc FROM matches").fetchone() == (
        "2026-03-12T08:00:00Z",)
    conn.close()


def test_bootstrap_and_legacy_scraper_persist_identical_string_for_the_same_instant(tmp_path):
    """The canonical bootstrap path (provider `Z` value) and the legacy
    scraper path (offset-form value produced from local date/time) must
    persist the exact same `start_time_utc` string for the same instant.
    """
    path = tmp_path / "bootstrap.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")

    legacy_value = parse_match_time("March 12 2026", "4:00pmAWST")
    persist_afl_metadata(conn, collected_with_utc_start_time(legacy_value))

    bootstrap_stored = conn.execute("SELECT start_time_utc FROM matches").fetchone()[0]
    conn.close()

    assert bootstrap_stored == "2026-03-12T08:00:00Z"
    assert bootstrap_stored == legacy_value


def test_legacy_scraper_db_write_normalizes_stale_fallback_copied_values(tmp_path):
    """`extract_match_data` falls back to copying an existing DB row's
    `start_time_utc` verbatim when a match card has no freshly parsable
    scheduled time (e.g. a LIVE match update touching only status/scores).
    A pre-fix row stored in `+00:00` form must still come out of
    `save_matches_to_db` in the canonical `Z` form, not be written back
    unchanged.
    """
    path = tmp_path / "legacy.db"
    migrate_database(path)
    conn = sqlite3.connect(path)

    stale_offset_value = "2026-03-12T08:00:00+00:00"
    save_matches_to_db([{
        "match_id": 9000, "match_provider_id": "CD_M9000", "round_id": 100,
        "home_team": "HOM", "away_team": "AWY", "venue": "MCG", "status": "LIVE",
        "start_time_utc": stale_offset_value, "score_home": 10, "score_away": 5,
        "match_time_label": "Q3",
    }], conn)

    # Simulate extract_match_data's fallback: no fresh time parsed this scrape,
    # so the (still non-canonical) existing value is copied through unchanged
    # into the next save, alongside an updated status/score.
    save_matches_to_db([{
        "match_id": 9000, "match_provider_id": "CD_M9000", "round_id": 100,
        "home_team": "HOM", "away_team": "AWY", "venue": "MCG", "status": "COMPLETED",
        "start_time_utc": stale_offset_value, "score_home": 88, "score_away": 65,
        "match_time_label": "FULL TIME",
    }], conn)

    stored = conn.execute("SELECT start_time_utc, status FROM matches WHERE match_id=9000").fetchone()
    conn.close()

    assert stored == ("2026-03-12T08:00:00Z", "COMPLETED")
