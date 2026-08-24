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

### E05a - Match detail

- Method: `GET`
- URL template: `https://aflapi.afl.com.au/afl/v2/matches/{afl_match_id}`
- Token: no
- Scope: one match's current public lifecycle status and identifiers
- Collection key: `matches[].id`
- Provider key: `matches[].providerId`
- Reconciliation use: advance, but never downgrade, the canonical match status
  before assigning player-stat snapshot authority

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
  - `*.player.jumperNumber`
  - `*.player.photoURL`
- Team identity in currently verified payloads: no `teamId`, `teamProviderId`,
  `squadId`, `clubId`, or
  equivalent independent identifier was found in the player records, the home
  or away collection containers, or match-level metadata in the inspected
  upcoming/live/postgame/concluded responses. The array name supplies only
  side context, not Champion Data `CD_T...` identity. This does not rule out a
  future optional field; such a field requires a documented namespace contract
  before it can be mapped.
- Important statistic path: `*.playerStats.stats`
- Core canonical AFL statistic fields:
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

**Update (Issue #193):** `matchInterchange/{matchProviderId}` is now under
active diagnostic verification via the checked-in `interchange` diagnostic
profile (see `docs/diagnostics_framework.md`,
`collection/match_interchange_evidence.py`,
`scheduler/match_interchange_capture.py`). This is opt-in, evidence-capture-only
polling to observe live behaviour ahead of a real match -- it is **not** a
production collector, is not wired into the consumer API, and this endpoint
remains an unverified, uncatalogued CFS endpoint with no production contract
until a separate, deliberate decision promotes it. A concluded-match response
has been observed (`tests/fixtures/afl/interchange/`) with top-level
`matchId`, `homeInterchange[]`, `awayInterchange[]`, `homeInterchangeCounts`
and `awayInterchangeCounts`; live-match behaviour (in particular whether
`homeInterchange[]`/`awayInterchange[]` membership means a player is
currently off the ground) is exactly what the `interchange` profile is
gathering evidence to answer.

**Update (Issue #196):** `commentaryFeed/{matchProviderId}` is now under
active diagnostic verification via the checked-in `commentary` diagnostic
profile (see `docs/diagnostics_framework.md`,
`collection/match_commentary_evidence.py`,
`scheduler/match_commentary_capture.py`). Like `matchInterchange` above, this
is opt-in, evidence-capture-only polling -- it is **not** a production
endpoint contract, is not wired into the consumer API, does not make match
state or match finality depend on commentary text, and remains entirely
independent of the production design work tracked in Issue #187. A
concluded-match response has been observed
(`tests/fixtures/afl/commentary/match_8216_commentary_concluded.json`, plus a
reduced fixture at `tests/fixtures/afl/commentary/commentary_feed_reduced.json`
used by the profile's own tests) with top-level `matchId`, `lastUpdated` and
an accumulated, newest-first `commentaryEvent[]` array whose entries carry
`comment`, `periodNumber`, `periodSeconds`, `playerId`, `teamId` and
`scoreEvent` -- but **no event identifier of any kind**, which is why the
diagnostic layer computes its own conservative fingerprint for deduplication
rather than assuming one exists. Live-match behaviour -- when the feed first
becomes available relative to the scheduled bounce, whether quarter-start/
quarter-end markers and score events are consistently well-formed, and
whether previously published entries are ever edited, removed or reordered
-- is exactly what the `commentary` profile is gathering evidence to answer
across the remaining Round 24 matches, alongside `match_clock` and
`interchange`.

**Update (Issue #201): `commentaryFeed/{matchProviderId}` is now a
production-supported endpoint contract**, promoted from the Issue #196
diagnostic investigation on real Round 24 evidence -- a live-poll capture
sequence and a raw Bruno `.response.json` snapshot for `CD_M20260142409`
(West Coast Eagles v Hawthorn, POSTGAME/CONCLUDED), plus the combined
diagnostic-evidence report covering the rest of that weekend's matches (see
`tests/fixtures/afl/commentary/commentary_CD_M20260142409.metadata.json`).
This section records the **confirmed production contract**; the diagnostic
evidence-capture pathway described above remains running and useful, but is
no longer the only or the authoritative path -- see the "Production vs.
diagnostic" note below.

*Endpoint:* `GET {CFS root}/commentaryFeed/{match_provider_id}` --
`https://api.afl.com.au/cfs/commentaryFeed/{match_provider_id}`, one
directory above the `/cfs/afl` root most other CFS endpoints live under
(Issue #199 tracks a possible future URL-model refactor; the production
endpoint definition in `afl_json/match_commentary.py` uses the same
`base_url_override` technique as the diagnostic definition rather than
pre-empting that refactor).

*Confirmed feed-level fields:* `matchId` (Champion Data match id),
`lastUpdated` (ISO-8601 with milliseconds, e.g.
`2026-08-23T12:15:40.217+0000`), `commentaryEvent[]`.

*Confirmed event-level fields (unchanged from Issue #196, now confirmed on a
concluded real match):* `comment`, `periodNumber`, `periodSeconds`,
`playerId`, `teamId`, `scoreEvent`. **No additional structured scoring
fields** (e.g. a points value or a discrete goal/behind/rushed type) were
present anywhere in the captured concluded response -- `scoreEvent` remains
the only structured scoring fact; the outcome type stays free text only.

*Confirmed accumulation/ordering behaviour:* the feed is still an
**accumulated, newest-first** array with **no upstream event identifier** --
confirmed again on real concluded data (period/second strictly
non-increasing from array index 0). Multiple events legitimately share one
`(periodNumber, periodSeconds)` pair (e.g. general statistical commentary
and a scoring event both timestamped `period=1, seconds=1483` in the
captured `CD_M20260142409` response).

*Confirmed scoreEvent behaviour:* `scoreEvent=true` events can have a null
`playerId` with a non-null `teamId` for a rushed behind (e.g.
`"BEHIND - Eagles (Rushed)"`), confirming the diagnostic evidence's
team-only score-event case on real data.

*Confirmed player/team identity supply:* structured `playerId`/`teamId`
only, exactly as documented under Issue #196; pre-match (`periodNumber=0`)
and general narrative commentary carry both as null. Never inferred from
the `comment` text.

*New finding -- `lastUpdated` is not a reliable change signal:* the Bruno
capture's `lastUpdated` (`12:15:40.217`) is materially newer than the
diagnostic capture's final poll for the same match
(`observed_at=2026-08-23T12:13:44Z`) with an identical event count --
`lastUpdated` can advance without new event content appearing. Production
ingestion must dedupe by event fingerprint, never by watching `lastUpdated`
alone.

*New finding -- a genuine same-slot scoring-outcome change was observed* in
the combined Round 24 diagnostic evidence, though not in `CD_M20260142409`
itself (that match's supplied evidence shows no such sequence in either
file). A different Round 24 match in the same capture set,
`CD_M20260142406`, recorded `"GOAL - Bulldogs (Cody Weightman)"` at
`period=3, seconds=839` (live diagnostic poll evidence), then a later poll
recorded a second, distinct event `"BEHIND - Bulldogs (Cody Weightman)"` at
the *same* `(periodNumber, periodSeconds, playerId, teamId, scoreEvent)`
slot. The real final concluded-match capture for this match
(`tests/fixtures/afl/commentary/commentary_CD_M20260142406_full.json`,
supplied directly by the repository owner) confirms only the `BEHIND`
remains at that slot -- the upstream feed itself appears to replace an
entry's text in place rather than only ever appending. AFL-api's own
persistence deliberately does not mirror that: it records the `BEHIND` as a
new event and links it via `possible_edit_of_event_id`, but never deletes
or rewrites the row already stored for the `GOAL` (see
`tests/fixtures/afl/commentary/commentary_CD_M20260142406_score_review.metadata.json`
for the full evidence chain and provenance). Nothing in the feed identifies
*why* the outcome changed -- no explicit review/correction marker exists --
so this is documented as an observed "scoring-outcome change", not an
"official review" or "reversal". This validates, on real evidence, the
"possible edit" slot-key detection Issue #196 already implemented for
exactly this shape of event, and is the basis for the production
`possible_edit_of_event_id` linkage in `afl_json/match_commentary.py`.

*POSTGAME/CONCLUDED behaviour:* the feed remains queryable and stable after
a match concludes -- the Bruno capture above was taken after the
diagnostic profile's final live poll, well into POSTGAME/CONCLUDED, and
returned the complete event history (as it stood at that time -- see the
same-slot scoring-outcome change finding above for a case where that
history no longer includes an earlier entry's exact text). Production
polling therefore continues through POSTGAME and for a bounded grace period
after, specifically to catch a late-arriving scoring-outcome change like
the one above (see `scheduler/match_commentary_production.py`).

*Production vs. diagnostic:* the diagnostic `commentary` profile
(`collection/match_commentary_evidence.py`,
`scheduler/match_commentary_capture.py`,
`commentary_evidence_polls`/`commentary_evidence_events`) is **unchanged and
still running independently** -- it remains useful for parser-regression
evidence, replay investigation, and confirming whether an apparent event
mutation originated upstream. It is **not** the backing store for the
consumer API. Production ingestion is a new, separate, narrowly-scoped path
(`afl_json/match_commentary.py`, `scheduler/match_commentary_production.py`,
`match_commentary_events`/`match_commentary_polls` -- migration `0019`),
mirroring how `afl_json/match_period.py` (Issue #187) sits alongside its own
diagnostic predecessor. See `docs/architecture/api/commentary_api_design.md`
for the full production/consumer design.

**Update (Issue #204): `matchInterchange/{matchProviderId}` is now a
production-supported endpoint contract for per-player interchange state**,
promoted from the Issue #193 diagnostic investigation. This PR's initial
draft had only a single CONCLUDED-match fixture to promote against
(materially weaker than Issue #201's commentary promotion); real Round 24
live diagnostic observations across 7 matches were subsequently supplied
and reviewed on PR #206, confirming the array-membership semantic for LIVE
play -- see below.

*Endpoint:* `GET {CFS root}/matchInterchange/{match_provider_id}` -- under
the standard `/cfs/afl` root, unlike `commentaryFeed`.

*Confirmed feed-level and entry-level fields* (unchanged from Issue #193,
now the production contract in `afl_json/match_interchange.py`): top-level
`matchId`, `homeInterchange[]`, `awayInterchange[]`, `homeInterchangeCounts`,
`awayInterchangeCounts`. Each interchange entry carries `teamId`,
`player.playerId` (plus `player.playerName`/`player.playerJumperNumber`,
deliberately **not** persisted -- identity resolution uses `playerId` only),
`interchangeCount`, `benchReason`, `timeOnGround`, `timeOnBench`,
`powerRating`. The team-level `home/awayInterchangeCounts` totals remain
diagnostic-scope only (still captured by the `interchange` diagnostic
profile) and are deliberately out of scope for the narrower production
contract, which answers one consumer question: is this canonical player
currently on the interchange list, and what does CFS say about them.

*What promotion evidence exists:* a single real captured concluded-match
response (`tests/fixtures/afl/interchange/match_interchange_8216_concluded.json`,
five entries per side, `benchReason="ROTATION"` throughout) confirming the
entry-level field shape, plus real Round 24 live diagnostic observations
across **7 matches** (`CD_M20260142401`, `CD_M20260142403`-`CD_M20260142406`,
`CD_M20260142408`, `CD_M20260142409` -- the `scripts/report_interchange_evidence.py`
output reviewed on PR #206), each polled roughly every 15 seconds through
its full ~3 hour LIVE window.

*Array-membership semantics: confirmed for LIVE play.* Issue #204 asked
this promotion to establish, from evidence, whether membership means "the
player is currently off the ground". The Round 24 live evidence confirms
this for LIVE play: the diagnostic module's `appeared`/`disappeared` flags
are an exact Champion Data `playerId` set difference (not an inference),
and across the 7 matches membership changed hundreds of times per match
(442/435 home appear/disappear events, 435/428 away), tightly
time-correlated with each team's own `totalInterchangeCount` incrementing,
with same-poll appear+disappear pairing holding each side's listed count
at a steady 5 (self-correcting after a handful of transient 4/6 blips --
e.g. `CD_M20260142403` seq 349, `CD_M20260142409` seq 329-332). This rules
out the "fixed, always-listed pool" reading this promotion originally could
not exclude. A subsequent full per-poll export for `CD_M20260142409`
(Issue #204 comment) closed the two remaining gaps: Champion Data player
`CD_I1028561` ("Tom Gross") is individually cited appearing/disappearing
from `homeInterchange[]` five separate times across the match (real raw
payloads checked in at `tests/fixtures/afl/interchange/match_interchange_CD_M20260142409_poll{002,048,100}_*.json`),
and 40 captured `POSTGAME` polls (poll_sequence 654-693) show every field
byte-identical throughout with zero transitions -- `matchInterchange` state
freezes exactly at the `LIVE` -> `POSTGAME` transition. See
`docs/architecture/api/interchange_api_design.md` §2.1 for the full
evidence. **CONCLUDED remains the one open question**: this match's
capture never reached it. The production contract accordingly exposes
**`on_bench`** (see `docs/api_v1_interchange.md`), confirmed for LIVE play
and confirmed to freeze through POSTGAME, documented with that one residual
caveat.

*`benchReason`:* persisted and returned exactly as CFS supplies it (only
`"ROTATION"` observed so far). Never inferred as injury, substitution,
tactical, or medical from commentary, timing, or any other field.

*Identity resolution:* `player.playerId`/`teamId` resolve through the
existing `player_provider_ids`/`afl_teams.provider_id` crosswalks, exactly
like `afl_json/match_commentary.py`. Display name and jumper number are
never used for identity. Unresolved crosswalks stay `NULL`, never guessed.

*Scheduling/lifecycle:* production polling covers LIVE, POSTGAME, a bounded
pre-kickoff tolerance, and a bounded post-active grace window after
LIVE/POSTGAME (covering the CONCLUDED transition) -- the same
stateless, self-terminating candidate-window pattern as commentary
production (`scheduler/match_interchange_production.py`). Interchange
availability never affects match finality, lifecycle, or authoritative
player-stat collection.

*Production vs. diagnostic:* the diagnostic `interchange` profile
(`collection/match_interchange_evidence.py`,
`scheduler/match_interchange_capture.py`,
`match_interchange_evidence_observations`) is **unchanged and still
running independently** -- it remains useful for parser-regression evidence
and for gathering the still-missing live-membership-transition evidence.
It is **not** the backing store for the consumer API. Production ingestion
is a new, separate, narrowly-scoped path (`afl_json/match_interchange.py`,
`scheduler/match_interchange_production.py`,
`match_interchange_state`/`match_interchange_events`/`match_interchange_polls`
-- migration `0021`), mirroring the commentary (Issue #201) and match-period
(Issue #187) promotion precedents. See
`docs/architecture/api/interchange_api_design.md` for the full production/
consumer design.

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
3. **Confirm fantasy score semantics.** Verify that `dreamTeamPoints` is the value expected for `af_score`, and document that any consumer-defined scoring is calculated independently by downstream applications.
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
