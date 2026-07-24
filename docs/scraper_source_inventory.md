# AFL scraper source inventory

Last updated: 2026-07-24

This is the repository-analysis phase for GitHub Issue #47. It records what the current code directly supports and deliberately marks live-source conclusions as **Pending verification** until pages, embedded data, and browser traffic are inspected.

## Summary decision matrix

| Source family | Active module | Public entry point | Current fetch method | Current documented recommendation | Verification status |
|---|---|---|---|---|---|
| Fixtures and rounds | `scraper.scrape_afl_fixtures` | `update_fixture_cache()` | `load_page_with_playwright()` | Pending verification | Pending verification |
| Match details and status | `scraper.scrape_afl_matches` | `run()` / `scrape_round()` | `load_page_with_playwright()` | Pending verification | Pending verification |
| Team lineups | `scraper.scrape_afl_lineups` | `scrape_team_lineups()` / `scrape_match_lineup()` | Playwright browser | Pending verification | Pending verification |
| Injuries | `scraper.scrape_afl_injuries` | `scrape_injury_list()` | Playwright browser | Pending verification | Pending verification |
| Club squads | `scraper.scrape_afl_clubs` | `scrape_club_players()` / `save_club_players_to_json()` | Playwright browser | Pending verification | Pending verification |
| Stats leaders player index | `scraper.scrape_afl_players` | `scrape_afl_stats_leaderboard()` | Playwright browser | Pending verification | Pending verification |
| Match player statistics | `scraper.scrape_afl_player_stats` | `run_scraper()` | `load_page_with_playwright()` with retry loop | Pending verification | Pending verification |

## Active scraper enumeration

Active scrapers are the modules referenced by CLI, scheduler, tests, or current import paths and not clearly named as historical. `scraper/scrape_afl_lineups-early2025.py` is excluded as archived/historical because the filename marks it as an early-2025 variant and the active lineup module is `scraper/scrape_afl_lineups.py`. Uncertain case for review: `scraper/scrape_afl_players_with_stats.py` writes a BBBFFL CSV and is not referenced by CLI/scheduler/tests found in this phase; it may be a utility or historical extractor rather than an active AFL-api scraper.

## Source contract: fixtures-rounds

### Verification status
Pending verification.

### Last verified
Code inventory only: 2026-07-24. Live page evidence: Pending verification.

### Public entry point
`scraper.scrape_afl_fixtures.update_fixture_cache()` wraps `_update_fixture_cache()` in scrape-run audit metadata.

### URL construction helper
`utils.afl_urls.get_fixture_url()` builds `${config.AFL_BASE_URL}/fixture`.

### Current fetch method
`utils.http_utils.load_page_with_playwright(url)`.

### Parser entry point
`parse_fixtures_metadata(html)` and `parse_round_list(html)`.

### Selectors or structured data access
`FIXTURE_SELECTORS`: `METADATA_ROOT_CLASS`, `ROUND_LIST_ITEMS`, and `ROUND_LABEL_BUTTON`. Structured data currently means DOM `data-*` attributes read from the fixture metadata root and round list items.

### Required output fields
Round rows require `round_id` and `round_label`. Metadata requires `season_pid`, `season_id`, `competition_id`, `default_round_id`, and `special_round` before saving occurs.

### Optional output fields
None identified in code; live missing-data states are Pending verification.

### Database or downstream destination
`db.import_to_db.save_rounds_to_db(rounds, metadata, conn)` writes to the `rounds` table in `data/afl_players.db`.

### Existing fixture and test coverage
`tests/fixtures/afl/fixture_index_rounds.html` and `tests/test_fixture_match_parsers.py` cover fixture parser behavior. `tests/test_scraper_audit_entrypoints.py` covers the audited entry point.

## Source contract: matches-status

### Verification status
Pending verification.

### Last verified
Code inventory only: 2026-07-24. Live page evidence: Pending verification.

### Public entry point
`scraper.scrape_afl_matches.run(round_id=None)` and `scrape_round(round_id, conn)`.

### URL construction helper
`utils.afl_urls.get_fixture_url_for_round(round_id)` builds `${config.AFL_BASE_URL}/fixture?Competition=${config.AFL_COMPETITION_ID}&Season=${config.AFL_SEASON_ID}&Round={round_id}`.

### Current fetch method
`utils.http_utils.load_page_with_playwright(url)`.

### Parser entry point
`parse_matches(html, existing_matches=None)`, `extract_match_data(div, season_year=None, existing_match=None)`, and `extract_season_year(html)`.

### Selectors or structured data access
`MATCH_CARD_SELECTORS`: season label, date header or match card, match card class, home/away names, venue, details link, match-time label, status label, score total, and live clock. Structured data currently means match card `data-match-id`, `data-match-provider-id`, `data-round-id`, and `data-match-status` attributes plus `aria-label` date/time text.

### Required output fields
`match_id`, `match_provider_id`, `round_id`, `status`, `home_team`, `away_team`, and `venue` are populated from required selectors/attributes during parse.

### Optional output fields
`start_time_utc`, `score_home`, `score_away`, and `match_time_label` may be absent depending on page state and existing database fallback.

### Database or downstream destination
`db.import_to_db.save_matches_to_db(matches, conn)` writes to the `matches` table in `data/afl_players.db`.

### Existing fixture and test coverage
`tests/fixtures/afl/matches_opening_round_completed.html` and `tests/test_fixture_match_parsers.py` cover match parser behavior. `tests/test_scraper_audit_entrypoints.py` covers the audited entry point.

## Source contract: team-lineups

### Verification status
Pending verification.

### Last verified
Code inventory only: 2026-07-24. Live page evidence: Pending verification.

### Public entry point
`scraper.scrape_afl_lineups.scrape_team_lineups(round_number=0)` and `scrape_match_lineup(match_id)`.

### URL construction helper
The active code currently uses the literal `https://www.afl.com.au/matches/team-lineups`; `utils.afl_urls.get_lineups_url()` exists but is not used by the active scraper.

### Current fetch method
Direct Playwright usage through `sync_playwright()`, `page.goto()`, selector waits, optional round button click, expand-all toggle click, and `page.content()`.

### Parser entry point
`parse_lineups_html(html, round_number)`.

### Selectors or structured data access
`TEAM_LINEUP_SELECTORS`: match item, ready state, header link/name, home/away ins-and-outs grids, player names, player entries, first/last name spans, home-team player-entry class, round list ready selector, round button template, and expand-lineups toggle. Structured access currently includes match IDs parsed from match links and AFL IDs parsed from player links.

### Required output fields
Each returned player dict includes `round_number`, `match_id`, `afl_id`, `first_name`, `surname`, `team`, `position_group`, `champion_id`, and `scraped_at`; in practice `afl_id`, names, and `champion_id` can be `None` in some code paths.

### Optional output fields
No additional optional fields are currently emitted. Page states such as unannounced lineups or unavailable expand controls are Pending verification.

### Database or downstream destination
The scraper returns player dicts. CLI code persists them via `db.import_to_db.save_lineups_to_db(players, conn, round_number)`; scheduled paths currently audit rows read.

### Existing fixture and test coverage
`tests/test_lineup_cli.py` covers CLI argument paths and match-round resolution. `tests/test_scraper_audit_entrypoints.py` covers the audited entry points. No static HTML lineup fixture was found in this phase.

## Source contract: injuries

### Verification status
Pending verification.

### Last verified
Code inventory only: 2026-07-24. Live page evidence: Pending verification.

### Public entry point
`scraper.scrape_afl_injuries.scrape_injury_list(db_conn)`.

### URL construction helper
The active code currently uses the literal `https://www.afl.com.au/matches/injury-list`; `utils.afl_urls.get_injuries_url()` exists but is not used by the active scraper.

### Current fetch method
Direct Playwright usage through `sync_playwright()`, `page.goto()`, `page.wait_for_selector()`, and `page.content()`.

### Parser entry point
`_scrape_injury_list(db_conn)` performs fetch and parse together; `extract_and_match_club(img_src, alt_text="")` resolves clubs from image/comment metadata.

### Selectors or structured data access
`INJURY_SELECTORS`: article body, team blocks, and promo image class. The parser also reads adjacent `div.table`/`table` markup and HTML comments containing image markup.

### Required output fields
Top-level result includes `source`, `scraped_at`, and `teams`. Each team includes `club`, `updated`, `player_count`, and `players`. Each player includes `name`, `injury`, `return`, and `afl_id`.

### Optional output fields
`afl_id` may be `None` when fuzzy matching cannot resolve a player. `updated` can be empty. Missing club/table states are skipped.

### Database or downstream destination
`db.import_to_db.save_injuries_to_db(data, conn)` is used by CLI/import flow; the entry point itself returns the injury payload and records scrape-run audit rows.

### Existing fixture and test coverage
`tests/test_scraper_audit_entrypoints.py` covers the audited entry point. No static HTML injury fixture was found in this phase.

## Source contract: club-squads

### Verification status
Pending verification.

### Last verified
Code inventory only: 2026-07-24. Live page evidence: Pending verification.

### Public entry point
`scraper.scrape_afl_clubs.scrape_club_players(club)` and `save_club_players_to_json(club, skip_existing=False)`.

### URL construction helper
The URL comes from the supplied club dictionary as `club["squad_url"]`; no central helper was found for this active source.

### Current fetch method
Direct Playwright usage through `sync_playwright()`, Chromium launch, `page.goto(..., wait_until="networkidle")`, selector wait, scrolling, and query selectors.

### Parser entry point
`scrape_club_players(club)` fetches and parses cards in one function.

### Selectors or structured data access
`CLUB_SQUAD_SELECTORS`: squad card, player link, first name, last name, position, jumper number, primary image, and fallback image. The parser also extracts club/player IDs from URLs and Champion Data IDs from card HTML.

### Required output fields
Returned player dicts include `full_name`, `short_name`, `first_name`, `last_name`, `nickname`, `formatted_nickname`, `formatted_last_name`, `club`, `guernsey`, `position`, `club_profile_url`, `image_url`, `champion_data_id`, `club_id`, and `scraped_at`.

### Optional output fields
`guernsey`, `club_profile_url`, `image_url`, `champion_data_id`, and `club_id` may be missing or `None`; the code logs validation warnings for missing image, Champion Data ID, or club ID.

### Database or downstream destination
`save_club_players_to_json()` writes `data/players-{slug}-raw.json`. The broader CLI enrichment/import workflow later imports player JSON into the database.

### Existing fixture and test coverage
No focused static fixture or parser test was found for this source in this phase.

## Source contract: stats-leaders-players

### Verification status
Pending verification.

### Last verified
Code inventory only: 2026-07-24. Live page evidence: Pending verification.

### Public entry point
`scraper.scrape_afl_players.scrape_afl_stats_leaderboard()`.

### URL construction helper
The active code currently uses the literal `https://www.afl.com.au/stats/leaders`; `utils.afl_urls.get_stats_url()` and `get_players_url()` exist but are not used by this scraper.

### Current fetch method
Direct Playwright usage through `sync_playwright()`, `page.goto()`, scrolling, image forcing, load-more clicks, and row query selectors.

### Parser entry point
`parse_row(row)` parses one Playwright row handle after `load_all_stats_rows(page)` prepares the rendered table.

### Selectors or structured data access
`STATS_LEADERS_SELECTORS`: scroll container, player images, final body row, load-more button, body rows, player name link, player headshot, and stat buttons. The parser reads profile URLs and extracts Champion Data IDs from row HTML.

### Required output fields
Each player emitted to JSON includes `full_name`, `afl_id`, `afl_url`, and `champion_data_id`.

### Optional output fields
`champion_data_id` can be `None` if extraction fails. Image URL is used internally but not emitted by this parser.

### Database or downstream destination
Writes `data/afl_stats_leaderboard.json`.

### Existing fixture and test coverage
No focused static fixture or parser test was found for this source in this phase.

## Source contract: match-player-stats

### Verification status
Pending verification.

### Last verified
Code inventory only: 2026-07-24. Live page evidence: Pending verification.

### Public entry point
`scraper.scrape_afl_player_stats.run_scraper(match_id, once=False)`.

### URL construction helper
The active code builds `https://www.afl.com.au/afl/matches/{match_id}#player-stats` inline after resolving `round_id` from the `matches` table.

### Current fetch method
`retry_load_page(url)` calls `utils.http_utils.load_page_with_playwright(url)` up to three times, then the runner optionally repeats until match status is completed.

### Parser entry point
`get_match_status_from_header(html)` and `parse_live_stats(html, match_id, round_id, status)`.

### Selectors or structured data access
`PLAYER_STATS_SELECTORS`: match status label, stats table, header cells, body rows, player profile link, player headshot, and jumper number. Structured access includes stat-header-to-database-field mapping and IDs extracted from profile/headshot URLs.

### Required output fields
Base player-stat rows include `match_id`, `round_id`, `afl_id`, `champion_id`, `player_name`, `team_code`, `jumper_number`, and `status`. Recognised stat columns map to `af_score`, `goals`, `behinds`, `disposals`, `kicks`, `handballs`, `marks`, `tackles`, `hitouts`, `clearances`, `metres_gained`, `goal_assists`, and `time_on_ground_pct` when present.

### Optional output fields
Mapped statistic fields are optional because they depend on table headers and parseability. `jumper_number` can be `None`; unresolved team aliases fall back to the raw code.

### Database or downstream destination
`db.import_to_db.save_player_stats_to_db(stats, conn)` writes player statistics; `db.import_to_db.log_scrape_event(conn, match_id, round_id, status)` records scrape events in continuous mode.

### Existing fixture and test coverage
`tests/test_scraper_audit_entrypoints.py` covers the audited entry point. No static HTML player-stats fixture was found in this phase.

## Manual inspection tool

Use `python -m tools.inspect_scraper_source URL --selector CSS --selector CSS` during later live investigation. The tool can compare plain HTTP and Playwright-rendered responses, report selector counts, embedded JSON or hydration candidates, and observed Playwright browser request candidates. It redacts sensitive headers and prints a note that it does not write pages, cookies, credentials, or raw captures to disk.

## How to investigate a source


### Expected Python environment and dependencies

The inspection tool is a repository-maintenance helper, not scraper runtime behaviour. It expects the same Python dependency set as the application:

- `requirements.txt` includes `playwright==1.61.0`, `beautifulsoup4`, and `requests`; Playwright is therefore already a runtime dependency rather than a new tool-only dependency.
- `requirements-dev.txt` includes `pytest` for the focused helper tests.
- The project Dockerfile uses `mcr.microsoft.com/playwright/python:v1.61.0-noble`, so Docker-based maintainers should already have the matching Playwright browser stack available.
- Local non-Docker environments may have the Python `playwright` package installed without Chromium browser assets. If the rendered mode reports that the Chromium executable is missing, run `python -m playwright install chromium` and retry.
- Fresh non-Docker Linux environments may also lack host browser libraries. The likely host-level dependency command is `sudo python -m playwright install-deps chromium`; on a new Linux environment this can be done in one step with `sudo python -m playwright install --with-deps chromium`. Do not assume `sudo` is available in every environment; when host-level dependency installation is unavailable, use the project Docker image.

The tool includes an `environment` block in its JSON output that repeats the Playwright package status, the pinned requirement, the Chromium install command, the Linux dependency command, the fresh-Linux one-step command, and the Docker browser-stack hint.

### Fixtures and match-card demonstration command

Run the fixture/match preset against the 2026 Opening Round fixture page:

```bash
python -m tools.inspect_scraper_source 'https://www.afl.com.au/fixture?Competition=1&Season=85&Round=1343' --preset fixture-match
```

The single `fixture-match` preset checks every selector currently used by `scraper/scrape_afl_fixtures.py` and `scraper/scrape_afl_matches.py`, then compares the unrendered HTTP response with the Playwright-rendered page. It does not save the downloaded HTML, cookies, browser profiles, credentials, or raw network captures.

### Example console output

The default output starts with a concise terminal summary, followed by JSON so maintainers can paste it into notes or compare runs. This is an abbreviated example showing the fields to review; counts and candidate URLs must be regenerated during live verification rather than copied into the contract as permanent facts. Use `--json-only` for machine-readable output without the summary, or `--verbose` to include underlying exception diagnostics and unfiltered candidate URL/request lists.

```text
INSPECTION INCOMPLETE
Plain HTTP: SUCCESS
Playwright: FAILED — Chromium system dependencies are missing
Suggested action: Likely host-level command: `sudo python -m playwright install-deps chromium`. For a fresh Linux environment, use `sudo python -m playwright install --with-deps chromium`. Use the project Docker image if host-level dependency installation is unavailable.
Rendered-page and acquisition-method conclusions cannot be drawn until all required modes succeed.

Findings:
Embedded JSON found? No
Hydration data found? No
Structured API endpoints observed? No
Current scraper contract satisfied? Unknown
Does the rendered page expose additional required fields? Unknown
Does this page still appear to require Playwright? Unknown
Recommendation: Inconclusive
Recommendation status: Pending verification

{
  "note": "No pages, cookies, credentials, or raw network captures were written to disk.",
  "preset": "fixture-match",
  "preset_description": "Selectors used by scraper.scrape_afl_fixtures and scraper.scrape_afl_matches.",
  "findings": {
    "embedded_json_found": "No",
    "hydration_data_found": "No",
    "structured_api_endpoints_observed": "No",
    "current_scraper_contract_satisfied": "Unknown",
    "rendered_page_exposes_additional_required_fields": "Unknown",
    "page_still_appears_to_require_playwright": "Unknown",
    "recommendation": "Inconclusive",
    "recommendation_status": "Pending verification"
  },
  "documentation_mapping": {
    "fixtures-rounds": "docs/scraper_source_inventory.md#source-contract-fixtures-rounds",
    "matches-status": "docs/scraper_source_inventory.md#source-contract-matches-status"
  },
  "comparison": {
    "selectors": {
      "raw_http_only": [],
      "rendered_html_only": ["fixtures.metadata_root", "matches.match_card"],
      "both": [],
      "neither": [],
      "unknown": ["fixtures.metadata_root", "matches.match_card", "matches.live_clock"],
      "not_compared_reason": "Both plain HTTP and Playwright-rendered inspections must succeed before acquisition-method conclusions can be drawn."
    },
    "fields": {
      "raw_http_only": [],
      "rendered_html_only": ["fixtures.season_id", "matches.match_id", "matches.home_team"],
      "both": [],
      "neither": [],
      "unknown": ["fixtures.season_id", "matches.match_id", "matches.home_team", "matches.score_home_away_candidate"],
      "not_compared_reason": "Both plain HTTP and Playwright-rendered inspections must succeed before acquisition-method conclusions can be drawn."
    }
  },
  "results": [
    {
      "mode": "plain-http",
      "status_code": 200,
      "selector_presence": {"fixtures.metadata_root": 0, "matches.match_card": 0},
      "field_presence": {"fixtures.season_id": false, "matches.match_id": false},
      "embedded_json_candidates": ["__NEXT_DATA__"],
      "html_url_candidates": ["/api/example-candidate"],
      "observed_network_requests": []
    },
    {
      "mode": "playwright-rendered",
      "status_code": null,
      "selector_presence": {"fixtures.metadata_root": 0, "matches.match_card": 0},
      "field_presence": {"fixtures.season_id": false, "matches.match_id": false},
      "embedded_json_candidates": [],
      "html_url_candidates": [],
      "observed_network_requests": [],
      "error": {
        "code": "playwright_system_dependency_missing",
        "summary": "Chromium system dependencies are missing",
        "remediation": "Likely host-level command: `sudo python -m playwright install-deps chromium`. For a fresh Linux environment, use `sudo python -m playwright install --with-deps chromium`. Use the project Docker image if host-level dependency installation is unavailable.",
        "missing_library": "libnspr4.so",
        "diagnostic": null
      }
    }
  ],
  "human_judgement_required": [
    "Decide whether candidate embedded JSON or network requests are stable, documented, and appropriate to treat as dependencies.",
    "Review page states not represented by this URL, including live, completed, postponed, bye, hidden or not-yet-announced states.",
    "Confirm whether missing optional fields are expected for this page state or indicate selector drift."
  ]
}
```

When either mode fails, `comparison.selectors.*` and `comparison.fields.*` use `unknown` instead of false `raw_http_only`, `rendered_html_only`, or `neither` conclusions. No acquisition-method conclusion can be made until both modes succeed.

If the maintainer's environment cannot reach `www.afl.com.au` or does not have Playwright browsers installed, the tool still exits successfully and records a structured `error` object for the affected mode with a stable code, concise summary, detected missing executable or library when available, and remediation command. That diagnostic output demonstrates tooling safety but is not live source evidence for changing a contract.



### Human-readable findings summary

The inspection report now includes a `findings` object and prints a matching human-readable `Findings:` block before the JSON output. This block is derived from the detailed selector, field, embedded-data, and observed-network results so maintainers do not have to infer the high-level answers manually. It explicitly reports:

- `Embedded JSON found?`
- `Hydration data found?`
- `Structured API endpoints observed?`
- `Current scraper contract satisfied?`
- `Does the rendered page expose additional required fields?`
- `Does this page still appear to require Playwright?`
- `Recommendation: Continue current scraper / Investigate structured API / Inconclusive`

When either required acquisition mode fails, contract and Playwright-requirement findings are reported as `Unknown`, and the recommendation is `Inconclusive`. Recommendations remain marked `Pending verification`; they are workflow triage hints only, not production scraper architecture decisions.


### Player-stats inspection preset

Use the `player-stats` preset to inspect the contract used by `scraper/scrape_afl_player_stats.py` without changing scraper behaviour:

```bash
python -m tools.inspect_scraper_source \
  'https://www.afl.com.au/afl/matches/8210#player-stats' \
  --preset player-stats \
  --output /tmp/match-8210-player-stats.json
```

The preset uses `PLAYER_STATS_SELECTORS` from `scraper.afl_selectors` and supports both match-centre URLs with and without the `#player-stats` fragment. Its human-readable summary reports plain HTTP success, Playwright success, stats-table/status-label presence, interpreted match state (`pre-match`, `live`, `completed`, or `unknown`), detected table headers, row count, player identity field presence, required stat-column coverage, missing required columns, unexpected additional columns, player-stats contract status, whether Playwright appears required, and whether structured player-stat API responses were observed.

The required recognised stat headers mirror the current parser mapping: `AF`, `G`, `B`, `D`, `K`, `H`, `M`, `T`, `HO`, `CLR`, `MG`, `GA`, and `ToG%`. Structured API responses observed during Playwright execution are reported with the same safe metadata, redaction, JSON-shape summary, and credential-free direct-fetch checks used for fixture API endpoint inspection. All recommendations remain **Pending verification** until returned fields are compared with the current player-stat database requirements.

### Playwright data-source response metadata

When Playwright rendering succeeds, the inspection helper records structured metadata for likely data-source responses, prioritising `https://aflapi.afl.com.au/afl/v2/` and suppressing analytics, advertising, images, fonts, scripts, store links, and unrelated static content by default. For each likely data-source response the report includes:

- request URL, HTTP method, and Playwright resource type;
- response status and response `Content-Type`;
- boolean flags indicating whether request cookies, `Authorization`, or API-key-style headers were present, without exposing values;
- response byte size;
- a safe JSON shape summary for JSON responses: object/array kind, top-level object keys, array item count, and representative item keys;
- a direct-fetch result using the shared scraper HTTP client without copying browser cookies, credentials, or tokens;
- an `endpoint_access` classification of `public_directly_callable`, `browser_context_dependent`, `authenticated`, or `inconclusive`.

`html_url_candidates` are URLs discovered in the HTML body. `observed_network_requests` and `data_source_responses` are reserved for Playwright-observed browser traffic. Full response bodies, cookies, credentials, tokens, and browser profiles are not saved.

### Likely fixture data endpoints from successful local inspection

A successful maintainer run observed high-value AFL API requests under `https://aflapi.afl.com.au/afl/v2/`, including competitions, compseasons, rounds, matches, teams, and venues. The inspection tool now surfaces these as `likely_fixture_data_endpoints` when they are observed in Playwright network traffic. Until each endpoint's returned fields are compared against the current fixture and match database requirements, architectural recommendations remain **Pending verification**.

| Endpoint family | Expected report evidence | Access classification | Contract status |
|---|---|---|---|
| competitions | `likely_fixture_data_endpoints[].url` path includes competitions | Reported by tool as public/browser/authenticated/inconclusive | Pending verification |
| compseasons | `likely_fixture_data_endpoints[].url` path includes compseasons | Reported by tool as public/browser/authenticated/inconclusive | Pending verification |
| rounds | `likely_fixture_data_endpoints[].url` path includes rounds | Reported by tool as public/browser/authenticated/inconclusive | Pending verification |
| matches | `likely_fixture_data_endpoints[].url` path includes matches | Reported by tool as public/browser/authenticated/inconclusive | Pending verification |
| teams | `likely_fixture_data_endpoints[].url` path includes teams | Reported by tool as public/browser/authenticated/inconclusive | Pending verification |
| venues | `likely_fixture_data_endpoints[].url` path includes venues | Reported by tool as public/browser/authenticated/inconclusive | Pending verification |

Interpret `endpoint_access` conservatively: `public_directly_callable` means the tool's credential-free direct fetch returned a 2xx/3xx response; `browser_context_dependent` means the browser observed a successful response but credential-free direct fetch did not; `authenticated` means cookies, `Authorization`, or API-key-style request headers were present; `inconclusive` means the run did not provide enough evidence. Do not convert any of these endpoint families into production scraper dependencies in this phase.

### Mapping fixture/match output to this inventory

- `documentation_mapping.fixtures-rounds` and `documentation_mapping.matches-status` identify which contract sections should be reviewed.
- `comparison.selectors.raw_http_only`, `rendered_html_only`, `both`, and `neither` map to each section's **Selectors or structured data access** heading. These buckets report selector presence only; they do not prove a selector is semantically correct.
- `comparison.fields.*` maps to **Required output fields** and **Optional output fields**. For fixture metadata this checks the documented `data-*` attributes. For match cards this checks IDs/status/team/venue text plus candidate time/score labels.
- `results[].embedded_json_candidates`, `results[].html_url_candidates`, and `results[].observed_network_requests` map to **Verification status** because any decision to use a JSON, REST, GraphQL, hydration source, or observed browser request is Pending verification and requires human review. Plain-response URL discoveries are HTML URL candidates; observed network requests are reserved for Playwright browser traffic.
- `results[].headers` demonstrates redaction behavior for sensitive response headers. Request cookies and credentials are not persisted.
- `results[].error`, if present, means that mode did not produce page evidence and the contract should remain Pending verification. Default errors are concise; rerun with `--verbose` to include underlying exception diagnostics and unfiltered URL/request candidates.

### Observations that still require human judgement

- Whether any embedded JSON, HTML URL candidate, observed REST/GraphQL/browser request, or hydration candidate is stable, documented enough, and acceptable as a future dependency.
- Whether a rendered-only selector means Playwright is required or simply that a plain HTTP request needs different headers, timing, or parsing.
- Whether selectors that appear in the page also contain the expected semantic data for all fixture states.
- Whether optional fields are absent because of page state, fixture status, or selector drift.
- Whether this Opening Round page represents other states such as live matches, completed matches, postponed matches, byes, hidden rounds, or not-yet-announced lineups.
- Whether candidate browser requests contain parameters or identifiers that must be documented before any future scraper change.
