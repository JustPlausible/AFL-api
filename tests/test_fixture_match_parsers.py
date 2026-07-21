from pathlib import Path

import pytest

from scraper.scrape_afl_fixtures import parse_fixtures_metadata, parse_round_list
from scraper import scrape_afl_matches as matches
from scraper.scrape_afl_matches import FixtureParseError, extract_season_year, parse_matches_from_html

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "afl"


def read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_fixture_index_metadata_extraction():
    """Scenario: saved AFL fixture index page exposes season metadata."""
    metadata = parse_fixtures_metadata(read_fixture("fixture_index_rounds.html"))

    assert metadata == {
        "season_pid": "CD_S2026014",
        "season_id": 62,
        "competition_id": 1,
        "default_round_id": 1167,
        "special_round": "false",
    }


def test_fixture_index_round_list_extraction():
    """Scenario: saved AFL fixture index page exposes the available round list."""
    rounds = parse_round_list(read_fixture("fixture_index_rounds.html"))

    assert rounds == [
        {"round_id": 1167, "round_label": "Opening Round"},
        {"round_id": 1168, "round_label": "Round 1"},
        {"round_id": 1169, "round_label": "Round 2"},
    ]


def test_match_page_season_extraction():
    """Scenario: saved AFL round page exposes the season year label."""
    assert extract_season_year(read_fixture("matches_round_scheduled_and_completed.html")) == 2026


def test_match_page_extracts_expected_match_count_and_fields(monkeypatch):
    """Scenario: saved AFL round page covers completed and scheduled match states."""
    monkeypatch.setattr(
        matches,
        "resolve_club_code",
        lambda name: {
            "Richmond": "RIC",
            "Carlton": "CAR",
            "Collingwood": "COL",
            "Essendon": "ESS",
        }[name],
    )
    monkeypatch.setattr(matches, "load_page_with_playwright", pytest.fail)

    parsed_matches = parse_matches_from_html(read_fixture("matches_round_scheduled_and_completed.html"))

    assert len(parsed_matches) == 2
    assert parsed_matches[0] == {
        "match_id": 8001,
        "match_provider_id": "CD_M20260140101",
        "round_id": 1168,
        "status": "COMPLETED",
        "home_team": "RIC",
        "away_team": "CAR",
        "venue": "MCG",
        "start_time_utc": "2026-03-06T08:40:00+00:00",
        "score_home": 87,
        "score_away": 73,
        "match_time_label": "Full Time",
    }
    assert parsed_matches[1] == {
        "match_id": 8002,
        "match_provider_id": "CD_M20260140102",
        "round_id": 1168,
        "status": "UPCOMING",
        "home_team": "COL",
        "away_team": "ESS",
        "venue": "Marvel Stadium",
        "start_time_utc": "2026-03-07T05:35:00+00:00",
        "score_home": None,
        "score_away": None,
        "match_time_label": "4:35pm",
    }


def test_match_parser_reports_clear_error_for_missing_required_selector(monkeypatch):
    """Scenario: malformed fixture HTML is missing the required away-team selector."""
    monkeypatch.setattr(matches, "resolve_club_code", lambda name: name)
    html = read_fixture("matches_round_scheduled_and_completed.html").replace(
        '<div class="fixtures__match-team fixtures__match-team--away"><span>Carlton</span></div>',
        "",
    )

    with pytest.raises(FixtureParseError, match="Match 8001 is missing required field 'away_team'.*fixtures__match-team--away"):
        parse_matches_from_html(html)


def test_match_parser_reports_clear_error_when_no_match_items_exist():
    """Scenario: structurally invalid fixture HTML contains no match item cards."""
    with pytest.raises(FixtureParseError, match=r"No fixture match items found.*div\.fixtures__item"):
        parse_matches_from_html("<html><body><p>No fixture cards here.</p></body></html>")
