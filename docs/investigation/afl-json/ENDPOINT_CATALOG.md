# AFL JSON Collection Module - Endpoint Catalogue

## 1. Purpose

This document converts the current AFL website investigation into an implementation contract for a JSON collection module. It separates:

- public AFL API endpoints (`aflapi.afl.com.au`), which currently work without a token;
- protected CFS endpoints (`api.afl.com.au/cfs/afl`), which require a WMCTok value in the `x-media-mis-token` header;
- source identifiers from AFL's numeric IDs and Champion Data provider IDs (`CD_*`);
- collection behaviour from database persistence.

The collector should retain raw source identifiers and raw payload metadata even where the existing database uses different names.

The maintained, machine-readable source of truth is `afl_json/contracts.py`.
Collectors must import that registry instead of copying URLs or request rules. This
document provides rationale and investigation detail; where it differs from the
registry, the registry is authoritative. Fields still awaiting verification are
listed both in section 9 and in each endpoint's `unverified_fields` value.

## 2. Authentication contract

### 2.1 WMCTok

- Method: `POST`
- URL: `https://api.afl.com.au/cfs/afl/WMCTok`
- Authentication required: no
- Response value used: `token`
- Request header for protected calls: `x-media-mis-token: <token>`

Recommended behaviour:

1. Cache the token in memory.
2. Reuse it across endpoints and matches.
3. On HTTP 401, obtain a new token and retry the failed request once.
4. Treat HTTP 404 with `CFSSDS001` as an authenticated request for data that is not yet published, not as an authentication failure.
5. Do not log or persist the token in collection metadata.

Observed token lifetime is at least 125 minutes. Refresh-on-401 is therefore preferable to refreshing before every request.

## 3. Identifier conventions

| Entity | AFL numeric ID | Champion Data/provider ID | Example |
|---|---:|---|---|
| Competition | `id` | `providerId` | `1`, `CD_C014` |
| Competition season | `id` | `providerId` | `85`, `CD_S2026014` |
| Round | `id` | `providerId` | `1363`, `CD_R202601420` |
| Match | `id` | `providerId` | `8216`, `CD_M20260142001` |
| Team | `id` | `providerId` | `1`, `CD_T10` |
| Club | `id` | `providerId` | `3`, `CD_O1` |
| Venue | `id` | `providerId` | `31`, `CD_V6` |
| Player | AFL player ID via ID map | CFS `playerId` | `7229`, `CD_I1022911` |

Do not derive relationships by parsing the characters inside `CD_*` identifiers. Treat each identifier as opaque.

## 4. Endpoint catalogue

### E01 - Competitions

- Method: `GET`
- URL template: `https://aflapi.afl.com.au/afl/v2/competitions?pageSize={page_size}`
- Token: no
- Scope: competition discovery
- Collection key: `competitions[].id`
- Provider key: `competitions[].providerId`
- Important fields:
  - `id`
  - `providerId`
  - `code`
  - `name`
- Suggested refresh: yearly and on application bootstrap
- Notes:
  - AFL Men's Premiership has been observed as numeric ID `1` and provider ID `CD_C014`.
  - Select by stable competition code/provider ID rather than assuming the first or highest numeric ID.

### E02 - Competition seasons

- Method: `GET`
- URL template: `https://aflapi.afl.com.au/afl/v2/competitions/{competition_id}/compseasons?pageSize={page_size}`
- Token: no
- Scope: seasons belonging to a competition
- Collection key: `compSeasons[].id`
- Provider key: `compSeasons[].providerId`
- Important fields:
  - `id`
  - `providerId`
  - `name`
  - `shortName`
  - `currentRoundNumber`
- Suggested refresh: daily during a season; yearly outside a season
- Pagination observation: requested page sizes may be capped; collector must follow response pagination rather than trusting the requested size.
- Selection rule: identify the target season from configured year/competition and response fields; do not assume the greatest numeric ID is always current.

### E03 - Rounds

- Method: `GET`
- URL templates:
  - `https://aflapi.afl.com.au/afl/v2/compseasons/{comp_season_id}/rounds?pageSize={page_size}`
  - `https://aflapi.afl.com.au/afl/v2/compseasons/{comp_season_id}/rounds?roundNumber={round_number}&pageSize={page_size}`
- Token: no
- Scope: rounds in a competition season
- Collection key: `rounds[].id`
- Provider key: `rounds[].providerId`
- Important fields:
  - `id`
  - `providerId`
  - `abbreviation`
  - `name`
  - `roundNumber`
  - `byes[]`
  - `utcStartTime`
  - `utcEndTime`
- Suggested refresh: daily; more often around fixture changes
- Notes:
  - Round zero/Opening Round is valid.
  - Bye teams embed team and club identifiers.

### E04 - Teams for a season

- Method: `GET`
- URL template: `https://aflapi.afl.com.au/afl/v2/teams?compSeasonId={comp_season_id}&pageSize={page_size}`
- Token: no
- Scope: teams participating in a competition season
- Collection key: `teams[].id`
- Provider key: `teams[].providerId`
- Important fields:
  - team identity and naming fields
  - nested `club`
  - `metadata`
  - `teamType`
- Suggested refresh: weekly and before each round
- Persistence note:
  - Keep canonical team identity separate from display names.
  - AFL display names may change for themed rounds. Store observed names as season/round presentation data rather than overwriting the canonical club identity without history.

### E05 - Matches

- Method: `GET`
- URL template: `https://aflapi.afl.com.au/afl/v2/matches?pageSize={page_size}&competitionId={competition_id}&compSeasonId={comp_season_id}&roundNumber={round_number}`
- Token: no
- Scope: fixture and result records
- Collection key: `matches[].id`
- Provider key: `matches[].providerId`
- Important fields:
  - competition season and round references
  - `home.team`, `away.team`
  - `home.score`, `away.score`
  - `venue`
  - `utcStartTime`
  - `status`
  - `metadata`
- Observed statuses:
  - `PLACEHOLDER`
  - `SCHEDULED`
  - `UNCONFIRMED_TEAMS`
  - `CONFIRMED_TEAMS`
  - `LIVE`
  - `CONCLUDED`
- Suggested refresh:
  - future fixtures: daily
  - current round: hourly before match day
  - near/live matches: 1-5 minutes as appropriate
  - concluded matches: one final confirmation, then immutable unless reconciliation detects change
- Status policy: preserve the source status string and map separately to any legacy internal enum.

### E06 - Player ID map

- Method: `GET`
- URL: `https://aflapi.afl.com.au/afl/v2/players/idmap`
- Token: no
- Scope: crosswalk from Champion Data player ID to AFL numeric player ID
- Response shape: `idMapResponse.ids` object
- Example: `"CD_I1022911": 7229`
- Suggested refresh: weekly and after list changes
- Persistence:
  - store both IDs;
  - use the Champion Data ID as the join key for CFS match/stat data;
  - do not assume every ID exists in both systems.

### E07 - Season player list

- Method: `GET`
- URL template: `https://api.afl.com.au/cfs/afl/players?seasonId={season_provider_id}`
- Token: yes
- Scope: all players associated with a season
- Collection key: `players[].playerId`
- Observed 2026 result: 812 players, one page when only `seasonId` was supplied
- Important fields:
  - `playerId`
  - `playerName.givenName`
  - `playerName.surname`
  - `team.teamId`
  - `team.teamAbbr`
  - `team.teamName`
  - `team.teamNickname`
  - `jumperNumber`
  - `playerPosition`
  - `photoURL`
- UI-supported optional parameters:
  - `pageSize`
  - `pageNum`
  - `sortBy`
  - `teamIds`
  - `playerPosition`
- Suggested refresh: daily during list-management periods; weekly otherwise
- Completeness guard:
  - verify `len(players) == totalResults`;
  - if not, explicitly paginate until all pages are collected.
- Modelling note: team, jumper number, position and photo URL are season-scoped attributes, not necessarily permanent player attributes.

### E08 - Match player statistics

- Method: `GET`
- URL template: `https://api.afl.com.au/cfs/afl/playerStats/match/{match_provider_id}`
- Token: yes
- Scope: player statistics for one match
- Record collections:
  - `homeTeamPlayerStats[]`
  - `awayTeamPlayerStats[]`
- Natural key: `(match_provider_id, player_id)`
- Important identity paths observed:
  - `*.player.player.player.playerId`
  - `*.player.player.player.playerName.givenName`
  - `*.player.player.player.playerName.surname`
  - `*.player.teamId`
  - `*.player.jumperNumber`
  - `*.player.photoURL`
- Important statistic path: `*.playerStats.stats`
- Core BBBFL fields:
  - `goals`
  - `behinds`
  - `kicks`
  - `handballs`
  - `disposals`
  - `marks`
  - `tackles`
  - `hitouts`
- Other useful fields already observed:
  - possessions, inside 50s, clearances, efficiency, clangers, frees, Dream Team points, rebounds, assists, rating points, turnovers, intercepts, score involvements, metres gained and `extendedStats`
- Suggested refresh:
  - live match: 1-5 minutes
  - immediately after conclusion
  - later reconciliation pass after the round
- Numeric policy:
  - provider values may be JSON floating-point values even for counts;
  - convert to integer only when the value is mathematically integral;
  - preserve nulls and never convert a missing stat to zero without an explicit business rule.

### E09 - Match rosters by round

- Method: `GET`
- URL template: `https://api.afl.com.au/cfs/afl/matchRosters/round/{round_provider_id}`
- Token: yes
- Scope: named players/rosters for every match in a round
- Suggested refresh:
  - after team announcements;
  - more frequently until match start when final teams may change;
  - final snapshot after each match.
- Intended use:
  - named-to-play state;
  - roster and selection history;
  - distinguishing listed players from actual match participants.
- Gap: full response schema and stable natural key still need to be documented.

### E10 - Stats Centre players

- Method: `GET`
- Example URL: `https://api.afl.com.au/cfs/afl/statsCentre/players?competitionId=CD_S2026014&teamIds=CD_T60,CD_T150`
- Token: yes
- Scope: stats-centre player result set
- Current priority: low
- Reason: appears to overlap with match player stats and may use confusing parameter naming (`competitionId` carrying a season provider ID).
- Action: retain as an investigated endpoint but do not make it a foundation dependency until its unique value is established.

## 5. Known but not yet catalogued CFS endpoints

Previous investigation has also identified endpoint families for interchange and commentary. They should not be enabled by default until a representative response, key structure and update semantics are captured.

Candidate families:

- match interchange
- commentary feed

Track these as research items rather than guessing their production contracts.

## 6. Canonical field mapping for match player statistics

The original Codex table was based on HTML scraping and contained placeholder JSON paths. For the JSON collector, most HTML fallback rules are no longer primary. Keep the HTML scraper only as a fallback adapter.

| Canonical field | Existing database column | Preferred JSON source | Policy |
|---|---|---|---|
| `match_id` | `player_stats.match_id` | collector context from `{match_provider_id}` plus numeric match lookup | required |
| `round_id` | `player_stats.round_id` | match record/collector context | resolve from match table |
| `player_id` | `player_stats.afl_id` | ID map using CFS `playerId` | nullable until crosswalk resolves |
| `champion_data_player_id` | `player_stats.champion_id` | roster item `playerId` | required source identity; preserve full `CD_I...` value |
| `player_name` | `player_stats.player_name` | `playerName.givenName` + `playerName.surname` | trim and join for legacy column |
| `jumper_number` | `player_stats.jumper_number` | roster/player wrapper `jumperNumber` | nullable |
| `team_code` | `player_stats.team_code` | resolve `teamId` through team catalogue | do not derive from CSS |
| `fantasy_score` | `player_stats.af_score` | `playerStats.stats.dreamTeamPoints` | nullable; confirm this is the desired fantasy source |
| `goals` | `player_stats.goals` | `playerStats.stats.goals` | nullable numeric count |
| `behinds` | `player_stats.behinds` | `playerStats.stats.behinds` | nullable numeric count |
| `disposals` | `player_stats.disposals` | `playerStats.stats.disposals` | nullable numeric count |
| `kicks` | `player_stats.kicks` | `playerStats.stats.kicks` | nullable numeric count |
| `handballs` | `player_stats.handballs` | `playerStats.stats.handballs` | nullable numeric count |
| `marks` | `player_stats.marks` | `playerStats.stats.marks` | nullable numeric count |
| `tackles` | `player_stats.tackles` | `playerStats.stats.tackles` | nullable numeric count |
| `hitouts` | `player_stats.hitouts` | `playerStats.stats.hitouts` | nullable numeric count |
| `clearances` | `player_stats.clearances` | `playerStats.stats.clearances.totalClearances` | nullable numeric count |
| `metres_gained` | `player_stats.metres_gained` | `playerStats.stats.metresGained` | signed numeric until verified otherwise |
| `goal_assists` | `player_stats.goal_assists` | `playerStats.stats.goalAssists` | nullable numeric count |
| `time_on_ground_percent` | `player_stats.time_on_ground_pct` | not yet verified in the supplied playerStats sample | gap; do not source from HTML if JSON-only mode is required |
| `match_status` | `player_stats.status` | public match record `status` | preserve source; map separately for legacy DB |
| `observed_at` | `player_stats.scraped_at` | collector UTC timestamp | required adapter metadata |
| `source_metadata` | no current column | collector envelope | recommended for audit/replay |

## 7. Source priority

Recommended authority order by domain:

1. Public API for competition, season, round, team, match, venue and match status.
2. CFS season players for the season player population and season-specific player attributes.
3. Public ID map for CFS-to-AFL player crosswalk.
4. CFS match rosters for selection/roster state.
5. CFS match player statistics for match-level performance.
6. Existing HTML scraper only as a monitored fallback, not as the normal source.

## 8. Required collection metadata

Every fetch should produce an ingestion envelope containing:

- endpoint catalogue key;
- resolved URL with credentials omitted;
- request timestamp in UTC;
- HTTP status;
- source system (`public_api` or `cfs`);
- relevant scope IDs (competition, season, round, match);
- payload byte count;
- SHA-256 payload hash;
- parser/schema version;
- retry count;
- result count where supplied;
- error code/message where supplied.

Raw payload retention can be configurable, but payload hashes and collection logs should be retained.

## 9. Remaining gaps and recommended tracked issues

### Priority 0 - required before replacing the current player-stat scraper

1. **Verify complete playerStats field paths.** Capture and store representative pre-match, live and concluded responses. Confirm whether nesting remains stable and whether time-on-ground is present under another path.
2. **Define ID persistence.** Decide whether current numeric `afl_id` remains the database conflict key or whether `CD_I...` becomes the primary external identity with AFL numeric ID as a cross-reference.
3. **Confirm fantasy score semantics.** Verify that `dreamTeamPoints` is the value expected for `af_score`, and document whether BBBFL scoring is calculated independently.
4. **Define null versus zero policy.** Especially for unpublished, not-applicable and not-yet-updated statistics.
5. **Define live-to-final reconciliation.** Specify when a match record is considered final and how later provider corrections are applied.

### Priority 1 - required for a complete collection module

6. **Document matchRosters response schema.** Capture natural keys, team lists, emergencies, late changes and status semantics.
7. **Capture pagination envelopes for every list endpoint.** Implement generic pagination rather than endpoint-specific assumptions.
8. **Build status mapping tests.** Include every observed public match status and preserve unknown values safely.
9. **Team naming history.** Decide how themed-round names are stored without corrupting canonical team identity.
10. **Season player list definition.** Determine why the 2026 endpoint returns 812 players and whether this includes inactive, delisted, supplementary or historical-in-season players. Add active/list-status handling if another source exposes it.
11. **ID-map completeness audit.** Measure unmapped CFS players and stale AFL IDs.
12. **Schema drift detection.** Store sample fixtures and fail visibly when required fields disappear or change type.

### Priority 2 - valuable extensions

13. **Interchange endpoint contract.** Capture URLs, schema, sequence keys and update frequency.
14. **Commentary endpoint contract.** Capture pagination/ordering and determine whether it is worth retaining.
15. **Venue catalogue.** Decide whether venues should be normalised from matches or collected from a dedicated endpoint.
16. **Historical backfill policy.** Define which seasons and endpoint variants are supported.
17. **Rate limiting and politeness controls.** Set concurrency, timeout, jitter and backoff rules despite no observed explicit rate limit.
18. **Raw payload retention.** Decide retention period and compression for reproducibility and future remapping.

## 10. Codex implementation boundary

Initial implementation should build collection and normalisation infrastructure without immediately rewriting all database tables. Recommended first increment:

- token provider;
- shared HTTP client with retry and structured errors;
- endpoint registry;
- generic paginator;
- collectors for competitions, seasons, rounds, teams, matches, ID map, season players and match player stats;
- raw/normalised response models;
- fixture-based tests using sanitised payloads;
- dry-run command that writes JSON outputs without persistence;
- optional persistence adapter behind an interface.

This keeps source discovery, collection, normalisation and database migration independently testable.
