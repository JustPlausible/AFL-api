"""Offline tests for production CFS match-roster persistence (Issue #219).

No live AFL/CFS access is required: MatchRosterCollector normalisation is
already covered by tests/test_afl_match_rosters.py and is reused unchanged
here; this file exercises afl_json.rosters.persist_match_rosters against a
migrated temporary SQLite database, following the same pattern as
tests/test_match_interchange_production.py (Issue #204's promotion, the
architectural template for this one -- see afl_json/rosters.py and
db/migrations/0024_match_roster_production.py module docstrings).

Fixtures reused from tests/fixtures/afl_json/: match_rosters_available.json
(initial published state) and match_rosters_changed.json (a later published
update -- one player's position moves, ins/outs/lateChanges differ) are the
same synthetic fixtures already committed for MatchRosterCollector's own
normalisation/compare_rosters tests -- see tests/test_afl_match_rosters.py.
Reusing them here keeps the collector and persistence tests aligned to the
same documented source shape rather than inventing a second one.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from afl_json.rosters import (
    MatchRosterCollector,
    RosterCollectionResult,
    RosterPersistenceSummary,
    RosterStatus,
    persist_match_rosters,
    resolve_canonical_player,
    resolve_canonical_team,
)
from db.migration_runner import MIGRATIONS_DIR, migrate_database

FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"

MATCH_ID = 100
MATCH_PROVIDER_ID = "CD_M100"
HOME_TEAM_ID = 1
HOME_PROVIDER_ID = "CD_T1"
AWAY_TEAM_ID = 2
AWAY_PROVIDER_ID = "CD_T2"
ROUND_PROVIDER_ID = "CD_R18"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class _Response:
    def __init__(self, data):
        self.data = data


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def get(self, name, path_parameters=None):
        return _Response(self.payload)


def _collect(fixture_name: str) -> RosterCollectionResult:
    return MatchRosterCollector(_FakeClient(_fixture(fixture_name))).collect(ROUND_PROVIDER_ID)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "rosters.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}','now')")
    conn.execute(
        "INSERT INTO afl_seasons(afl_id,provider_id,competition_id,year,is_current,updated_at) "
        "VALUES(85,'CD_S2026014',1,2026,NULL,'now')"
    )
    conn.execute(
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, provider_id) "
        "VALUES(1,'R18',85,1,?)", (ROUND_PROVIDER_ID,),
    )
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, status, scraped_at) "
        "VALUES(?,?,1,'Cats','Dogs','SCHEDULED','now')", (MATCH_ID, MATCH_PROVIDER_ID),
    )
    conn.executemany(
        "INSERT INTO afl_teams(afl_id,provider_id,season_id,name,abbreviation,updated_at) "
        "VALUES(?,?,85,?,?,'now')",
        [(HOME_TEAM_ID, HOME_PROVIDER_ID, "Cats", "CAT"), (AWAY_TEAM_ID, AWAY_PROVIDER_ID, "Dogs", "DOG")],
    )
    conn.commit()
    yield conn
    conn.close()


def add_player(conn, canonical_id, cd_id, name="Ada Able"):
    given, family = name.split(" ", 1)
    conn.execute(
        "INSERT INTO canonical_players(id, display_name, given_name, family_name, created_at, updated_at) "
        "VALUES(?,?,?,?,'now','now')", (canonical_id, name, given, family),
    )
    conn.execute(
        "INSERT INTO player_provider_ids(player_id, provider, provider_player_id, created_at, updated_at) "
        "VALUES(?, 'champion_data', ?, 'now', 'now')", (canonical_id, cd_id),
    )
    conn.commit()


# --- Migration ----------------------------------------------------------------

def test_fresh_database_has_roster_production_tables(tmp_path):
    db_path = tmp_path / "fresh.db"
    migrate_database(db_path)
    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"cfs_match_rosters", "cfs_match_roster_selections", "cfs_match_roster_context"} <= tables
    conn.close()


def test_migration_file_is_registered_and_discoverable():
    assert (MIGRATIONS_DIR / "0024_match_roster_production.py").exists()


def test_migration_on_populated_existing_database_preserves_existing_rows(db):
    """Re-running migrate_database against an already-migrated, populated
    database (simulating deploying this migration onto an existing production
    database) must be a no-op for existing data -- CREATE TABLE IF NOT EXISTS
    only, no ALTER of any existing table."""
    before = dict(db.execute(
        "SELECT match_id, match_provider_id, status FROM matches WHERE match_id=?", (MATCH_ID,)
    ).fetchone())
    # Re-apply migrations against the same (now populated) database file.
    migrate_database(db.execute("PRAGMA database_list").fetchone()[2])
    after = dict(db.execute(
        "SELECT match_id, match_provider_id, status FROM matches WHERE match_id=?", (MATCH_ID,)
    ).fetchone())
    assert before == after
    assert db.execute("SELECT COUNT(*) FROM cfs_match_rosters").fetchone()[0] == 0


# --- Identity resolution --------------------------------------------------------

def test_resolve_canonical_team_and_player(db):
    add_player(db, 501, "CD_I1")
    assert resolve_canonical_team(db, HOME_PROVIDER_ID) == HOME_TEAM_ID
    assert resolve_canonical_team(db, "CD_T_UNKNOWN") is None
    assert resolve_canonical_team(db, None) is None
    assert resolve_canonical_player(db, "CD_I1") == 501
    assert resolve_canonical_player(db, "CD_I_UNKNOWN") is None
    assert resolve_canonical_player(db, None) is None


# --- Normal publish -------------------------------------------------------------

def test_persist_published_roster_writes_team_selection_and_context_rows(db):
    add_player(db, 501, "CD_I1")
    result = _collect("match_rosters_available.json")
    assert result.status is RosterStatus.PUBLISHED

    summary = persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()

    assert summary.rosters_written == 2
    assert summary.selections_written == 4
    assert summary.context_written == 2
    assert summary.unmatched_matches == ()
    assert summary.unmatched_teams == ()

    rosters = {row["side"]: dict(row) for row in db.execute("SELECT * FROM cfs_match_rosters")}
    assert rosters["home"]["team_provider_id"] == HOME_PROVIDER_ID
    assert rosters["home"]["canonical_team_id"] == HOME_TEAM_ID
    assert rosters["home"]["team_status"] == "CONFIRMED"
    assert rosters["home"]["match_status_at_observation"] == "PUBLISHED"
    assert rosters["home"]["source_last_updated"] == "2026-07-25T08:30:00Z"
    assert rosters["away"]["team_provider_id"] == AWAY_PROVIDER_ID


def test_home_and_away_sides_resolve_correct_canonical_teams(db):
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    rows = {row["side"]: row["canonical_team_id"] for row in db.execute(
        "SELECT DISTINCT side, canonical_team_id FROM cfs_match_roster_selections"
    )}
    assert rows["home"] == HOME_TEAM_ID
    assert rows["away"] == AWAY_TEAM_ID


def test_resolved_player_gets_canonical_id(db):
    add_player(db, 501, "CD_I1", "Ada Able")
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    row = db.execute(
        "SELECT canonical_player_id FROM cfs_match_roster_selections WHERE player_provider_id='CD_I1'"
    ).fetchone()
    assert row["canonical_player_id"] == 501


def test_unresolved_player_persists_with_null_canonical_id_not_guessed(db):
    """CD_I2 (Bea Baker) has no player_provider_ids crosswalk row in this test
    database. The Champion Data id must still be persisted; canonical
    identity must stay null, never guessed from playerName."""
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    row = db.execute(
        "SELECT canonical_player_id FROM cfs_match_roster_selections WHERE player_provider_id='CD_I2'"
    ).fetchone()
    assert row["canonical_player_id"] is None


def test_no_name_based_guessing_even_when_a_same_named_canonical_player_exists(db):
    """A canonical player sharing the source displayName but never linked via
    the champion_data crosswalk must never be matched by name."""
    add_player(db, 999, "CD_I_OTHER", "Bea Baker")
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    row = db.execute(
        "SELECT canonical_player_id FROM cfs_match_roster_selections WHERE player_provider_id='CD_I2'"
    ).fetchone()
    assert row["canonical_player_id"] is None


def test_later_crosswalk_self_heals_unresolved_canonical_player(db):
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    assert db.execute(
        "SELECT canonical_player_id FROM cfs_match_roster_selections WHERE player_provider_id='CD_I2'"
    ).fetchone()["canonical_player_id"] is None

    add_player(db, 777, "CD_I2", "Bea Baker")
    # A later valid observation (same content) must repair the unresolved row.
    persist_match_rosters(db, result, observed_at="2026-07-25T09:00:00+00:00")
    db.commit()
    assert db.execute(
        "SELECT canonical_player_id FROM cfs_match_roster_selections WHERE player_provider_id='CD_I2'"
    ).fetchone()["canonical_player_id"] == 777


def test_unmatched_canonical_team_still_persists_provider_identity(db):
    """Team resolution failing (no afl_teams row) must not drop the roster
    for that team -- the Champion Data team id is preserved, canonical_team_id
    stays null, and it is reported in unmatched_teams for observability."""
    db.execute("DELETE FROM afl_teams WHERE afl_id=?", (AWAY_TEAM_ID,))
    db.commit()
    result = _collect("match_rosters_available.json")
    summary = persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    assert (MATCH_PROVIDER_ID, AWAY_PROVIDER_ID) in summary.unmatched_teams
    row = db.execute(
        "SELECT canonical_team_id FROM cfs_match_rosters WHERE team_provider_id=?", (AWAY_PROVIDER_ID,)
    ).fetchone()
    assert row[0] is None


def test_unmatched_canonical_match_is_skipped_without_affecting_persistence_call(db):
    db.execute("DELETE FROM matches WHERE match_id=?", (MATCH_ID,))
    db.commit()
    result = _collect("match_rosters_available.json")
    summary = persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    assert summary.unmatched_matches == (MATCH_PROVIDER_ID,)
    assert summary.rosters_written == 0
    assert db.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0] == 0


# --- Selections vs positional groups vs context ---------------------------------

def test_selected_positional_groups_persist_distinctly(db):
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    positions = {
        row["player_provider_id"]: row["position"]
        for row in db.execute("SELECT player_provider_id, position FROM cfs_match_roster_selections")
    }
    assert positions["CD_I1"] == "FORWARDS"
    assert positions["CD_I2"] == "INTERCHANGE"
    assert positions["CD_I3"] == "FORWARDS"


def test_ins_and_outs_remain_separate_from_selections_and_each_other(db):
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    selection_players = {row[0] for row in db.execute(
        "SELECT player_provider_id FROM cfs_match_roster_selections"
    )}
    context = {
        (row["context_type"], row["player_provider_id"]): row["reason"]
        for row in db.execute("SELECT context_type, player_provider_id, reason FROM cfs_match_roster_context")
    }
    assert context[("ins", "CD_I1")] == "Selected"
    assert context[("outs", "CD_I4")] == "Managed"
    # CD_I1 is both selected (FORWARDS, home) and a home "in" -- two separate
    # rows in two separate tables, never merged into one lineup-membership
    # record.
    assert "CD_I1" in selection_players
    # CD_I4 is a home-side "out" but is also selected in the *away* team's
    # EMERGENCIES group in this fixture -- confirming the "out" context
    # record is team-scoped and never collapses onto the other side's
    # selection for the same Champion Data player id.
    home_out_side = db.execute(
        "SELECT side FROM cfs_match_roster_context WHERE context_type='outs' AND player_provider_id='CD_I4'"
    ).fetchone()[0]
    away_selection_side = db.execute(
        "SELECT side FROM cfs_match_roster_selections WHERE player_provider_id='CD_I4'"
    ).fetchone()[0]
    assert home_out_side == "home"
    assert away_selection_side == "away"


def test_late_changes_list_persists_as_context_record(db):
    result = _collect("match_rosters_changed.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T09:00:00+00:00")
    db.commit()
    row = db.execute(
        "SELECT player_provider_id, reason FROM cfs_match_roster_context WHERE context_type='lateChanges'"
    ).fetchone()
    assert row["player_provider_id"] == "CD_I6"
    assert row["reason"] == "Warm-up change"


def test_object_form_late_changes_produce_no_context_rows(db):
    """match_rosters_available.json's away team supplies lateChanges: [] and
    its home team supplies lateChanges: {} (an unresolved object form, per
    docs/match_rosters.md 'Verified live structure') -- MatchRosterCollector
    never turns either into change records, so persistence must write zero
    lateChanges rows for this fixture."""
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    assert db.execute(
        "SELECT COUNT(*) FROM cfs_match_roster_context WHERE context_type='lateChanges'"
    ).fetchone()[0] == 0


# --- Ordering, updates and replacement safety -----------------------------------

def test_provider_array_reordering_does_not_create_duplicate_or_changed_rows(db):
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    before = {row["player_provider_id"]: dict(row) for row in db.execute(
        "SELECT player_provider_id, position, first_observed_at FROM cfs_match_roster_selections"
    )}

    reordered = _fixture("match_rosters_available.json")
    home_positions = reordered[0]["matchRoster"]["homeTeam"]["positions"]
    reordered[0]["matchRoster"]["homeTeam"]["positions"] = list(reversed(home_positions))
    reordered_result = MatchRosterCollector(_FakeClient(reordered)).collect(ROUND_PROVIDER_ID)
    persist_match_rosters(db, reordered_result, observed_at="2026-07-25T08:05:00+00:00")
    db.commit()

    after = {row["player_provider_id"]: dict(row) for row in db.execute(
        "SELECT player_provider_id, position, first_observed_at FROM cfs_match_roster_selections"
    )}
    assert after == before
    assert db.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0] == 4


def test_genuine_position_change_updates_the_same_row_in_place(db):
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()

    changed = _collect("match_rosters_changed.json")
    persist_match_rosters(db, changed, observed_at="2026-07-25T09:00:00+00:00")
    db.commit()

    row = db.execute(
        "SELECT position, first_observed_at, last_observed_at FROM cfs_match_roster_selections "
        "WHERE player_provider_id='CD_I1'"
    ).fetchone()
    # CD_I1 moves FORWARDS -> INTERCHANGE between the two fixtures.
    assert row["position"] == "INTERCHANGE"
    assert row["first_observed_at"] == "2026-07-25T08:00:00+00:00"
    assert row["last_observed_at"] == "2026-07-25T09:00:00+00:00"
    assert db.execute(
        "SELECT COUNT(*) FROM cfs_match_roster_selections WHERE player_provider_id='CD_I1'"
    ).fetchone()[0] == 1


def test_later_valid_pre_match_update_supersedes_prior_selection_state(db):
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()

    changed = _collect("match_rosters_changed.json")
    persist_match_rosters(db, changed, observed_at="2026-07-25T09:00:00+00:00")
    db.commit()

    home_players = {row[0] for row in db.execute(
        "SELECT player_provider_id FROM cfs_match_roster_selections WHERE side='home'"
    )}
    # CD_I2 (Bea Baker) was in the FORWARDS/INTERCHANGE selection before, but
    # is absent from the changed fixture's homeTeam.positions -- the new
    # observation is the complete current selection, so the stale row is gone.
    assert "CD_I2" not in home_players
    assert home_players == {"CD_I1", "CD_I6"}


@pytest.mark.parametrize("positions", [None, {"unresolved": True}, "unresolved", 7])
def test_non_list_positions_observation_preserves_prior_selection(db, positions):
    initial = _collect("match_rosters_available.json")
    persist_match_rosters(db, initial, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()

    partial = _fixture("match_rosters_available.json")
    partial[0]["matchRoster"]["homeTeam"]["positions"] = positions
    result = MatchRosterCollector(_FakeClient(partial)).collect(ROUND_PROVIDER_ID)
    persist_match_rosters(db, result, observed_at="2026-07-25T08:15:00+00:00")
    db.commit()

    assert db.execute(
        "SELECT COUNT(*) FROM cfs_match_roster_selections "
        "WHERE team_provider_id=? AND player_provider_id='CD_I1'", (HOME_PROVIDER_ID,),
    ).fetchone()[0] == 1


def test_missing_positions_observation_preserves_prior_selection(db):
    initial = _collect("match_rosters_available.json")
    persist_match_rosters(db, initial, observed_at="2026-07-25T08:00:00+00:00")
    partial = _fixture("match_rosters_available.json")
    del partial[0]["matchRoster"]["homeTeam"]["positions"]
    result = MatchRosterCollector(_FakeClient(partial)).collect(ROUND_PROVIDER_ID)
    persist_match_rosters(db, result, observed_at="2026-07-25T08:15:00+00:00")
    db.commit()
    assert db.execute(
        "SELECT COUNT(*) FROM cfs_match_roster_selections "
        "WHERE team_provider_id=? AND player_provider_id='CD_I1'", (HOME_PROVIDER_ID,),
    ).fetchone()[0] == 1


def test_authoritative_empty_positions_replaces_prior_selection(db):
    initial = _collect("match_rosters_available.json")
    persist_match_rosters(db, initial, observed_at="2026-07-25T08:00:00+00:00")
    replacement = _fixture("match_rosters_available.json")
    replacement[0]["matchRoster"]["homeTeam"]["positions"] = []
    result = MatchRosterCollector(_FakeClient(replacement)).collect(ROUND_PROVIDER_ID)
    summary = persist_match_rosters(db, result, observed_at="2026-07-25T08:15:00+00:00")
    db.commit()
    assert db.execute(
        "SELECT COUNT(*) FROM cfs_match_roster_selections WHERE team_provider_id=?",
        (HOME_PROVIDER_ID,),
    ).fetchone()[0] == 0
    assert summary.state_changed is True


@pytest.mark.parametrize("collection", ["ins", "outs", "lateChanges", "clubDebuts", "milestones"])
def test_non_list_context_observation_preserves_prior_collection(db, collection):
    payload = _fixture("match_rosters_available.json")
    # Give every collection one verified record so the same preservation
    # assertion covers all five separately persisted context projections.
    player = payload[0]["matchRoster"]["homeTeam"]["ins"][0]
    payload[0]["matchRoster"]["homeTeam"][collection] = [player]
    initial = MatchRosterCollector(_FakeClient(payload)).collect(ROUND_PROVIDER_ID)
    persist_match_rosters(db, initial, observed_at="2026-07-25T08:00:00+00:00")

    partial = json.loads(json.dumps(payload))
    partial[0]["matchRoster"]["homeTeam"][collection] = {"unresolved": True}
    result = MatchRosterCollector(_FakeClient(partial)).collect(ROUND_PROVIDER_ID)
    persist_match_rosters(db, result, observed_at="2026-07-25T08:15:00+00:00")
    db.commit()
    assert db.execute(
        "SELECT COUNT(*) FROM cfs_match_roster_context "
        "WHERE team_provider_id=? AND context_type=?", (HOME_PROVIDER_ID, collection),
    ).fetchone()[0] == 1


def test_null_unavailable_observation_does_not_erase_prior_valid_roster(db):
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    before = db.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0]

    unavailable = RosterCollectionResult(ROUND_PROVIDER_ID, RosterStatus.UNAVAILABLE, [], [])
    summary = persist_match_rosters(db, unavailable, observed_at="2026-07-25T09:00:00+00:00")
    db.commit()

    assert summary == RosterPersistenceSummary()
    assert db.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0] == before


def test_empty_list_observation_is_conservatively_non_destructive(db):
    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    before = db.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0]

    empty = RosterCollectionResult(ROUND_PROVIDER_ID, RosterStatus.EMPTY, [], [])
    persist_match_rosters(db, empty, observed_at="2026-07-25T09:00:00+00:00")
    db.commit()

    assert db.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0] == before


def test_malformed_response_never_reaches_persistence(db):
    """MatchRosterCollector.collect raises before returning a result for a
    malformed payload -- persist_match_rosters is therefore never invoked, so
    a previously persisted roster is left completely untouched."""
    from afl_json.client import AflJsonInvalidResponse

    result = _collect("match_rosters_available.json")
    persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    before = db.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0]

    malformed_payload = {"not": "a list"}
    with pytest.raises(AflJsonInvalidResponse):
        MatchRosterCollector(_FakeClient(malformed_payload)).collect(ROUND_PROVIDER_ID)
    # No call to persist_match_rosters happens on this path (mirrors how a
    # caller such as collect_operational or the production scheduler is
    # structured -- collect() must succeed before persistence runs at all).
    assert db.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0] == before


def test_repeated_identical_published_observation_is_idempotent(db):
    add_player(db, 501, "CD_I1")
    result = _collect("match_rosters_available.json")
    first = persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    second = persist_match_rosters(db, result, observed_at="2026-07-25T08:00:00+00:00")
    db.commit()
    assert first.rosters_written == second.rosters_written
    assert first.selections_written == second.selections_written
    assert first.context_written == second.context_written
    assert first.state_changed is True
    assert first.change_magnitude > 0
    assert second.state_changed is False
    assert second.change_magnitude == 0
    assert db.execute("SELECT COUNT(*) FROM cfs_match_roster_selections").fetchone()[0] == 4
    assert db.execute("SELECT COUNT(*) FROM cfs_match_roster_context").fetchone()[0] == 2
