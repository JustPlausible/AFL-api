import ast
from pathlib import Path

import pytest

from scraper.afl_selectors import MATCH_CARD_SELECTORS
from scraper import scrape_afl_matches

REPO_ROOT = Path(__file__).resolve().parents[1]
ACTIVE_SCRAPER_MODULES = [
    "scraper/monitor_match_status.py",
    "scraper/scrape_afl_clubs.py",
    "scraper/scrape_afl_fixtures.py",
    "scraper/scrape_afl_injuries.py",
    "scraper/scrape_afl_lineups.py",
    "scraper/scrape_afl_matches.py",
    "scraper/scrape_afl_player_stats.py",
    "scraper/scrape_afl_players.py",
    "scraper/scrape_afl_players_with_stats.py",
]
def _all_central_selector_values() -> set[str]:
    from scraper import afl_selectors

    values = set()
    for name in dir(afl_selectors):
        if not name.endswith("_SELECTORS"):
            continue
        group = getattr(afl_selectors, name)
        for value in group.__class__.__dict__.values():
            if isinstance(value, str):
                values.add(value)
    return values


def _string_literals(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def test_active_scraper_modules_use_centralised_selector_definitions():
    selector_values = _all_central_selector_values()
    offenders = {
        module: sorted(_string_literals(REPO_ROOT / module) & selector_values)
        for module in ACTIVE_SCRAPER_MODULES
    }
    offenders = {module: values for module, values in offenders.items() if values}

    assert offenders == {}


def test_match_parser_uses_central_selector_definition_for_match_cards():
    fixture = REPO_ROOT / "tests" / "fixtures" / "afl" / "matches_opening_round_completed.html"
    html = fixture.read_text()
    original = MATCH_CARD_SELECTORS.DATE_HEADER_OR_MATCH_CARD

    try:
        object.__setattr__(MATCH_CARD_SELECTORS, "DATE_HEADER_OR_MATCH_CARD", "div.selector-changed-for-test")
        with pytest.raises(ValueError, match="No match cards found using selector div.fixtures__item"):
            scrape_afl_matches.parse_matches(html)
    finally:
        object.__setattr__(MATCH_CARD_SELECTORS, "DATE_HEADER_OR_MATCH_CARD", original)
