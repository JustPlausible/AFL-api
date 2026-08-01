# AFL JSON payload fixtures and contract regression tests

The maintained, payload-only JSON corpus is stored in `tests/fixtures/afl_json/`. Its
`manifest.json` is the authoritative endpoint inventory: every payload file has an individual
record containing its endpoint family, generalised source URL, capture date, scenario, full or
reduced status, sanitisation, reduction notes and production parser. The manifest deliberately
keeps this inventory independent of Python filenames while the `ENDPOINTS` catalogue provides a
lightweight completeness boundary.

## Endpoint coverage inventory

| Endpoint family | Production module | Fixtures | Covered states | Contract test | Intentionally unsupported |
| --- | --- | --- | --- | --- | --- |
| competitions | `afl_json.collectors` | `afl_json/`, `afl_json/contracts/competitions__*` | normal, empty, two-page continuation, missing optional/required, malformed record | `test_afl_json_fixture_contracts.py` | CFS errors and match lifecycle |
| competition seasons | `afl_json.collectors` | `seasons.json`, `contracts/competition_seasons__*` | normal, empty, pagination envelope, missing optional/required, malformed record | same | CFS errors and match lifecycle |
| rounds | `afl_json.collectors` | `rounds.json`, `contracts/rounds__*` | normal including round zero/byes, empty, pagination envelope, missing optional/required, malformed record | same | CFS errors and match lifecycle |
| teams | `afl_json.collectors` | `teams.json`, `contracts/teams__*` | normal, empty, pagination envelope, missing optional/required, malformed record | same | CFS errors and match lifecycle |
| matches | `afl_json.collectors` | `matches_round_*.json`, `contracts/matches__*` | scheduled/concluded, empty, pagination envelope, missing optional/required, malformed record | same | CFS unpublished state |
| match detail | `afl_json.collectors`, `afl_json.match_status` | `match_detail_concluded.json`, `contracts/match_detail__*` | concluded, empty, missing optional/required, malformed record | same | identifier detail is not paginated |
| player ID map | `afl_json.collectors` | `player_id_map.json`, `contracts/player_id_map__*` | normal, empty, malformed row, renamed required collection | same | no pagination/display optionals/match lifecycle |
| season players | `afl_json.collectors` | `season_players_complete.json`, `contracts/season_players__*` | complete, empty, two-page continuation, missing optional/required with record diagnostic | same | match lifecycle does not apply |
| match rosters | `afl_json.rosters` | `match_rosters_*.json` | published, empty, changed/live, concluded, malformed, unpublished | same | verified response has no pagination envelope |
| match player statistics | `afl_json.player_stats` | `match_player_stats_*.json` | live partial/missing optional, concluded, malformed records, unpublished | same | match-scoped response is not paginated |

The shared `contracts/http_401_authentication_failure.json` and
`contracts/cfs_unpublished_resource.json` payloads exercise the production CFS transport for all
protected families. The WMC token-acquisition endpoint is transport infrastructure, not a data
family: no successful token response is retained because even a sanitised example encourages an
unsafe fixture convention. No fixture may contain `WMCTok`, `x-media-mis-token`, cookies,
authorisation headers or reusable credentials.

## Naming, capture and sanitisation

Names use `<endpoint-family>__<scenario>.json`; pages add deterministic `_page_<number>` suffixes.
Older descriptive captures remain at the directory root and are explicitly represented in the
manifest rather than duplicated. Fixture values use stable example IDs and names only where the
real captured structure does not depend on identity.

To capture or refresh a payload:

1. Identify the stable endpoint family in `afl_json.contracts.ENDPOINTS` and save **only the JSON
   response body**. Never save a command transcript, HAR, cookies or request/response headers.
2. Replace unnecessary player/match identifiers and remove personal or analytics fields. Preserve
   container nesting, pagination keys, required/optional distinctions, nulls, numeric types,
   diagnostic context and lifecycle status.
3. Reduce repeated records only after retaining enough records to demonstrate ordering,
   deduplication, malformed-record isolation and continuation behaviour. Do not invent an envelope
   that has not been observed in repository examples.
4. Add one manifest fixture record with all required metadata. Update the endpoint inventory's
   covered scenarios or explain why the state is inapplicable.
5. Replay the payload through production code in `tests/test_afl_json_fixture_contracts.py`.
   Required-field failures must identify the endpoint and local record; optional-field changes use
   supported defaults or structured diagnostics rather than a broad failure.

A `401` fixture contains only the provider error body and must become
`AflJsonAuthenticationError` after the one bounded refresh. `CFSSDS001` is represented as a
sanitised 404 error body and must become `AflJsonResourceUnavailable`. Live statistics use
`matchStatus: LIVE`, may contain null/missing team arrays and incomplete metrics, and normalise to
`LIVE_PARTIAL`; final fixtures use `CONCLUDED` and retain all expected statistic fields.

## Running offline

Run `pytest -q tests/test_afl_json_fixture_contracts.py`. The module blocks Python socket creation
and `requests.Session.request` for every test, so accidental external access fails immediately.
Then run the existing JSON/CFS suites and full `pytest` suite.

This work replaces only the JSON portion of Issue #48. The HTML fixtures, manifest entries and
parser regression tests under `tests/fixtures/afl/`, `tests/fixtures/afl_sources/` and
`tests/test_afl_golden_fixtures.py` remain intact; expanding the postponed all-HTML corpus is not
part of this contract suite.
