from pathlib import Path

import pytest

from scraper.scrape_afl_fixtures import parse_fixtures_metadata, parse_round_list
from scraper import scrape_afl_matches

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "afl"


def read_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


@pytest.fixture(autouse=True)
def deterministic_club_codes(monkeypatch):
    codes = {
        "Sydney Swans": "SYD",
        "Carlton": "CARL",
        "Gold Coast SUNS": "GCFC",
        "Geelong Cats": "GEEL",
        "GWS GIANTS": "GWS",
        "Hawthorn": "HAW",
        "Brisbane Lions": "BL",
        "Western Bulldogs": "WB",
        "St Kilda": "STK",
        "Collingwood": "COLL",
    }
    monkeypatch.setattr(scrape_afl_matches, "resolve_club_code", codes.__getitem__)


def test_fixture_metadata_extraction():
    metadata = parse_fixtures_metadata(read_fixture("fixture_index_rounds.html"))

    assert metadata == {
        "season_pid": "CD_S2026014",
        "season_id": 85,
        "competition_id": 1,
        "default_round_id": 20,
        "special_round": "None",
    }


def test_round_list_extraction_begins_with_captured_rounds():
    rounds = parse_round_list(read_fixture("fixture_index_rounds.html"))

    assert rounds[:3] == [
        {"round_id": 1343, "round_label": "OR"},
        {"round_id": 1344, "round_label": "1"},
        {"round_id": 1345, "round_label": "2"},
    ]


def test_opening_round_match_cards_parse_expected_fields_without_season_label():
    html = read_fixture("matches_opening_round_completed.html")

    assert scrape_afl_matches.extract_season_year(html) is None
    matches = scrape_afl_matches.parse_matches(html)

    assert len(matches) == 5
    assert matches == [
        {
            "match_id": 8041,
            "match_provider_id": "CD_M20260140001",
            "round_id": 1343,
            "status": "COMPLETED",
            "home_team": "SYD",
            "away_team": "CARL",
            "venue": "SCG, Sydney • Gadigal",
            "start_time_utc": "2026-03-05T08:30:00+00:00",
            "score_home": 132,
            "score_away": 69,
            "match_time_label": "FULL TIME",
        },
        {
            "match_id": 8042,
            "match_provider_id": "CD_M20260140002",
            "round_id": 1343,
            "status": "COMPLETED",
            "home_team": "GCFC",
            "away_team": "GEEL",
            "venue": "People First Stadium, Gold Coast • Yugambeh",
            "start_time_utc": "2026-03-06T09:05:00+00:00",
            "score_home": 125,
            "score_away": 69,
            "match_time_label": "FULL TIME",
        },
        {
            "match_id": 8040,
            "match_provider_id": "CD_M20260140003",
            "round_id": 1343,
            "status": "COMPLETED",
            "home_team": "GWS",
            "away_team": "HAW",
            "venue": "ENGIE Stadium, Sydney • Wangal",
            "start_time_utc": "2026-03-07T05:15:00+00:00",
            "score_home": 122,
            "score_away": 95,
            "match_time_label": "FULL TIME",
        },
        {
            "match_id": 8043,
            "match_provider_id": "CD_M20260140004",
            "round_id": 1343,
            "status": "COMPLETED",
            "home_team": "BL",
            "away_team": "WB",
            "venue": "Gabba, Brisbane • Yuggera - Turrbal",
            "start_time_utc": "2026-03-07T08:35:00+00:00",
            "score_home": 106,
            "score_away": 111,
            "match_time_label": "FULL TIME",
        },
        {
            "match_id": 8046,
            "match_provider_id": "CD_M20260140005",
            "round_id": 1343,
            "status": "COMPLETED",
            "home_team": "STK",
            "away_team": "COLL",
            "venue": "MCG, Melbourne • Wurundjeri",
            "start_time_utc": "2026-03-08T08:20:00+00:00",
            "score_home": 66,
            "score_away": 78,
            "match_time_label": "FULL TIME",
        },
    ]


def test_missing_required_match_selector_reports_context():
    html = read_fixture("matches_opening_round_completed.html").replace(
        "fixtures__match-venue", "fixtures__match-venue-removed", 1
    )

    with pytest.raises(ValueError, match="Missing required selector '.fixtures__match-venue'.*8041"):
        scrape_afl_matches.parse_matches(html)


def test_no_match_cards_reports_meaningful_failure():
    html = read_fixture("matches_opening_round_completed.html").replace(
        "fixtures__item", "fixtures__item-removed"
    )

    with pytest.raises(ValueError, match="No match cards found using selector div.fixtures__item"):
        scrape_afl_matches.parse_matches(html)


def test_parser_tests_do_not_use_playwright_or_network(monkeypatch):
    def fail_network(*args, **kwargs):
        raise AssertionError("fixture parser tests must not use Playwright or network access")

    monkeypatch.setattr(scrape_afl_matches, "load_page_with_playwright", fail_network)

    matches = scrape_afl_matches.parse_matches(read_fixture("matches_opening_round_completed.html"))

    assert len(matches) == 5
