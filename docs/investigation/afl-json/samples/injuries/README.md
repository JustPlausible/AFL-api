# AFL Injury List — 2026 Finals Evidence

## Capture

- Source: https://www.afl.com.au/matches/injury-list
- Capture date: 25 August 2026
- Local timezone: AWST (UTC+8)
- Competition season: 2026 AFL
- Page state: 10 teams displayed

## Purpose

These files preserve the live AFL injury-list page during the 2026 finals
for investigation of AFL-api Issue #213 / PR #214.

At this point in the season the AFL injury page displayed only the 10 teams
remaining in the finals rather than all 18 AFL clubs.

This provides evidence for two behaviours under investigation:

1. Whether ordinary HTTP acquisition contains the same injury data required
   by the parser as a browser-rendered page, and therefore whether Playwright
   remains necessary.
2. Whether injury persistence correctly treats teams omitted from the source
   as "not observed" rather than as having zero injuries.

## Files

### injury_list_2026_finals_10teams_http_2026-08-25.html

Captured using a direct HTTP request without browser rendering.

### injury_list_2026_finals_10teams_rendered_2026-08-25.html

Captured from the DOM after loading the page in Chrome.

This is browser-rendered evidence and should not be described as a
Playwright capture.

## Manually observed teams

The following 10 team sections were visibly present on the page at capture
time:

- Adelaide
- Brisbane
- Carlton
- Collingwood
- Fremantle
- Geelong
- Hawthorn
- Melbourne
- Sydney
- Western Bulldogs

The remaining eight AFL clubs were not represented on the page.

Absence of a team from this source must not be interpreted as evidence that
the team has zero current injuries.

## Source metadata

The captured source identifies the article as:

- Title: `2.0 AFL Injury List`
- `dateModified`: `2026-08-24T07:55:26.435Z`

These values are source metadata and are distinct from the local capture
time.

## Notes

The raw evidence files should be retained unchanged.

Tests may use derived/minimised fixtures where appropriate rather than
modifying these source captures.