from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment

from scraper.afl_selectors import INJURY_SELECTORS
from .models import InjuryParseResult, InjuryParserDiagnostic, ParsedInjuryRecord, ParsedTeamBlock


def parse_injuries_html(html: str) -> InjuryParseResult:
    """Deterministically parse caller-supplied HTML without I/O or identity lookup."""
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select_one(INJURY_SELECTORS.ARTICLE_BODY):
        raise ValueError(
            f"Injury source contract missing article body '{INJURY_SELECTORS.ARTICLE_BODY}'"
        )
    blocks = soup.select(INJURY_SELECTORS.TEAM_BLOCKS)
    if not blocks:
        raise ValueError(
            f"Injury source contract contains no team blocks '{INJURY_SELECTORS.TEAM_BLOCKS}'"
        )
    records, diagnostics, teams = [], [], []
    for index, block in enumerate(blocks):
        comment = block.find(string=lambda value: isinstance(value, Comment))
        image_soup = BeautifulSoup(comment, "html.parser") if comment else None
        image = image_soup.find("img", class_=INJURY_SELECTORS.PROMO_IMAGE_CLASS) if image_soup else None
        if not image or not image.get("src"):
            raise ValueError(f"Injury team block {index} is missing its commented promo image")
        wrapper = block.find_next_sibling()
        if wrapper is not None and wrapper.name == "table":
            table = wrapper
        elif wrapper is not None and "table" in wrapper.get("class", []):
            table = wrapper.find("table")
        else:
            table = None
        if table is None:
            # Real production pages have been observed to append a trailing,
            # non-team promotional widget that reuses this same team-block
            # markup (commented promo image included) but has no following
            # table at all -- e.g. a "Play Pack & Play" house ad appended
            # after the last real team section. Treat a block with no table
            # as non-team content rather than a structural break, since every
            # genuine team block observed live does have one.
            diagnostics.append(InjuryParserDiagnostic(
                "non_team_widget_skipped",
                "Team block has no following table; treated as non-team content",
                index,
            ))
            continue
        updated = ""
        player_rows = []
        for row_index, row in enumerate(table.find_all("tr")[1:], start=1):
            cells = row.find_all("td")
            if len(cells) >= 3:
                player_rows.append(cells)
            elif len(cells) == 1 and "updated:" in cells[0].get_text().lower():
                match = re.search(r"updated:\s*(.+)", cells[0].get_text(" ", strip=True), re.I)
                updated = match.group(1).strip() if match else ""
            elif len(cells) == 1 and not cells[0].get_text(" ", strip=True):
                # The update row is optional and may be rendered empty.
                continue
            elif cells:
                raise ValueError(
                    f"Injury table {index} has unexpected row {row_index} with {len(cells)} cells"
                )
        if not updated:
            diagnostics.append(InjuryParserDiagnostic(
                "missing_optional_updated", "Team table has no update date", index
            ))
        # Record this team block's coverage regardless of row count: a block
        # with zero rows is still an authoritative empty list for that team,
        # distinct from a team never appearing on the page at all.
        teams.append(ParsedTeamBlock(
            team_index=index,
            club_image_src=image["src"],
            club_image_alt=image.get("alt", "").strip(),
            updated=updated,
            row_count=len(player_rows),
        ))
        for cells in player_rows:
            records.append(ParsedInjuryRecord(
                player_name=cells[0].get_text(" ", strip=True),
                injury=cells[1].get_text(" ", strip=True),
                estimated_return=cells[2].get_text(" ", strip=True),
                updated=updated,
                club_image_src=image["src"],
                club_image_alt=image.get("alt", "").strip(),
            ))
    return InjuryParseResult(tuple(records), len(teams), tuple(diagnostics), tuple(teams))
