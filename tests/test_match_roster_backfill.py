import sqlite3
from types import SimpleNamespace

import pytest

import cli
from afl_json.roster_backfill import sync_match_rosters
from afl_json.rosters import RosterCollectionResult, RosterPersistenceSummary, RosterStatus


def database(rounds=(1, 2, 3)):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE afl_competitions(afl_id INTEGER PRIMARY KEY, provider_id TEXT, code TEXT);
        CREATE TABLE afl_seasons(afl_id INTEGER PRIMARY KEY, competition_id INTEGER, name TEXT, year INTEGER);
        CREATE TABLE rounds(round_id INTEGER PRIMARY KEY, season_id INTEGER, round_number INTEGER, provider_id TEXT);
        INSERT INTO afl_competitions VALUES(1, 'CD_S1', 'AFL');
        INSERT INTO afl_seasons VALUES(85, 1, '2026 AFL Premiership Season', 2026);
    """)
    conn.executemany("INSERT INTO rounds VALUES(?,?,?,?)", [
        (100 + number, 85, number, f"CD_R{number}") for number in rounds
    ])
    return conn


class Collector:
    calls = []
    outcomes = {}

    def __init__(self, _client, *, raw_directory=None):
        self.raw_directory = raw_directory

    def collect(self, provider_id):
        self.calls.append(provider_id)
        outcome = self.outcomes.get(provider_id, RosterStatus.PUBLISHED)
        if isinstance(outcome, Exception):
            raise outcome
        return RosterCollectionResult(provider_id, outcome, [], [])


@pytest.fixture(autouse=True)
def fake_components(monkeypatch):
    Collector.calls = []
    Collector.outcomes = {}
    monkeypatch.setattr("afl_json.roster_backfill.MatchRosterCollector", Collector)
    monkeypatch.setattr(
        "afl_json.roster_backfill.persist_match_rosters",
        lambda *_args, **_kwargs: RosterPersistenceSummary(rosters_written=2),
    )


def run(conn, **selectors):
    return sync_match_rosters(
        object(), conn, year=2026, competition_code="AFL",
        competition_provider_id="CD_COMP", **selectors,
    )


def test_one_round_collects_exactly_once():
    result = run(database(), round_number=2)
    assert Collector.calls == ["CD_R2"]
    assert [item.round_number for item in result.rounds] == [2]


def test_inclusive_range_collects_each_canonical_round_once():
    result = run(database(), round_from=1, round_to=3)
    assert Collector.calls == ["CD_R1", "CD_R2", "CD_R3"]
    assert result.outcome == "success"


def test_whole_season_collects_each_round_once():
    run(database((0, 1, 2, 3)))
    assert Collector.calls == ["CD_R0", "CD_R1", "CD_R2", "CD_R3"]


def test_historical_concluded_status_is_not_part_of_selection():
    # The round query deliberately has no match lifecycle/status predicate.
    run(database(), round_number=1)
    assert Collector.calls == ["CD_R1"]


def test_unavailable_and_failure_do_not_stop_later_rounds():
    Collector.outcomes = {
        "CD_R1": RosterStatus.UNAVAILABLE,
        "CD_R2": RuntimeError("provider broke"),
    }
    result = run(database())
    assert Collector.calls == ["CD_R1", "CD_R2", "CD_R3"]
    assert [item.outcome for item in result.rounds] == ["unavailable", "failed", "published"]
    assert result.outcome == "partial"


def test_empty_and_unavailable_never_reach_persistence(monkeypatch):
    persisted = []
    monkeypatch.setattr("afl_json.roster_backfill.persist_match_rosters",
                        lambda *_args, **_kwargs: persisted.append(True))
    Collector.outcomes = {"CD_R1": RosterStatus.EMPTY, "CD_R2": RosterStatus.UNAVAILABLE}
    run(database((1, 2)))
    assert persisted == []


def test_published_uses_existing_canonical_writer(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "afl_json.roster_backfill.persist_match_rosters",
        lambda _conn, result, **_kwargs: (
            persisted.append(result.round_provider_id) or RosterPersistenceSummary(rosters_written=2)
        ),
    )
    run(database((1,)))
    assert persisted == ["CD_R1"]


@pytest.mark.parametrize("arguments", [
    ["--round", "1", "--round-from", "1", "--round-to", "2"],
    ["--round-from", "1"],
    ["--round-from", "3", "--round-to", "2"],
    ["--match-id", "8"],
])
def test_invalid_roster_selectors_exit_two(arguments):
    with pytest.raises(SystemExit, match="2"):
        cli.handle_args(["--sync-match-rosters", "2026", *arguments])


def test_missing_bounded_round_fails_before_collection():
    with pytest.raises(ValueError, match="missing canonical rounds"):
        run(database((1, 3)), round_from=1, round_to=3)
    assert Collector.calls == []


@pytest.mark.parametrize(("selectors", "message"), [
    ({"round_number": 2, "round_from": 1, "round_to": 3},
     "round_number cannot be combined"),
    ({"round_number": 2, "round_from": 1},
     "round_number cannot be combined"),
    ({"round_number": 2, "round_to": 3},
     "round_number cannot be combined"),
    ({"round_from": 1}, "round_from and round_to must be supplied together"),
    ({"round_to": 3}, "round_from and round_to must be supplied together"),
    ({"round_from": 3, "round_to": 1}, "round_from cannot be greater than round_to"),
])
def test_reusable_sync_rejects_invalid_selectors_before_database_or_collection(
    selectors, message,
):
    class QueryForbiddenConnection:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("selector validation must happen before database access")

    with pytest.raises(ValueError, match=message):
        run(QueryForbiddenConnection(), **selectors)
    assert Collector.calls == []


def test_structured_result_contains_aggregates_and_rounds():
    Collector.outcomes = {"CD_R2": RosterStatus.EMPTY}
    payload = run(database((1, 2))).to_dict()
    assert payload["aggregates"]["rounds_selected"] == 2
    assert payload["aggregates"]["rounds_conservative_empty"] == 1
    assert payload["aggregates"]["rounds_requiring_attention"] == [2]
    assert payload["rounds"][1]["outcome"] == "conservative_empty"


def test_no_legacy_lineup_dependency_is_imported():
    import afl_json.roster_backfill as module
    names = set(module.sync_match_rosters.__code__.co_names)
    assert "scrape_team_lineups" not in names
    assert "save_lineups_to_db" not in names
