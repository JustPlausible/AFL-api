"""Validate the repository's AFL scraper source inventory document."""
from __future__ import annotations

import argparse
import ast
import importlib
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOC = REPO_ROOT / "docs" / "scraper_source_inventory.md"
REQUIRED_HEADINGS = [
    "Verification status",
    "Last verified",
    "Public entry point",
    "URL construction helper",
    "Current fetch method",
    "Parser entry point",
    "Selectors or structured data access",
    "Required output fields",
    "Optional output fields",
    "Database or downstream destination",
    "Existing fixture and test coverage",
]

@dataclass(frozen=True)
class ActiveScraper:
    source_id: str
    module: str
    parser_names: tuple[str, ...]
    selector_names: tuple[str, ...]

ACTIVE_SCRAPERS = (
    ActiveScraper("fixtures-rounds", "scraper.scrape_afl_fixtures", ("parse_fixtures_metadata", "parse_round_list"), ("FIXTURE_SELECTORS",)),
    ActiveScraper("matches-status", "scraper.scrape_afl_matches", ("parse_matches", "extract_match_data", "extract_season_year"), ("MATCH_CARD_SELECTORS",)),
    ActiveScraper("team-lineups", "scraper.scrape_afl_lineups", ("parse_lineups_html",), ("TEAM_LINEUP_SELECTORS",)),
    ActiveScraper("injuries", "scraper.scrape_afl_injuries", ("_scrape_injury_list", "extract_and_match_club"), ("INJURY_SELECTORS",)),
    ActiveScraper("club-squads", "scraper.scrape_afl_clubs", ("scrape_club_players",), ("CLUB_SQUAD_SELECTORS",)),
    ActiveScraper("stats-leaders-players", "scraper.scrape_afl_players", ("parse_row",), ("STATS_LEADERS_SELECTORS",)),
    ActiveScraper("match-player-stats", "scraper.scrape_afl_player_stats", ("parse_live_stats", "get_match_status_from_header"), ("PLAYER_STATS_SELECTORS",)),
)

SECTION_RE = re.compile(r"^## Source contract: (?P<id>[a-z0-9-]+)\s*$", re.MULTILINE)


def _sections(text: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        sections[match.group("id")] = text[start:end]
    return sections


def _module_attr_exists(module_name: str, attr: str) -> bool:
    module_path = REPO_ROOT / Path(*module_name.split(".")).with_suffix(".py")
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == attr for node in tree.body)


def _selector_exists(selector_name: str) -> bool:
    selectors = importlib.import_module("scraper.afl_selectors")
    return hasattr(selectors, selector_name)


def validate_inventory(path: Path = DEFAULT_DOC) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    sections = _sections(text)

    for scraper in ACTIVE_SCRAPERS:
        section = sections.get(scraper.source_id)
        if section is None:
            errors.append(f"missing source contract section for {scraper.source_id}")
            continue
        for heading in REQUIRED_HEADINGS:
            if f"### {heading}" not in section:
                errors.append(f"{scraper.source_id}: missing heading {heading!r}")
        if "Verification status" in section and "Pending verification" not in section:
            errors.append(f"{scraper.source_id}: verification state must include Pending verification for this phase")
        for parser in scraper.parser_names:
            if parser not in section:
                errors.append(f"{scraper.source_id}: parser {parser!r} is not documented")
            if not _module_attr_exists(scraper.module, parser):
                errors.append(f"{scraper.source_id}: parser {scraper.module}.{parser} does not exist")
        for selector in scraper.selector_names:
            if selector not in section:
                errors.append(f"{scraper.source_id}: selector {selector!r} is not documented")
            if not _selector_exists(selector):
                errors.append(f"{scraper.source_id}: selector scraper.afl_selectors.{selector} does not exist")

    extras = sorted(set(sections) - {s.source_id for s in ACTIVE_SCRAPERS})
    for extra in extras:
        errors.append(f"unexpected source contract section {extra}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate docs/scraper_source_inventory.md against active scraper code symbols.")
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_DOC)
    args = parser.parse_args(argv)
    errors = validate_inventory(args.path)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"OK: {args.path} documents {len(ACTIVE_SCRAPERS)} active scraper sources")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
