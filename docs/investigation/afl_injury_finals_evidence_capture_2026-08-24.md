# Injury-list finals-boundary evidence capture attempt (Issue #213)

**Attempted:** 2026-08-24<br>
**Outcome:** Live capture blocked; no new fixture added.

**Superseded 2026-08-25:** an operator with unrestricted network access
captured the exact evidence the "Required follow-up" section below asked
for -- a paired plain HTTP + browser-rendered capture of the same 10-team
finals page -- and added it directly to PR #214 at
`docs/investigation/afl-json/samples/injuries/`. Running the unchanged
`parse_injuries_html()` over both showed they are materially equivalent, and
injury acquisition now uses plain HTTP (Playwright removed). See
`docs/architecture/injury_collector_pipeline.md`'s "Acquisition decision:
Playwright replaced by plain HTTP" section for the full analysis. This
document is retained unmodified below as the historical record of why the
comparison could not be completed from this repository's own execution
environment.

## Why this capture was attempted

As of 2026-08-24, `https://www.afl.com.au/matches/injury-list` is reported to
show only the 10 teams remaining in the AFL finals series, rather than all 18
teams. This is a temporary, valuable real-world boundary case for the partial
source coverage semantics fixed in this issue (a team's absence from the page
is not evidence that team has zero injuries), and Issue #213 asked that it be
preserved as fixture/evidence material before the page reverts to a full
18-team state next round.

## What was attempted

Two acquisition methods were attempted directly against the live URL from
this execution environment:

1. **Plain HTTP**, using the repository's existing scraper HTTP conventions
   (`utils/http_utils.py`'s default headers/user agent) via `curl`.
2. **A general-purpose fetch tool** capable of following redirects and
   rendering page content.

Both were blocked before any origin response could be inspected:

* The plain HTTP attempt received `HTTP/1.1 403 Forbidden` from this
  environment's own egress proxy, with the connection failing at
  `CONNECT tunnel failed, response 403` -- i.e. the block happened in this
  sandbox's network policy, before a TLS connection to `afl.com.au` was even
  established.
* The general-purpose fetch tool reported `EGRESS_BLOCKED` for
  `www.afl.com.au` for the same reason.

This is the same failure mode already recorded in
`docs/scraper_source_inventory.md` for the 2026-07-28 investigation: "Direct
requests to all inspected `www.afl.com.au` pages ... were blocked by the
execution environment's HTTPS proxy with `403 Forbidden` before an origin
response was received." Nothing about that constraint has changed between
2026-07-28 and this attempt.

## What this means for Issue #213

* **No new 10-team finals fixture was captured**, because this execution
  environment cannot reach the live source at all -- neither over plain HTTP
  nor via a rendered fetch. Per the issue's own contingency instructions,
  fixture structure for that state was **not fabricated**.
* The existing committed fixtures remain the only available evidence:
  * `tests/fixtures/afl/injuries/injury_list_round_21_2026_2026-07-29_rendered.html`
    -- a full 18-team rendered capture from 2026-07-29 (pre-finals), used as
    the parser contract's golden fixture.
  * `tests/fixtures/afl_sources/html_rendered/injuries_round_21_populated.html`
    -- a minimal 2-team fixture already containing exactly the boundary case
    this issue's persistence fix depends on: one team with a listed injury row
    and one team present with **zero** rows (`tests/test_injury_collection_pipeline.py::test_pure_parser_records_team_coverage_including_zero_row_block`).
    It does not, however, demonstrate a team *absent* from the page entirely,
    which is the finals-specific case.
* The acquisition-method comparison requested by Issue #213 (plain HTTP vs.
  Playwright vs. a structured source, run against the same live page) could
  not be performed for the same reason: there is no live response to compare
  against in this environment. The acquisition decision recorded in
  `docs/architecture/injury_collector_pipeline.md` and
  `docs/scraper_source_inventory.md` is therefore unchanged from the prior
  investigation -- Playwright is retained, not because parity was disproven,
  but because it was never possible to test.

## Required follow-up (while the finals window is still open)

An operator with unrestricted network access should, before the finals
conclude and the page reverts to an 18-team state:

1. Capture the live page's rendered HTML (e.g. via the same DevTools
   "Copy outerHTML" of `div.article__body.article-body` used for the existing
   2026-07-29 fixture) and, separately, the plain HTTP response body for the
   same URL at approximately the same time.
2. Record which of the 10 finalist teams appear, confirm zero-row vs.
   absent-team distinctions are visible in the markup, and diff the two
   acquisition methods' team count, club markers, player/injury/return values,
   and `Updated:` text.
3. Add the result as a new dated fixture pair (rendered + plain HTTP) with a
   metadata file following the existing convention in
   `tests/fixtures/afl/injuries/*.metadata.json`, explicitly noting the
   10-team finals state and capture date.
4. If plain HTTP is found to carry the full parser contract, re-open the
   acquisition-method decision recorded here and in
   `docs/architecture/injury_collector_pipeline.md`.

This document intentionally records only the fact and mechanism of the
capture failure -- no credentials, tokens, or response bodies were obtained.
