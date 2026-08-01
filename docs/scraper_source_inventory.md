# AFL scraper source inventory and page contracts

**Repository revision investigated:** `6c738e6`<br>
**Last verified:** 2026-08-01<br>
**Scope:** documentation and investigation only; no runtime scraper, selector,
model, schema, scheduler, Admin action, or acquisition-routing change is made here.

This inventory describes what the repository actually invokes. “Active” below is
not inferred merely from a filename: production-scheduled, Admin-triggered, CLI,
and deliberately manual paths are distinguished from historical code.

## 1. Purpose and terminology

This document is the maintained map from an AFL **data domain** (a coherent set
of records such as matches or injuries) to its **page contract** (the minimum
source structure, fields, identifiers, and valid states that its parser assumes).
An **acquisition method** is how bytes are obtained: plain HTTP, public JSON,
authenticated CFS JSON, or browser-rendered HTML.

* A **preferred source** is the single, policy-selected normal production source.
* A **fallback source** is an explicitly configured, observable secondary source
  expected, where practical, to yield the same canonical domain records.
* A **diagnostic source** is collected only for comparison or fault analysis and
  does not silently replace the preferred source.
* A **historical-gap source** is used deliberately when the preferred source does
  not cover a known season or publication interval.
* An **active scraper** has a current production caller, Admin/CLI entry point, or
  intentional manual executable. The table labels the last category; a file
  without any such path is not production-active.
* **Rendered HTML** is DOM captured after JavaScript/browser execution.
  **Embedded JSON** or **hydration data** is structured state delivered inside a
  page for client startup, rather than visible DOM text.
* **Public AFL JSON** means unauthenticated endpoints rooted at
  `https://aflapi.afl.com.au/afl/v2`, maintained in
  `afl_json/contracts.py:ENDPOINTS`.
* **Authenticated CFS JSON** means Champion Data-backed endpoints rooted at
  `https://api.afl.com.au/cfs/afl`, acquired through
  `afl_json.client.AflJsonClient`; its process-local token is secret and must
  never be logged or captured.

“Fallback” never means an uncontrolled second production path. It must be
explicitly selected, failure-classified, observable, contract-compatible, and
proven against fixtures before it is eligible to persist records.

### Evidence convention and live-inspection limitation

`Repository` evidence is a code path or deterministic fixture/test. `Live` means
a response checked on 2026-07-28. The public `competitions` JSON endpoint returned
HTTP 200 JSON containing `meta` and `competitions`, confirming plain HTTP for that
contract. Direct requests to all inspected `www.afl.com.au` pages (fixture,
lineups, injuries, leaders, and a club squad) were blocked by the execution
environment's HTTPS proxy with `403 Forbidden` before an origin response was
received. Consequently, this document does **not** claim that plain HTTP is
sufficient for any HTML page; HTML conclusions use repository code and fixtures.
No browser network trace was available, so REST calls made by pages, embedded
JSON, hydration state, and GraphQL remain investigation items unless represented
by the maintained JSON collectors.

## 2. Active scraper inventory

### Invocation status

| Module | Status and callers |
|---|---|
| `scraper/scrape_afl_fixtures.py` | **Explicit legacy/manual HTML:** direct module. Imported by `scheduler/api.py`, although the API refresh route only re-registers jobs; scheduled metadata collection selects public JSON through `SOURCE_POLICY`. |
| `scraper/scrape_afl_matches.py` | **Explicit legacy/manual HTML:** CLI `--scrape-round`/`--scrape-all-rounds` and direct module. Scheduler match metadata/status and Admin fixture refreshes select public JSON through `SOURCE_POLICY`. |
| `scraper/monitor_match_status.py` | **Manual diagnostic only:** hard-coded match/round constants and direct module execution; no scheduler, Admin, or CLI caller. |
| `scraper/scrape_afl_lineups.py` | **Scheduled production:** `scheduler/schedule_lineup_scrapes.py`; **Admin:** round/match paths; **CLI:** module `--round/--match` and `cli.py --scrape-lineups`. |
| `scraper/scrape_afl_injuries.py` | **Scheduled production:** daily job; **Admin:** injuries path; **CLI/direct module:** `--scrape-injuries`. |
| `scraper/scrape_afl_clubs.py` | **CLI production tool:** `--scrape-club`, `--scrape-clubs`, and enrichment workflow; not scheduled/Admin-triggered. |
| `scraper/scrape_afl_players.py` | **Scheduled production tool:** five-day refresh in `scheduler/schedule_refresh_jobs.py`; direct module. |
| `scraper/scrape_afl_players_with_stats.py` | **Manual export tool:** direct module only; not persistence, scheduler, Admin, or unified CLI. |
| `scraper/scrape_afl_player_stats.py` | **Explicit legacy/manual HTML only:** unified CLI `--scrape-match`; direct module `--match-id`/`--round-id`. Scheduler and Admin player-stat jobs do not call this module; they select CFS JSON through `SOURCE_POLICY`. |
| `scraper/scrape_afl_lineups-early2025.py` | **Historical/unused legacy:** no import or current caller, uses inline selectors, and is excluded from active implementations. Retained only as historical evidence. |

The shared symbolic definitions for every active HTML parser are in
`scraper/afl_selectors.py`: `FIXTURE_SELECTORS`, `MATCH_CARD_SELECTORS`,
`TEAM_LINEUP_SELECTORS`, `INJURY_SELECTORS`, `CLUB_SQUAD_SELECTORS`,
`STATS_LEADERS_SELECTORS`, and `PLAYER_STATS_SELECTORS`.

### Source records

The following compact records contain every required inventory attribute.
“Optional” means parser output may legitimately be null/empty; it does not mean
the downstream database necessarily accepts every shape.

#### Fixtures and rounds

* **Domain/module/entry:** fixture index and round metadata;
  `scraper/scrape_afl_fixtures.py` → `update_fixture_cache` /
  `_update_fixture_cache`.
* **URL:** `utils.afl_urls.get_fixture_url()` →
  `https://www.afl.com.au/fixture` (representative URL is identical).
* **Fetch/parser:** Playwright through
  `utils.http_utils.load_page_with_playwright`; `parse_fixtures_metadata` and
  `parse_round_list` (Beautiful Soup).
* **Selectors/data:** `FIXTURE_SELECTORS.METADATA_ROOT_CLASS`,
  `.ROUND_LIST_ITEMS`, `.ROUND_LABEL_BUTTON`. Stable `data-season-pid`,
  `data-season-id`, `data-competition-id`, `data-no-filter-round`,
  `data-special-round`, and each `data-round-id` are structured DOM attributes;
  no embedded JSON is consumed.
* **Output:** required metadata `season_pid`, numeric `season_id`, numeric
  `competition_id`, numeric `default_round_id`; required round `round_id` and
  `round_label`. `special_round` is optional.
* **Identifiers/states:** AFL numeric season, competition, default-round and
  round IDs plus season provider ID. Ordinary rounds, byes, Opening Round and
  other special labels are valid. An empty round list or missing metadata root
  is treated as missing data and prevents persistence, not as a proven valid
  empty season.
* **Requirements/coverage:** browser currently required by implementation; no
  auth. Fixtures `tests/fixtures/afl/fixture_index_rounds.html` and parser tests
  in `tests/test_fixture_match_parsers.py`; selector wiring and DB path tests in
  `tests/test_afl_selectors.py` and `tests/test_scraper_database_paths.py`.
* **Fragility/risk/verified:** metadata attribute renames, navigation markup,
  special-round semantics and season turnover. Repository verified 2026-07-28;
  live HTML blocked.

#### Match details and match status

* **Domain/module/entry:** round match cards;
  `scraper/scrape_afl_matches.py:run`, `_run`, `scrape_round`, `parse_matches`,
  `extract_match_data`; diagnostic status polling uses
  `scraper/monitor_match_status.py:monitor` and `extract_status_for_match`.
* **URL pattern/example:** `utils.afl_urls.get_fixture_url_for_round` →
  `https://www.afl.com.au/fixture?Competition={competition_id}&Season={season_id}&Round={round_id}`;
  example with repository defaults:
  `https://www.afl.com.au/fixture?Competition=1&Season=73&Round=1155`.
* **Fetch/parser:** Playwright helper, then Beautiful Soup. The monitor uses the
  same path and contract.
* **Selectors/data:** `MATCH_CARD_SELECTORS.SEASON_LABEL`,
  `.DATE_HEADER_OR_MATCH_CARD`, `.MATCH_CARD_CLASS`, `.HOME_TEAM_NAME`,
  `.AWAY_TEAM_NAME`, `.VENUE`, `.DETAILS_LINK`, `.MATCH_TIME`, `.STATUS_LABEL`,
  `.SCORE_TOTAL`, and monitor-only `.DATE_HEADER_CLASS`/`.LIVE_CLOCK`.
  Required card attributes are `data-match-id`, `data-round-id`, and team/venue
  DOM; `data-match-provider-id` and `data-match-status` are consumed structured
  attributes. No embedded JSON is consumed.
* **Output:** required `match_id`, `round_id`, home/away resolvable team names,
  and venue. Currently emitted but source-nullable: `match_provider_id`,
  `status`, `start_time_utc`, `score_home`, `score_away`, `match_time_label`.
  The `aria-label` supplies scheduled date/time enrichment. Existing stored
  start time is retained if a live rendering omits it.
* **States:** upcoming cards may have no scores; live cards have status/quarter
  and optional clock/partial scores; completed cards have status-label and
  normally two totals. Postponed/cancelled values are preserved verbatim but are
  not explicitly normalised. Bye/special rounds may have no cards; presently
  `parse_matches` reports this as failure, so valid-empty versus breakage is an
  unresolved contract gap.
* **Requirements/coverage:** browser currently used; no auth. Opening Round and
  completed fixture `tests/fixtures/afl/matches_opening_round_completed.html`,
  parser cases in `tests/test_fixture_match_parsers.py`; selector tests. The
  monitor has no direct test or scheduler caller.
* **Fragility/risk/verified:** English `aria-label` regex, CSS classes, team alias
  resolution, provider-ID absence, live clock, status vocabulary, and season
  label. Repository verified 2026-07-28; live HTML blocked.

#### Lineups / team selections

* **Domain/module/entry:** `scraper/scrape_afl_lineups.py` →
  `scrape_team_lineups`, `scrape_match_lineup`, `_scrape_team_lineups`, and
  `parse_lineups_html`.
* **URL/example:** fixed
  `https://www.afl.com.au/matches/team-lineups`; round selection is a browser
  click on `data-round-id`, not a URL parameter.
* **Fetch/parser:** direct Playwright, including round-button and expand-all
  clicks, then Beautiful Soup.
* **Selectors/data:** all `TEAM_LINEUP_SELECTORS` symbols: match/header links and
  name, home/away ins-and-outs grids, player-name, player-entry/name/class,
  round-list, templated round button, expand toggle, and ready selectors. Match
  and AFL player IDs are parsed from `/matches/{id}` and `/players/{id}` links;
  no embedded JSON is consumed.
* **Output:** required per usable row `round_number`, `match_id`, team,
  `position_group`, `scraped_at`; names are required for ins/outs and may be null
  for malformed on-field entries. Optional `afl_id`; `champion_id` is always
  null. Team headings must contain `" v "`.
* **States:** unpublished/collapsed lineups and a valid round with no published
  players can yield `[]`; late changes appear as subsequent page state.
  `IN`/`OUT`/`SUB` are emitted from each grid, while all selected player entries
  are currently labelled `ONFIELD`; position semantics are therefore partial.
* **Requirements/coverage:** browser is specifically required by current code to
  select rounds and expand content; no auth. CLI and scheduler behavior is tested
  in `tests/test_lineup_cli.py`; audit/Admin routing is tested, but there is no
  golden lineup HTML fixture/parser test.
* **Fragility/risk/verified:** interactive toggle/round IDs, collapsed markup,
  header delimiter, late changes, special rounds, and duplicated traversal of
  ins/outs grids. Repository verified 2026-07-28; live HTML blocked.

#### Injuries

* **Domain/module/entry:** `scraper/scrape_afl_injuries.py` →
  `scrape_injury_list`, `_scrape_injury_list`, `extract_and_match_club`.
* **URL/example:** fixed
  `https://www.afl.com.au/matches/injury-list`.
* **Fetch/parser:** direct Playwright; Beautiful Soup over rendered content.
* **Selectors/data:** `INJURY_SELECTORS.ARTICLE_BODY`, `.TEAM_BLOCKS`, and
  `.PROMO_IMAGE_CLASS`; additionally, the parser contract uses a commented promo
  image, its `src`/`alt`, the following sibling `div.table`, `table`, header row,
  and rows of at least three `td` cells. No JSON/hydration is consumed.
* **Output:** team `club`, `updated`, `player_count`, and `players`; player
  required fields are `name`, `injury`, and `return`; matched `afl_id` is optional.
  Source URL and UTC `scraped_at` are enrichment.
* **States:** an empty club table is a valid empty injury list; an “Updated:”
  one-cell row supplies optional update text. Missing/unmatched club image,
  missing sibling/table, short row, or unmatched player produces partial output,
  not a whole-page exception.
* **Requirements/coverage:** Playwright is the safest current method because the
  parser waits for rendered `ARTICLE_BODY` and no maintained structured injury
  endpoint exists. No auth. Audit/Admin wiring tests exist, but no injury HTML
  fixture or parser contract test.
* **Fragility/risk/verified:** club identity hidden in an HTML comment/image,
  sibling adjacency, unvalidated headings/order (`name`, injury description,
  return estimate), Indigenous Round names/logos, and editorial redesign.
  Repository verified 2026-07-28; live HTML blocked.

#### Clubs / club squads and players

* **Domain/module/entry:** per-club current squad/player identity;
  `scraper/scrape_afl_clubs.py:scrape_club_players` and
  `save_club_players_to_json`, invoked by `cli.py` club workflows.
* **URL pattern/example:** database/configured `club["squad_url"]`, expected AFL
  club squad pages such as `https://www.afl.com.au/club/richmond/players`.
* **Fetch/parser:** direct Playwright, `networkidle`, scroll/lazy-load, then
  Playwright element methods and `merge.helpers.extract_champion_data_id_from_html`.
* **Selectors/data:** every `CLUB_SQUAD_SELECTORS` symbol: squad card, player link,
  first/last names, position, jumper and primary/fallback images. AFL player ID
  is parsed from `/players/{id}/`; Champion Data ID/image are derived from card
  image HTML. No embedded JSON is consumed.
* **Output:** identity contract requires a usable card/name; emitted fields are
  `full_name`, `short_name`, `first_name`, `last_name`, derived nickname/casing,
  club, and `scraped_at`. Optional `guernsey`, `position`, profile URL, image URL,
  `champion_data_id`, and numeric `club_id` (despite its name, this is parsed from
  the player URL and functions as an AFL player ID).
* **States:** offseason squads, delistings, missing jumper/position/image/ID and
  lazy images are partial valid states; no squad card currently returns `[]`.
* **Requirements/coverage:** browser currently required for network-idle,
  scrolling and lazy media; no auth. Central-selector static tests only; no squad
  HTML fixture or parser test.
* **Fragility/risk/verified:** configured URLs, lazy image markup, name nesting,
  URL regex/trailing slash, ambiguous `club_id`, list turnover. Repository
  verified 2026-07-28; live HTML blocked.

#### Player leaderboard / identity mapping

* **Domain/module/entry:** player IDs and names;
  `scraper/scrape_afl_players.py:scrape_afl_stats_leaderboard`, `parse_row`, and
  load/scroll helpers. Scheduled every five days and writes
  `data/afl_stats_leaderboard.json`.
* **URL/example:** fixed `https://www.afl.com.au/stats/leaders`.
* **Fetch/parser:** Playwright, repeated show-more clicks and scrolling, then
  Playwright element parsing plus `extract_champion_data_id_from_html`.
* **Selectors/data:** every `STATS_LEADERS_SELECTORS` symbol except
  `.STAT_BUTTONS`; player links supply AFL numeric IDs and image markup supplies
  Champion Data IDs. No embedded JSON is consumed.
* **Output:** required `full_name`, `afl_id`, `afl_url`; optional
  `champion_data_id`. A missing link or malformed ID drops the row.
* **States:** incremental pagination/lazy images are expected; a hidden/missing
  show-more means complete-or-partial cannot currently be distinguished.
* **Requirements/coverage:** browser required by current interaction; no auth.
  Only central-selector static tests; no leaderboard fixture/parser test.
* **Fragility/risk/verified:** button behavior, virtual scrolling, image ID
  convention, list completeness, and season rollover. Repository verified
  2026-07-28; live HTML blocked.

#### Player bootstrap identity resolution

Club squad cards and leaderboard rows both expose the Champion Data identifier
in `ChampIDImages/.../{champion_data_id}.png`. Club profile links additionally
contain the numeric AFL player ID in `/players/{afl_id}/`, so the squad scraper
now persists that direct identity as well as using the leaderboard mapping.
Champion Data identifiers are normalized to strings at the join boundary.

The previous on-demand refresh invoked the nonexistent
`scraper.scrape_afl_stats` module. A missing or stale leaderboard was therefore
left missing/stale after the failed subprocess, and enrichment silently produced
players with null `afl_id`. Refresh now invokes `scraper.scrape_afl_players` with
the current Python interpreter and rejects missing, invalid, or empty output.
Under Compose, the repository is mounted at `/app` and the shared data volume at
`/app/data`, matching the relative paths used by the CLI and refresh subprocess.
An unmatched Champion Data ID is never guessed; it remains unresolved and is
skipped explicitly by the importer.

#### Leaderboard player statistics (manual export)

* **Domain/module/entry:** season total/average export;
  `scraper/scrape_afl_players_with_stats.py:scrape_afl_stats`, `scrape_table`.
* **URL pattern/examples:**
  `https://www.afl.com.au/stats/leaders?dataType={totals|averages}`.
* **Fetch/parser/selectors:** Playwright and show-more interactions;
  `STATS_LEADERS_SELECTORS.LOAD_MORE_BUTTON`, `.BODY_ROWS`, `.PLAYER_NAME_LINK`,
  `.STAT_BUTTONS`. Link supplies AFL ID; no embedded JSON is consumed.
* **Output:** player name/ID with totals and averages for `Goals`, `Disposals`,
  `Hitouts`, `Marks`, and `Tackles`, written to
  `data/bbbffl_player_stats.csv`; absent/malformed values are partial.
* **States/requirements/coverage:** manual-only, browser currently required, no
  auth, no dedicated tests or fixtures. Header/order changes, show-more limits,
  season filters and stat-button order are fragile. Repository verified
  2026-07-28; live HTML blocked.

#### Match player statistics

* **Domain/module/entry:** live/completed match statistics;
  `scraper/scrape_afl_player_stats.py:run_scraper`, `_run_scraper`,
  `get_match_status_from_header`, and `parse_live_stats`.
* **URL pattern/example:**
  `https://www.afl.com.au/afl/matches/{afl_match_id}#player-stats`, e.g.
  `https://www.afl.com.au/afl/matches/7043#player-stats`.
* **Fetch/parser:** Playwright helper with retries; Beautiful Soup. Continuous
  direct-module mode polls until completed; CLI `--scrape-match` fetches once.
* **Selectors/data:** every `PLAYER_STATS_SELECTORS` symbol. Profile link supplies
  AFL player ID; headshot URL supplies Champion Data ID; jumper CSS class supplies
  team alias. Table headings are dynamically mapped.
* **Output:** required per accepted row `match_id`, `round_id`, `afl_id`, player
  name, team code and status. `champion_id`, jumper, and these mapped stats are
  optional/partial: `AF→af_score`, `G→goals`, `B→behinds`, `D→disposals`,
  `K→kicks`, `H→handballs`, `M→marks`, `T→tackles`, `HO→hitouts`,
  `CLR→clearances`, `MG→metres_gained`, `GA→goal_assists`, and
  `ToG/ToG%→time_on_ground_pct`.
* **States:** upcoming/unpublished statistics currently have no table and return
  `[]`; live tables may be partial; `Q1`–`Q4`/`LIVE` are live and `FULL TIME` is
  completed. Unknown/missing status defaults to `LIVE`, which can cause continued
  polling. Postponed/cancelled are not explicitly handled; malformed rows are
  skipped independently.
* **Requirements/coverage:** Playwright remains required for this explicit legacy
  path because repository policy treats the match-centre table as rendered until
  a fixture proves otherwise; no HTML auth. Parser has no golden HTML fixture;
  CLI, audit and selector wiring have tests. Scheduler/Admin routing tests instead
  verify the operational CFS JSON path.
* **Fragility/risk/verified:** header abbreviations/order, missing player/headshot,
  image ID convention, alias CSS class, live publication and status vocabulary.
  Repository verified 2026-07-28; live HTML blocked.

## 3. Source-contract details

The source records above identify fields; this section states the minimum
pass/fail boundary and separates enrichment.

| Domain | Minimum required contract | Optional enrichment / valid absence | Broken or unresolved boundary |
|---|---|---|---|
| Fixtures/rounds | Metadata root has parseable season/competition/default-round IDs; round `li` has parseable `data-round-id` and button label. | provider season ID and `special_round`; bye/special labels. | Missing root/list prevents save; empty-season semantics unresolved. |
| Matches/status | At least one card with parseable match/round IDs, resolvable home/away names and venue. | provider ID, lifecycle status, schedule, label/clock and two scores. Upcoming scores absent; partial live scores permitted. | No cards currently conflates bye/empty with breakage; postponed/cancelled vocabulary is unnormalised. |
| Lineups | Match link ID, `home v away` heading, and a player entry/grid row with team/status context. | AFL player ID, name components, change lists; missing/unpublished lineups may validly yield none. | Parser cannot prove whether `[]` is unpublished, a bye, wrong round click, or DOM breakage. Late changes are snapshots without provenance. |
| Injuries | Club block resolvable from promo image and following three-column player table (`name`, injury description, return estimate). | matched AFL ID and update date; zero player rows is valid. | It does not validate headings; missing block/table is silently partial. |
| Squad players | Squad card with usable player name/link context. | jumper, position, AFL/player URL, headshot and Champion Data ID. | No-card and partial lazy-load completeness are indistinguishable. |
| Player identities | Leader row with name link matching `/players/{afl_id}`. | Champion Data ID/image. | Missing/malformed rows drop; pagination completeness not proven. |
| Leader statistics | Link identity and ordered stat buttons for the five configured headings. | individual missing numeric values. | Heading-to-button association and completeness have no fixture contract. |
| Match statistics | Stats table with headings; row with profile link/name/AFL ID and team context. | Champion Data ID, jumper and any mapped numeric statistic, including partial live stats. | No table may be correctly unpublished or broken; unknown status defaults live. |

Identity namespaces must not be guessed across systems. AFL numeric IDs come from
DOM links/attributes or public JSON `id`; opaque Champion Data IDs (`CD_C`,
`CD_S`, `CD_R`, `CD_M`, `CD_T`, `CD_O`, `CD_V`, `CD_I`) come from provider
fields or the public player ID map. `afl_json/contracts.py:IDENTIFIER_TYPES`
defines these namespaces. Missing identifiers are a documented partial state,
not permission to synthesize one from the other.

## 4. Alternative-source investigation

### Maintained structured collectors

These are implemented, tested alternatives—not endpoints inferred from a single
browser observation:

| Collector | Contract and fields | Activity |
|---|---|---|
| `afl_json.collectors.PublicAflCollector` | Public `competitions`, `competition_seasons`, `rounds`, `teams`, `matches`, and direct `match_detail`; normalises numeric and provider IDs, names/codes, round number, match teams/venue/start/status/scores while retaining `source`. | `cli.py --collect-afl-metadata` is manual read-only; `--bootstrap-afl-season` persists the hierarchy. Scheduler/Admin metadata operations also use public JSON through `SOURCE_POLICY`. |
| `PublicAflCollector.season_players` / `.collect_players` / `.player_id_map` | Authenticated CFS `/players?seasonId=...` supplies season listings and provider identity; public `/players/idmap` supplies validated Champion Data-to-AFL numeric crosswalk. | Implemented and fixture-tested. CLI `--bootstrap-afl-season` persists canonical players, provider mappings, team links where resolvable, and competition-season membership; there is no scheduler/Admin player-bootstrap route. |
| `afl_json.rosters.MatchRosterCollector` | Authenticated CFS `/matchRosters/round/{round_provider_id}`; selection/roster state, provider timestamps/version and Champion Data IDs. Explicit unpublished/malformed/change states. | `cli.py --collect-match-rosters`; not scheduled/Admin production routing. |
| `afl_json.player_stats.MatchPlayerStatsCollector` | Authenticated CFS `/playerStats/match/{match_provider_id}`; canonical mapping preserves extra stats and source/status provenance, with concluded/live-partial/unpublished/malformed fixtures. | Selected for Scheduler and Admin operational player-stat jobs and CLI `--collect-match-player-stats`; all three persist through `upsert_player_stats` to `cfs_player_stats`. |
| `afl_json.match_status.reconcile_match_status` | Public direct match detail reconciles `SCHEDULED`, `LIVE`, `POSTGAME`, `CONCLUDED` monotonically against stored status. | Used by operational Scheduler/Admin and CLI CFS player-stat collection, and by match-status policy operations. |

Endpoint URL templates, methods, required parameters, authentication, collection
paths, pagination, and known unverified fields live in
`afl_json/contracts.py:ENDPOINTS`; `SOURCE_PRIORITY` is existing metadata, not an
implemented runtime router. CFS authentication is established by a plain HTTP
token request and header refresh policy in `afl_json/client.py`; it does not need
browser execution, but it does require transient secret material that must never
be captured.

### Findings per HTML domain

* **Fixtures, rounds, teams, match metadata and status:** public JSON is the best
  maintained structured source and is covered by repository JSON fixtures and
  collector/bootstrapping tests. Plain HTTP sufficiency is supported for the
  public API by the live HTTP 200 check above, not by an HTML-page claim.
  Scheduler/Admin metadata and match-status operations now select public JSON;
  fixture/match HTML remains an explicit legacy/manual diagnostic path, not an
  automatic fallback.
* **Lineups:** CFS match rosters are the best maintained structured alternative
  and specifically model unavailable, changed and completed states. Positions,
  `teamPlayers` relationships, and late-change timing remain marked unverified in
  `ENDPOINTS`; HTML and CFS outputs also use different identifiers/position
  semantics. Therefore migration/fallback eligibility needs parity work first.
* **Injuries:** **Investigation incomplete.** Checked: maintained endpoint
  contracts, all `afl_json` collectors, repository docs/tests, and attempted
  initial HTML. No public AFL JSON, CFS collector, REST, embedded JSON, hydration
  or GraphQL injury contract is maintained in the repository; external HTML was
  proxy-blocked before inspection. Unknown: whether the live page calls a stable,
  documented structured service or delivers usable initial HTML. Safest current
  method is the existing Playwright HTML path.
* **Club squads/players:** CFS season players plus public player ID map provide a
  stronger maintained structured identity/season-listing source; public teams
  cover club/team metadata. Squad HTML may still provide editorial name, jumper,
  position and image enrichment whose exact parity is unproven. **Investigation
  incomplete** for page REST/embedded/hydration/GraphQL and initial HTML because
  live HTML was blocked; retain the current browser only where enrichment/parity
  requires it.
* **Player leaderboard identities and season totals/averages:** CFS season players
  and the ID map cover identity but not the manual export's proven five-stat
  totals/averages contract. No maintained GraphQL/REST/hydration source exists in
  this repository. **Investigation incomplete**; browser remains safest for that
  manual export, while scheduled identity refresh should be evaluated for CFS.
* **Match player statistics:** authenticated CFS is the maintained,
  fixture-tested operational source for Scheduler and Admin and for CLI
  `--collect-match-player-stats`; these paths persist only to `cfs_player_stats`.
  The HTML table remains a separately invoked legacy/manual source via CLI
  `--scrape-match` or the scraper module, whose confirmed writer targets only
  `player_stats`. Neither path falls back to or dual-writes the other table.

No undocumented endpoint observed once is recommended for production. Stable
DOM `data-*` attributes are documented above; no scraper consumes embedded JSON,
hydration state, or GraphQL today.

## 5. Decision matrix

Classification values are intentionally controlled. “Browser required” refers
to the recommendation; the current method column separately records reality.

| Domain | Current production source | Current acquisition | Best structured alternative | Recommended preferred source | Recommended fallback source | Fallback purpose | Auth required | Browser required | Confidence | Evidence | Follow-up |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Competitions/seasons/rounds | Public JSON | Plain HTTP JSON | Public competitions/seasons/rounds | **Public JSON** | Fixture HTML | Explicit legacy diagnostics only; no automatic fallback | No | No | High | `SOURCE_POLICY`, public collector contracts, JSON fixtures/tests, live public HTTP 200 | Define bye/round semantics |
| Teams/clubs | Club squad HTML plus configured club DB | Playwright HTML | Public teams; CFS season players | **Public JSON preferred** for team metadata | Club HTML | **HTML fallback only** for proven enrichment gaps | CFS only for players | No for canonical team metadata | Medium | Public team collector tests; squad enrichment differs | Define team-vs-club and enrichment contract |
| Match metadata/details | Public JSON | Plain HTTP JSON | Public matches/match detail | **Public JSON** | Fixture HTML | Explicit legacy diagnostics only; no automatic fallback | No | No | High | `SOURCE_POLICY`, normalisers, bootstrap and fixtures/tests | Compare start/status/score and special-round behavior |
| Match status | Public match-detail JSON | Plain HTTP JSON | Public match detail | **Public JSON** | Fixture HTML | Explicit manual diagnostic only; no automatic fallback | No | No | High | `SOURCE_POLICY`, monotonic status reconciler and tests | Unify HTML vocabulary including postponed/cancelled |
| Operational lineups/team selections | Team-lineups HTML | Interactive Playwright HTML | Authenticated CFS match rosters | **Playwright HTML intentionally selected for persistence** | None | Not a fallback; CFS roster collection is a separate read-only CLI path | No for operational HTML; yes for CFS diagnostic | Yes for operational path | Medium | `SOURCE_POLICY`, scheduler/Admin routing and persistence tests | Map IDs, positions and late changes before canonical CFS persistence |
| Injuries | Injury-list article | Playwright HTML | None maintained | **Playwright HTML required** / **Further investigation required** | None identified | Not applicable | No known | Yes, safest current | Medium-low | Only active implementation waits for rendered article; live inspection blocked | Capture safe network evidence and add empty/partial fixtures |
| Season players / player IDs | CFS season players + public ID map during CLI bootstrap | Plain authenticated/public JSON | Same implemented sources | **CFS season players plus public ID map** | Squad/leader HTML | Separate enrichment/historical-gap tools only; no automatic fallback | Yes for season list | No for canonical bootstrap | High for identity; medium enrichment | Persistence adapter, bootstrap and crosswalk tests | Keep enrichment parity distinct from canonical persistence |
| Leaderboard totals/averages export | Stats leaders HTML (manual only) | Interactive Playwright HTML | None with proven field parity | **Playwright HTML required** / **Further investigation required** | None | Manual diagnostic/export only | No known | Yes currently | Low | Repository parser only; no fixture/live response | Investigate maintained stats endpoint; add heading fixture |
| Operational match player statistics | Authenticated CFS player stats | Plain authenticated JSON | Same implemented source | **Authenticated CFS JSON** | None | No automatic fallback; persists `cfs_player_stats` | Yes | No | High | `SOURCE_POLICY`, Scheduler/Admin/CLI routing and persistence tests | Reconcile the parallel models separately |
| Legacy/manual match player statistics | Match-centre HTML | Polling/rendered Playwright HTML | CFS is the operational source, not a fallback for this explicit command | **Explicit legacy/manual HTML only** | None | CLI `--scrape-match` and direct module persist `player_stats` only | No HTML auth | Yes | Medium | Legacy scraper and `save_player_stats_to_db` | Preserve explicit support until separately retired |

The injury conclusion is deliberately conservative: it remains HTML-sourced
because no maintained structured injury endpoint or collector exists in this
repository, the implementation waits for rendered article content, and live
source/network inspection was blocked. That is evidence for retaining the safest
current path—not proof that a structured source can never exist.

## 6. Proposed future acquisition boundary

A follow-up architecture should separate:

1. **Acquisition** — HTTP/browser/authentication and bounded retry only.
2. **Source-specific parsing** — JSON shape or page-contract interpretation.
3. **Canonical normalisation** — one domain record/identifier/nullability model.
4. **Validation** — distinguish valid empty/unpublished/partial states from
   structural breakage and reject identity conflicts.
5. **Persistence** — idempotent writes only after validation.
6. **Provenance and diagnostics** — source, endpoint/page contract version,
   collection time, failure class, fallback eligibility and comparison result.

```text
domain request
    -> source policy
    -> preferred collector
    -> canonical records
    -> validation
    -> persistence
```

Only when policy explicitly configures a compatible fallback:

```text
preferred collector failure
    -> recorded failure classification
    -> eligible fallback collector
    -> canonical records
    -> comparison/provenance diagnostics
    -> persistence
```

Both JSON and HTML implementations can then satisfy the same domain-level
contract without becoming competing production writers. Policy must specify
eligible failure classes (for example, CFS “not published” versus authentication
failure), canonical parity, observability and whether fallback persistence is
allowed. This is an architectural recommendation for follow-up work, **not** an
implementation or routing change in Issue #47.

## 7. Gaps and recommended follow-up issues

No issues are created by this document. Suggested titles/scopes are:

* **Undocumented/manual entry points — “Classify or retire manual AFL scraper
  executables.”** Decide support for `monitor_match_status.py` and
  `scrape_afl_players_with_stats.py`; archive/remove the unused early-2025 lineup
  module only after fixture parity.
* **Duplicated acquisition paths — “Introduce domain source policy without dual
  writes.”** Implement the boundary above and migrate one domain at a time.
* **Output-contract mismatches — “Prove CFS roster parity with HTML lineups.”**
  Resolve AFL/provider IDs, position groups, unpublished/late-change semantics
  and canonical nullability.
* **Output-contract mismatches — “Prove CFS match-stat parity with match-centre
  HTML.”** Compare every canonical field, live partials, conclusion status and
  historical availability before fallback eligibility.
* **Missing fixtures — “Add golden HTML contract fixtures for lineups, injuries,
  squads, leaders and match stats.”** Cover valid empty, unpublished, partial,
  malformed and seasonal/special-round states without a complete corpus.
* **Unclear fallback semantics — “Define observable fallback failure classes.”**
  Specify not-published, auth, transport, validation and contract-break behavior;
  prohibit silent catch-all fallback.
* **Scheduler/Admin source routing:** now aligned through `SOURCE_POLICY`: player
  statistics use CFS JSON, while lineups deliberately use the persistent HTML
  writer. Keep this distinction covered when adding entry points.
* **Provenance gaps — “Persist acquisition provenance and comparison
  diagnostics.”** Record source/contract version and fallback decisions without
  secrets or raw personal data.
* **Stale/fragile selectors — “Validate selectors against seasonal page-state
  fixtures.”** Include no-card byes, Opening Round, postponed/cancelled matches,
  empty injuries and missing lazy media.
* **Endpoint investigation — “Investigate a maintained structured AFL injury
  source.”** Inspect safe browser network traces, initial HTML, embedded/hydration
  JSON, REST and GraphQL; document stability/authorization before recommending.
* **Browser retirement — “Retire redundant browser metadata/player identity
  scraping after parity.”** Public metadata and CFS player identity are strongest
  candidates; preserve HTML only for proven enrichment/historical gaps.

### Validation checklist for future updates

When this inventory changes, enumerate `scraper/*.py`; trace imports from
`cli.py`, `scheduler/`, and `admin.py`; confirm every symbolic selector and parser
exists; compare URLs with `utils/afl_urls.py` and module constants; run collector
and parser fixture tests; and scan the diff for tokens, cookies, authorization
headers, raw captures, and personal data. Live evidence must record date, status,
and inspected shape without saving credentials or response bodies.
