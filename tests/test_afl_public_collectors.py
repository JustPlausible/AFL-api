import json
from datetime import date
from pathlib import Path

import pytest

from afl_json import AflJsonResponse
from afl_json.collectors import (
    CollectionError,
    PaginationError,
    PublicAflCollector,
    determine_current_season,
    resolve_competition,
    select_season,
)


FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FixtureClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, endpoint, *, path_parameters=None, params=None):
        name = endpoint.name if hasattr(endpoint, "name") else endpoint
        self.calls.append((name, path_parameters, dict(params or {})))
        value = self.responses[name]
        if callable(value):
            value = value(path_parameters or {}, params or {})
        elif isinstance(value, list):
            value = value.pop(0)
        return AflJsonResponse(name, 200, value, {})


def hierarchy_client():
    return FixtureClient({
        "competitions": fixture("competitions.json"),
        "competition_seasons": fixture("seasons.json"),
        "rounds": fixture("rounds.json"),
        "teams": fixture("teams.json"),
        "matches": lambda _path, params: fixture(f"matches_round_{params['roundNumber']}.json"),
    })


def test_collects_complete_hierarchy_and_resolves_stable_identifiers():
    result = PublicAflCollector(hierarchy_client()).collect(season=2026)

    assert (result.competition["afl_id"], result.competition["provider_id"]) == (1, "CD_C014")
    assert (result.season["afl_id"], result.season["provider_id"]) == (85, "CD_S2026014")
    assert [item["round_number"] for item in result.rounds] == [0, 1]
    assert result.rounds[0]["byes"][0]["providerId"] == "CD_T20"
    assert result.teams[0]["name"] == "Canonical Cats"
    assert result.teams[0]["displayName"] == "Round Theme Cats"
    assert result.teams[0]["club"]["providerId"] == "CD_O1"
    scheduled, concluded = result.matches
    assert scheduled["status"] == "FUTURE_STATUS"
    assert scheduled["home_score"] is None and scheduled["away_score"] is None
    assert scheduled["venue"]["providerId"] == "CD_V6"
    assert scheduled["utc_start_time"] == "2026-03-05T08:00:00Z"
    assert scheduled["competition_season"]["providerId"] == "CD_S2026014"
    assert scheduled["round"]["providerId"] == "CD_R0"
    assert scheduled["home_team"]["providerId"] == "CD_T10"
    assert concluded["home_score"]["totalScore"] == 65
    assert concluded["away_score"]["totalScore"] == 55


def competition_records():
    return PublicAflCollector(hierarchy_client()).competitions()


def test_competition_resolver_requires_both_configured_selectors_to_match_same_record():
    selected = resolve_competition(competition_records(), code="AFL", provider_id="CD_C014")
    assert selected["afl_id"] == 1


def test_competition_resolver_rejects_when_only_one_of_two_selectors_matches():
    with pytest.raises(CollectionError, match="was not found"):
        resolve_competition(competition_records(), code="AFL", provider_id="CD_MISSING")


def test_competition_resolver_rejects_selectors_matching_different_records():
    with pytest.raises(CollectionError, match="inconsistent.*different records"):
        resolve_competition(competition_records(), code="AFLW", provider_id="CD_C014")


@pytest.mark.parametrize("code, provider_id", [("AFL", None), (None, "CD_C014")])
def test_competition_resolver_supports_one_configured_selector(code, provider_id):
    selected = resolve_competition(competition_records(), code=code, provider_id=provider_id)
    assert selected["afl_id"] == 1


def test_current_season_is_not_selected_by_highest_numeric_id():
    seasons = PublicAflCollector(hierarchy_client()).competition_seasons(1)
    with_dates = [
        {**seasons[0], "start_time": "2026-02-01T00:00:00Z", "end_time": "2026-09-30T00:00:00Z"},
        {**seasons[1], "start_time": "2025-02-01T00:00:00Z", "end_time": "2025-09-30T00:00:00Z"},
    ]
    assert select_season(with_dates, relevant_date=date(2026, 7, 1))["afl_id"] == 85
    assert select_season(seasons, selector="CD_S2026014")["year"] == 2026
    assert select_season(seasons, selector=85)["year"] == 2026


def test_live_season_shape_extracts_exact_year_from_name_without_parsing_provider_id():
    seasons = PublicAflCollector(hierarchy_client()).competition_seasons(1)

    selected = select_season(seasons, selector=2026)

    assert selected["name"] == "2026 Toyota AFL Premiership"
    assert selected["provider_id"] == "CD_S2026014"


@pytest.mark.parametrize("names", [
    ["Toyota AFL Premiership"],
    ["2026 1999 Anniversary AFL Premiership"],
    ["20260 Toyota AFL Premiership"],
])
def test_absent_ambiguous_or_embedded_season_year_fails_clearly(names):
    payload = {
        "compSeasons": [
            {"id": index + 1, "providerId": f"CD_S_YEAR_CASE_{index}",
             "name": name, "shortName": "Premiership"}
            for index, name in enumerate(names)
        ]
    }
    seasons = PublicAflCollector(FixtureClient({"competition_seasons": payload})).competition_seasons(1)

    with pytest.raises(CollectionError, match="No competition season matched.*--afl-season"):
        select_season(seasons, selector=2026)


def test_determine_current_season_prefers_the_explicit_current_flag():
    seasons = [{"afl_id": 84, "year": 2025, "current": False},
               {"afl_id": 85, "year": 2026, "current": True}]
    assert determine_current_season(seasons)["afl_id"] == 85


def test_determine_current_season_falls_back_to_date_containment_like_selection():
    seasons = [
        {"afl_id": 84, "year": 2025, "start_time": "2025-02-01T00:00:00Z", "end_time": "2025-09-30T00:00:00Z"},
        {"afl_id": 85, "year": 2026, "start_time": "2026-02-01T00:00:00Z", "end_time": "2026-09-30T00:00:00Z"},
    ]
    assert determine_current_season(seasons, relevant_date=date(2026, 7, 1))["afl_id"] == 85
    # Never the highest year/ID by default when no flag or date matches.
    assert determine_current_season(seasons, relevant_date=date(2030, 1, 1)) is None


def test_determine_current_season_is_none_rather_than_guessing():
    # No usable current flag or date on either season.
    assert determine_current_season([{"afl_id": 84, "year": 2025}, {"afl_id": 85, "year": 2026}]) is None
    # Contradictory upstream data: two seasons both flagged current.
    ambiguous = [{"afl_id": 84, "year": 2025, "current": True},
                 {"afl_id": 85, "year": 2026, "current": True}]
    assert determine_current_season(ambiguous) is None


def test_missing_and_ambiguous_season_selection_are_actionable():
    seasons = PublicAflCollector(hierarchy_client()).competition_seasons(1)
    with pytest.raises(CollectionError, match="specify --afl-season"):
        select_season(seasons, selector=2030)
    ambiguous = [{**seasons[0], "year": 2026}, {**seasons[1], "year": 2026}]
    with pytest.raises(CollectionError, match="ambiguous.*--afl-season"):
        select_season(ambiguous, selector=2026)


def test_pagination_uses_metadata_deduplicates_overlap_and_ignores_requested_size():
    client = FixtureClient({"competitions": [
        {"competitions": [{"id": 1, "providerId": "one"}, {"id": 2, "providerId": "two"}],
         "pagination": {"pageNum": 1, "totalResults": 3}},
        {"competitions": [{"id": 2, "providerId": "two"}, {"id": 3, "providerId": "three"}],
         "pagination": {"pageNum": 2, "totalResults": 3}},
    ]})
    records = PublicAflCollector(client, page_size=100).collect_endpoint("competitions")
    assert [item["id"] for item in records] == [1, 2, 3]
    assert [call[2]["pageNum"] for call in client.calls] == [1, 2]


@pytest.mark.parametrize("payload, message", [
    ({"competitions": [{"id": 1, "providerId": "CD_C014"}],
      "pagination": {"pageNum": 1, "nextPage": 1}}, "did not progress"),
    ({"competitions": [], "pagination": {"pageNum": 1, "totalResults": 2}}, "empty page"),
])
def test_pagination_detects_non_progressing_responses(payload, message):
    with pytest.raises(PaginationError, match=message):
        PublicAflCollector(FixtureClient({"competitions": payload})).collect_endpoint("competitions")


def test_diagnostic_mode_writes_deterministic_raw_files(tmp_path):
    collector = PublicAflCollector(hierarchy_client(), raw_directory=tmp_path / "raw")
    collector.collect(season=2026)
    files = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*.json"))
    assert "raw/rounds/rounds__comp_season_id-85__page-0001.json" in files
    assert "raw/matches/matches__compSeasonId-85__competitionId-1__roundNumber-0__page-0001.json" in files
    assert json.loads((tmp_path / files[0]).read_text())


def test_ordinary_collection_does_not_write_raw_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    PublicAflCollector(hierarchy_client()).collect(season=2026)
    assert list(tmp_path.rglob("*.json")) == []
