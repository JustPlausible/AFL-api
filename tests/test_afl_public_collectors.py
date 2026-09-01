import json
from datetime import date
from pathlib import Path

import pytest

from afl_json import AflJsonResponse
from afl_json.collectors import (
    CollectionError,
    PaginationError,
    PublicAflCollector,
    is_current_season,
    resolve_competition,
    resolve_current_season,
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
    assert [item["competition_phase"] for item in result.rounds] == [
        "HOME_AND_AWAY", "HOME_AND_AWAY"]


def test_supported_fixture_metadata_semantically_classifies_finals_without_round_rules():
    rounds = fixture("rounds.json")
    rounds["rounds"] = [rounds["rounds"][0], {
        "id": 1399, "providerId": "CD_RX", "name": "Opaque stage",
        "abbreviation": "X", "roundNumber": 99, "byes": [],
        "utcStartTime": "2026-09-01T00:00:00Z", "utcEndTime": "2026-09-02T00:00:00Z",
    }]
    client = FixtureClient({
        "competitions": fixture("competitions.json"),
        "competition_seasons": fixture("seasons.json"), "rounds": rounds,
        "teams": fixture("teams.json"),
        "matches": lambda _path, params: (
            fixture("matches_round_0.json") if params["roundNumber"] == 0 else
            {"matches": [{"id": 8999, "providerId": "CD_MX", "status": "CONCLUDED",
              "round": {"id": 1399, "providerId": "CD_RX", "roundNumber": 99},
              "home": {"team": {"id": 1}}, "away": {"team": {"id": 2}},
              "metadata": {"finals_match_label": "Source-defined championship stage"}}]}
        ),
    })
    result = PublicAflCollector(client).collect(season=2026)
    assert [(item["round_number"], item["competition_phase"]) for item in result.rounds] == [
        (0, "HOME_AND_AWAY"), (99, "FINALS")]


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


def test_is_current_season_trusts_an_explicit_upstream_flag():
    assert is_current_season({"afl_id": 85, "current": True}) is True
    assert is_current_season({"afl_id": 84, "current": False}) is False


def test_is_current_season_falls_back_to_season_level_dates_when_no_flag():
    season = {"afl_id": 85, "start_time": "2026-02-01T00:00:00Z", "end_time": "2026-09-30T00:00:00Z"}
    assert is_current_season(season, relevant_date=date(2026, 7, 1)) is True
    assert is_current_season(season, relevant_date=date(2027, 1, 1)) is False


def test_is_current_season_falls_back_to_round_dates_matching_the_live_payload_shape():
    # The live competition-season endpoint provides neither a current flag
    # nor season-level dates (docs/investigation/afl-json/ENDPOINT_CATALOG.md
    # E02); only its rounds carry real utcStartTime/utcEndTime values.
    season = {"afl_id": 85, "year": 2026}  # no "current", no start/end -- as seasons.json fixture
    rounds = PublicAflCollector(hierarchy_client()).rounds(85)

    assert is_current_season(season, rounds, relevant_date=date(2026, 3, 10)) is True
    assert is_current_season(season, rounds, relevant_date=date(2026, 6, 1)) is False


def test_is_current_season_is_none_rather_than_guessing_when_no_signal_exists():
    # No flag, no season dates, and no rounds at all: never guessed True.
    assert is_current_season({"afl_id": 85, "year": 2026}) is None
    assert is_current_season({"afl_id": 85, "year": 2026}, []) is None


def test_collect_marks_the_live_shaped_current_season_using_only_round_dates():
    # End-to-end: the collected season (via the real hierarchy_client fixture,
    # matching the live payload's lack of a current flag/season dates) is
    # still correctly identified as current from its own rounds' dates.
    result = PublicAflCollector(hierarchy_client()).collect(
        season=2026, relevant_date=date(2026, 3, 10))
    assert result.season.get("current") is None
    assert result.season.get("start_time") is None
    assert result.current_season_afl_id == result.season["afl_id"] == 85

    later = PublicAflCollector(hierarchy_client()).collect(
        season=2026, relevant_date=date(2026, 6, 1))
    assert later.current_season_afl_id is None


# --- resolve_current_season / AFL_SEASON_YEAR-style configured override ----------


def test_resolve_current_season_prefers_a_uniquely_resolved_configured_override():
    all_seasons = [{"afl_id": 84, "year": 2025}, {"afl_id": 85, "year": 2026}]
    historical = all_seasons[0]  # the operator is explicitly bootstrapping 2025...

    # ...but AFL_SEASON_YEAR=2026 still marks 2026 canonically current, even
    # though 2025 (not 2026) is the season actually being collected/persisted
    # this run, and neither season has a flag or dates.
    current = resolve_current_season(all_seasons, historical, configured_year=2026)

    assert current["afl_id"] == 85


def test_resolve_current_season_falls_back_when_configured_year_is_unresolvable():
    all_seasons = [{"afl_id": 84, "year": 2025, "current": True}, {"afl_id": 85, "year": 2026}]
    selected = all_seasons[0]

    # A misconfigured/nonexistent year is never blindly trusted; it falls
    # through to the collected season's own upstream flag.
    assert resolve_current_season(all_seasons, selected, configured_year=2099)["afl_id"] == 84

    # An ambiguous configured value (matches more than one season) is
    # likewise never silently trusted -- also falls through.
    ambiguous = [{"afl_id": 84, "year": 2025, "current": True}, {"afl_id": 86, "year": 2025}]
    assert resolve_current_season(ambiguous, ambiguous[0], configured_year=2025)["afl_id"] == 84


def test_resolve_current_season_is_none_when_nothing_resolves():
    all_seasons = [{"afl_id": 84, "year": 2025}]
    selected = all_seasons[0]
    # Unresolvable override, no flag, no dates: explicit None, never guessed.
    assert resolve_current_season(all_seasons, selected, configured_year=2099) is None
    assert resolve_current_season(all_seasons, selected) is None


def test_resolve_current_season_uses_existing_upstream_and_date_semantics_when_unconfigured():
    current = {"afl_id": 85, "current": True}
    assert resolve_current_season([current], current)["afl_id"] == 85
    not_current = {"afl_id": 84, "current": False}
    assert resolve_current_season([not_current], not_current) is None


def test_collect_configured_current_season_year_overrides_the_bootstrapped_season():
    # Mirrors the deployment scenario: AFL_SEASON_YEAR=2026 is configured,
    # but an operator explicitly runs `--bootstrap-afl-season 2025`. 2025 is
    # collected and persisted, while 2026 (already established elsewhere)
    # stays canonically current.
    result = PublicAflCollector(hierarchy_client()).collect(
        season=2025, current_season_year=2026)

    assert result.season["year"] == 2025
    assert result.current_season_afl_id == 85


def test_collect_configured_current_season_year_falls_back_when_unresolvable():
    result = PublicAflCollector(hierarchy_client()).collect(
        season=2026, current_season_year=2099, relevant_date=date(2026, 3, 10))

    # The misconfigured override cannot be resolved, so persistence falls
    # back to the bootstrapped season's own (round-derived) date signal
    # rather than leaving current-season state undefined.
    assert result.current_season_afl_id == 85


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
