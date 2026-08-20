from __future__ import annotations

import sqlite3

import pytest

from afl_json import CollectionResult, persist_afl_metadata
from db.migration_runner import migrate_database


def collected(*, venue="MCG", status="SCHEDULED", provider_ids=True, current_season_afl_id=85):
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
        current_season_afl_id=current_season_afl_id,
    )


def collected_season(season_afl_id, year, *, current_season_afl_id, team_id, round_id,
                     match_id, status="SCHEDULED", venue="MCG"):
    """A second, distinct persisted season within the same AFL competition."""
    return CollectionResult(
        competition={"afl_id": 1, "provider_id": "CD_C014", "code": "AFL",
                     "name": "AFL Premiership", "metadata": None, "source": {"id": 1}},
        season={"afl_id": season_afl_id, "provider_id": f"CD_S{year}014", "name": f"{year} AFL",
                "short_name": str(year), "year": year, "current": True, "current_round_number": 0,
                "start_time": None, "end_time": None, "metadata": None,
                "source": {"id": season_afl_id}},
        teams=[{"afl_id": team_id, "provider_id": f"CD_T{team_id}", "name": "Home",
                "abbreviation": "HOM", "nickname": None, "displayName": "Home",
                "shortName": None, "team_type": None, "metadata": None, "club": None,
                "source": {"id": team_id}}],
        rounds=[{"afl_id": round_id, "provider_id": f"CD_R{round_id}", "name": "Opening Round",
                 "abbreviation": "OR", "round_number": 0, "start_time": None, "end_time": None,
                 "byes": [], "metadata": None, "source": {"id": round_id}}],
        matches=[{"afl_id": match_id, "provider_id": f"CD_M{match_id}", "status": status,
                  "round": {"id": round_id}, "home": {"team": {"id": team_id, "abbreviation": "HOM"},
                                                       "score": {"totalScore": 12}},
                  "away": {"team": {"id": team_id + 1, "abbreviation": "AWY"},
                           "score": {"totalScore": 8}},
                  "home_score": {"totalScore": 12}, "away_score": {"totalScore": 8},
                  "venue": {"name": venue}, "utc_start_time": f"{year}-03-01T08:00:00Z",
                  "metadata": None, "source": {"id": match_id}}],
        current_season_afl_id=current_season_afl_id,
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


# --- afl_seasons.is_current invariant (issue #184) --------------------------------


def _is_current(database, afl_id):
    row = database.execute("SELECT is_current FROM afl_seasons WHERE afl_id=?", (afl_id,)).fetchone()
    return row[0] if row else None


def _current_count(database):
    return database.execute("SELECT COUNT(*) FROM afl_seasons WHERE is_current=1").fetchone()[0]


def test_bootstrapping_the_current_season_marks_it_current(database):
    persist_afl_metadata(database, collected(current_season_afl_id=85))

    assert _is_current(database, 85) == 1
    assert _current_count(database) == 1


def test_historical_season_is_marked_not_current(database):
    # The independently determined current season (85) differs from the one
    # being bootstrapped here (84), as when an operator requests a past year.
    historical = collected_season(84, 2025, current_season_afl_id=85,
                                  team_id=20, round_id=200, match_id=2000)
    persist_afl_metadata(database, historical)

    assert _is_current(database, 84) == 0
    # The undetermined-yet-current season has no persisted row, so the
    # invariant "at most one current season" still holds without inventing one.
    assert _current_count(database) == 0


def test_season_advancement_clears_the_previous_current_marker(database):
    persist_afl_metadata(database, collected(current_season_afl_id=85))
    assert _is_current(database, 85) == 1

    advanced = collected_season(90, 2027, current_season_afl_id=90,
                                team_id=30, round_id=300, match_id=3000)
    persist_afl_metadata(database, advanced)

    assert _is_current(database, 85) == 0
    assert _is_current(database, 90) == 1
    assert _current_count(database) == 1


def test_repeated_bootstrap_of_the_current_season_is_idempotent(database):
    persist_afl_metadata(database, collected(current_season_afl_id=85))
    summary = persist_afl_metadata(database, collected(current_season_afl_id=85))

    assert (summary.inserted, summary.updated, summary.unchanged) == (0, 0, 5)
    assert _is_current(database, 85) == 1
    assert _current_count(database) == 1


def test_repeated_sync_across_seasons_never_yields_multiple_current_markers(database):
    persist_afl_metadata(database, collected(current_season_afl_id=85))
    persist_afl_metadata(database, collected_season(84, 2025, current_season_afl_id=85,
                                                    team_id=20, round_id=200, match_id=2000))
    persist_afl_metadata(database, collected_season(90, 2027, current_season_afl_id=90,
                                                    team_id=30, round_id=300, match_id=3000))
    # Re-syncing the now-historical 2026 season must not resurrect its marker.
    persist_afl_metadata(database, collected(current_season_afl_id=90))

    rows = dict(database.execute("SELECT afl_id, is_current FROM afl_seasons").fetchall())
    assert rows == {85: 0, 84: 0, 90: 1}
    assert _current_count(database) == 1


def test_undeterminable_current_season_is_explicit_and_never_null(database):
    unknown = collected(current_season_afl_id=None)
    persist_afl_metadata(database, unknown)

    # Explicitly 0, not NULL: the canonical state must never be left undefined.
    assert _is_current(database, 85) == 0
    assert _current_count(database) == 0


def test_undeterminable_current_season_does_not_clear_a_prior_established_marker(database):
    persist_afl_metadata(database, collected(current_season_afl_id=85))

    undeterminable = collected_season(84, 2025, current_season_afl_id=None,
                                      team_id=20, round_id=200, match_id=2000)
    persist_afl_metadata(database, undeterminable)

    assert _is_current(database, 85) == 1
    assert _is_current(database, 84) == 0
    assert _current_count(database) == 1


def test_canonical_persistence_alone_resolves_current_team_regression(database):
    """Regression for #184/#180 (Josh Daicos): once canonical persistence sets
    afl_seasons.is_current deterministically, GET /api/v1/players/{id} resolves
    current_team purely from persisted state, with no endpoint-side fallback."""
    from afl_json import PlayerCollectionResult, persist_player_seasons
    from api.routes_v1 import _current_team

    persist_afl_metadata(database, collected(current_season_afl_id=85))
    player_result = PlayerCollectionResult(
        players=[{"champion_data_player_id": "CD_I396", "afl_player_id": 396,
                  "name": "Josh Daicos", "given_name": "Josh", "family_name": "Daicos"}],
        player_seasons=[{"champion_data_player_id": "CD_I396", "team_id": "CD_T10",
                          "jumper_number": 35, "listed_position": "MID", "photo_url": None,
                          "source": {"playerId": "CD_I396"}}],
        diagnostics=[], status="published",
    )
    persist_player_seasons(database, player_result, provider_season_id="CD_S2026014")

    database.row_factory = sqlite3.Row
    player_id = database.execute(
        "SELECT id FROM canonical_players WHERE display_name='Josh Daicos'"
    ).fetchone()["id"]

    current_team = _current_team(database, player_id)

    assert current_team is not None
    assert (current_team.team_id, current_team.name) == (10, "Home")
