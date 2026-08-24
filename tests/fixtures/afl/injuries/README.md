# AFL Injury List Fixtures

## 2026 finals-window boundary case (Issue #213)

As of 2026-08-24, the live injury page is reported to show only the 10 teams
remaining in the AFL finals series, rather than all 18 AFL teams. This is
temporary but important real-world evidence for partial source coverage: a
team's absence from the page must never be treated as "that team has zero
injuries" (see `docs/architecture/injury_collector_pipeline.md`).

**No fixture for this specific 10-team finals state is committed here.** A
live capture was attempted on 2026-08-24 and was blocked by this execution
environment's own network policy before any origin response -- see
`docs/investigation/afl_injury_finals_evidence_capture_2026-08-24.md` for the
full record. Per repository policy, fixture structure for a state that could
not actually be observed was not fabricated. An operator with unrestricted
network access should still capture this state (rendered + plain HTTP, per
that investigation doc) before the finals conclude and the page reverts to an
18-team state.

The closest committed evidence for the "team present with zero rows" half of
this boundary case is
`tests/fixtures/afl_sources/html_rendered/injuries_round_21_populated.html`,
which already has one team with a listed row and one team with a table but no
rows. It does not, however, demonstrate a team missing from the page
entirely -- only a live finals-window capture can.

## Purpose

These fixtures support development and regression testing of the AFL injury
list parser.

The AFL injury-list page has not yet been observed loading a dedicated public
AFL or CFS JSON payload containing the injury table data. The current source
therefore appears to be editorial HTML embedded within the article body.

## Source

Page:

`https://www.afl.com.au/matches/injury-list`

Current preferred source classification:

- source type: rendered/editorial HTML
- JSON equivalent: not identified
- authentication: not required for the rendered page
- player identity links: not supplied
- Champion Data player IDs: not supplied
- AFL numeric player IDs: not supplied

## Fixtures

### `injury_list_round_21_2026_2026-07-29_rendered.html`

Rendered outer HTML captured from:

`div.article__body.article-body`

Capture context:

- season: 2026
- before Round 21
- AFL numeric round ID: `1364`
- captured: 29 July 2026
- most team tables updated: 28 July 2026

This is an HTML fragment rather than a complete HTML document.

## Observed structure

Each club section generally contains:

1. a club-branded image separator;
2. an injury table;
3. an update-date row;
4. an `In the mix` heading;
5. an editorial paragraph.

The injury tables use the following columns:

- `PLAYER`
- `INJURY`
- `ESTIMATED RETURN`

The final table row usually contains text such as:

`Updated: July 28, 2026`

## Parsing considerations

The tables do not expose stable club identifiers directly.

Club identity currently needs to be resolved from one or more of:

- the preceding club-branded image URL;
- the order of club sections;
- nearby editorial content;
- a maintained club-name or image-filename mapping.

The parser should not rely on inline styles such as column widths or row
heights because these vary between tables.

Player names are plain text. They should be joined to the canonical player
identity dataset after parsing rather than treated as authoritative IDs.

## Canonical output

A parsed injury record should contain at least:

```json
{
  "season": 2026,
  "round_context": 21,
  "team_id": null,
  "team_name": "Adelaide Crows",
  "player_name": "Hugh Bond",
  "afl_player_id": null,
  "champion_data_player_id": null,
  "injury": "Hamstring",
  "estimated_return_raw": "2-3 weeks",
  "team_updated_date": "2026-07-28",
  "captured_at": "2026-07-29",
  "source": "afl_injury_list_html"
}