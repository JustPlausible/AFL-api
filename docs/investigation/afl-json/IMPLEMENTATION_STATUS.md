# AFL Source and Endpoint Implementation Status

**Repository revision audited:** `b977c8323794028a624e637f3f2ddf0b76145f4d`  
**Audit date:** 2026-08-15  
**Scope:** discovered AFL/CFS/HTML sources -> collector implementation -> persistence -> collection trigger -> consumer API exposure.

## 1. Purpose

This document records the implementation status of known AFL data sources across four separate layers:

1. whether the upstream source or endpoint has been discovered and sufficiently understood;
2. whether AFL-api has a maintained collector for it;
3. whether resulting data is persisted, and how collection/persistence is triggered; and
4. whether persisted data is exposed for consumer API use.

These questions remain deliberately separate. A working upstream endpoint does not imply that AFL-api collects it; a collector does not imply persistence; persistence does not imply scheduled collection; and persisted data is not necessarily part of the supported consumer API.

This document complements:

- `docs/investigation/afl-json/ENDPOINT_CATALOG.md`, which describes discovered structured sources;
- `docs/scraper_source_inventory.md`, which documents HTML and alternative-source contracts;
- `docs/operational_source_policy.md`, which determines operational source selection; and
- the `/api/v1` design documents, which define the supported consumer-facing contract.

`afl_json/contracts.py` is the machine-readable registry for structured endpoints implemented by the maintained collector stack. The investigation catalogue remains useful for endpoints that have been observed but have not graduated into maintained collection.

## 2. Status terminology

| Status | Meaning |
|---|---|
| **Operational** | Used by normal CLI, Scheduler or Admin workflows. |
| **Implemented/manual** | Collector exists but requires an explicit manual/CLI action. |
| **Read-only** | Collector exists but application database persistence is deliberately absent. |
| **Legacy/manual** | Supported explicit compatibility path but no longer the preferred operational source. |
| **Investigated only** | Endpoint/source has been observed or documented but no maintained collector exists. |
| **Not consumer exposed** | Data may exist internally but no supported consumer route directly exposes it. |

The operational source policy prevents silent fallback and dual execution. CFS data-not-published states do not implicitly select an HTML fallback.

## 3. Structured AFL/CFS endpoint implementation matrix

### E00 - CFS `WMCTok`

**Endpoint:** `POST https://api.afl.com.au/cfs/afl/WMCTok`

| Layer | Status |
|---|---|
| Discovered | **Yes** |
| Collector/client support | **Yes - operational infrastructure** |
| Database persistence | **No - deliberately prohibited** |
| Manual use | Indirectly through CFS collectors |
| Scheduled use | Indirectly through scheduled CFS player-stat collection |
| Consumer API | **No** |

`AflJsonClient` obtains and caches the token and refreshes it after authentication failure. It is transient authentication material rather than application data.

**Assessment:** complete infrastructure; no product-level persistence is required.

### E01 - Competitions

**Endpoint:** `/afl/v2/competitions`

| Layer | Status |
|---|---|
| Collector | **Yes** |
| Normalisation | Yes |
| Persistence | **Yes**, through canonical metadata persistence |
| Manual trigger | Season bootstrap/sync; read-only metadata collection tools |
| Scheduled trigger | **Yes**, through operational metadata refresh |
| Consumer API | **No direct competition resource** |

The public metadata collector is the preferred production source for the competition/season/round/team/match hierarchy.

**Assessment:** no immediate consumer API requirement while competition discovery is not a supported consumer workflow.

### E02 - Competition seasons

**Endpoint:** `/afl/v2/competitions/{competition_id}/compseasons`

| Layer | Status |
|---|---|
| Collector | **Yes** |
| Persistence | **Yes - `afl_seasons`** |
| Manual trigger | `--bootstrap-afl-season`, `--sync-afl-season` |
| Scheduled metadata refresh | **Yes** |
| Consumer API | **Yes - `/api/v1/seasons`** |

Bootstrap persists the canonical season foundation before collecting the corresponding CFS player population. The v1 consumer route projects reviewed canonical fields rather than returning provider payloads directly.

**Assessment:** implemented end-to-end.

### E03 - Rounds

**Endpoint:** `/afl/v2/compseasons/{comp_season_id}/rounds`

| Layer | Status |
|---|---|
| Collector | **Yes** |
| Persistence | **Yes - `rounds`** |
| Manual trigger | Bootstrap/sync |
| Scheduled trigger | **Yes - metadata refresh** |
| Consumer API | **Yes** |
| Compatibility API | Yes |

Supported v1 resources include season-round and individual-round projections. Older compatibility routes also remain available.

**Assessment:** implemented end-to-end.

### E04 - Teams

**Endpoint:** `/afl/v2/teams?compSeasonId=...`

| Layer | Status |
|---|---|
| Collector | **Yes** |
| Persistence | **Yes - canonical AFL team/season relationships** |
| Manual trigger | Bootstrap/sync |
| Scheduled trigger | **Yes - metadata refresh** |
| Consumer API | **Indirect only** |

Teams are used by match projections and round/bye information. There is currently no dedicated `/api/v1/teams` resource.

**Potential future decision:** add a canonical team resource only when consumer workflows require team discovery independently of matches or rounds.

### E05 - Matches / fixture records

**Endpoint:** `/afl/v2/matches?...`

| Layer | Status |
|---|---|
| Collector | **Yes** |
| Persistence | **Yes - `matches`** |
| Manual trigger | Bootstrap/sync |
| Scheduled trigger | **Yes - metadata refresh** |
| Consumer API | **Yes** |
| Compatibility API | Yes |

The canonical match hierarchy is a mature public-JSON -> persistence -> consumer API path.

**Assessment:** implemented end-to-end.

### E05a - Public match detail

**Endpoint:** `/afl/v2/matches/{afl_match_id}`

**Purpose:** obtain the latest public lifecycle/status for one match.

| Layer | Status |
|---|---|
| Collector | **Yes** |
| Persistence | **Yes - reconciles canonical match status** |
| Manual use | Used by player-stat workflows and season sync |
| Scheduled use | **Yes** |
| Consumer API | **Indirect through match resources and player-stat lifecycle** |

Public match detail is used to reconcile lifecycle state before CFS player-stat processing. Scheduler planning derives fixture timing from persisted public AFL match metadata rather than CFS timestamps.

**Assessment:** operational and architecturally important.

### E06 - Player ID map

**Endpoint:** `/afl/v2/players/idmap`

**Purpose:** Champion Data `CD_I...` to AFL numeric player ID crosswalk.

| Layer | Status |
|---|---|
| Collector | **Yes** |
| Validation | **Yes - duplicate/conflict checks** |
| Persistence | **Yes - canonical/provider identity mapping** |
| Manual trigger | Season bootstrap/sync |
| Scheduled trigger | **No dedicated refresh job** |
| Consumer API | **Indirect** |

The player bootstrap combines this ID map with the CFS season-player list to establish canonical players and provider identities. Player-stat responses subsequently use those mappings to expose canonical identity.

The compatibility `/api/players` routes should not be treated as canonical player exposure because they query the legacy player model rather than the canonical player foundation.

**Gap:** there is no standalone `/api/v1/players` resource.

### E07 - CFS season players

**Endpoint:** `/cfs/afl/players?seasonId={season_provider_id}`

| Layer | Status |
|---|---|
| Collector | **Yes** |
| Pagination/completeness handling | **Yes** |
| Persistence | **Yes** |
| Main data | canonical players, provider IDs, competition-season membership and team associations |
| Manual trigger | **`--bootstrap-afl-season`, `--sync-afl-season`** |
| Scheduled trigger | **No dedicated player-bootstrap refresh** |
| Admin trigger | **No dedicated canonical player bootstrap** |
| Consumer API | **Indirect only** |

The supported first-run process bootstraps public metadata and then the CFS season-player population.

**Assessment:** persistence is mature but refresh remains operator-driven.

**Potential future decision:** determine whether canonical season-player membership needs a low-frequency scheduled refresh during list-management periods.

### E08 - CFS match player statistics

**Endpoint:** `/cfs/afl/playerStats/match/{match_provider_id}`

This is currently the most complete end-to-end CFS implementation.

| Layer | Status |
|---|---|
| Collector | **Yes - `MatchPlayerStatsCollector`** |
| Normalisation | **Yes** |
| Persistence | **Yes - `cfs_player_stats`** |
| One-match CLI | **Yes** |
| Whole-season CLI | **Yes - eligible/concluded match processing** |
| Scheduler | **Yes - operational polling/window workflow** |
| Admin | **Yes** |
| Consumer API | **Yes - canonical `/api/v1/.../player-stats`** |

Operational source policy selects CFS JSON and persists authoritative records to `cfs_player_stats`; the HTML scraper is not an automatic fallback and is not dual-written.

Scheduler support includes durable polling-series state and individual polling attempts with restart/recovery semantics.

Consumer access is provided through `GET /api/v1/matches/{match_id}/player-stats`, with canonical match/player/team resolution layered over the source statistics.

**Known source limitation:** verified CFS player-stat records do not independently expose a stable team provider ID. Home/away side context is combined with canonical match metadata to resolve consumer-facing team identity. A null source-side `team_provider_id` therefore does not prevent canonical API team resolution.

**Assessment:** primary authoritative match-stat path and the strongest complete source -> DB -> schedule -> API pipeline.

### E09 - CFS match rosters by round

**Endpoint:** `/cfs/afl/matchRosters/round/{round_provider_id}`

| Layer | Status |
|---|---|
| Collector | **Yes - `MatchRosterCollector`** |
| Persistence | **No - deliberately read-only** |
| CLI | **Yes - `--collect-match-rosters`** |
| Scheduler | **No CFS roster persistence** |
| Admin | No canonical CFS roster writer |
| Consumer API | **No** |

Canonical roster persistence is not currently implemented. The operational lineup writer remains the rendered HTML team-lineups source until CFS player IDs, positions, late changes and roster semantics are reconciled.

**Assessment:** collector implemented; persistence and consumer exposure remain deliberate gaps.

**Strong future candidate:** establish parity with the existing lineup source before deciding whether a canonical selection/roster model should replace it.

### E10 - CFS Stats Centre players

**Observed endpoint:** `/cfs/afl/statsCentre/players?...`

| Layer | Status |
|---|---|
| Discovered | **Yes** |
| Maintained endpoint contract | **No** |
| Collector | **No** |
| Persistence | No |
| Scheduler | No |
| Consumer API | No |

The endpoint remains low priority because its unique value over season players and match statistics has not been established.

**Assessment:** retain as research only.

## 4. Additional CFS endpoints discovered from AFL match-centre traffic

These are observed upstream endpoints but are not currently part of the maintained collection architecture.

### CFS `matchRoster/full/{matchProviderId}`

Observed authenticated JSON appears to include both team rosters plus supplementary match information such as status, umpires, weather, venue and recent team results.

| Collector | DB | Scheduler | Consumer API |
|---|---|---|---|
| **No** | **No** | **No** | **No** |

**Relationship to existing work:** potentially overlaps both `matchRosters/round` and public match metadata, but appears richer.

**Recommendation:** compare publication timing, late-change semantics and roster parity before considering implementation. Do not implement solely because the response is richer.

### CFS `matchItem/{matchProviderId}`

Observed content includes match identity, round information, home/away scoring, period-by-period scoring, period/match clock information, scoreworm/scoring events, weather and venue.

| Collector | DB | Scheduler | Consumer API |
|---|---|---|---|
| **No** | **No** | **No** | **No** |

Existing investigation captures have informed scheduler research, but CFS match-item fields are not currently used as scheduler planning inputs.

**Assessment:** probably the most interesting currently unimplemented endpoint because it may provide genuinely new canonical domains: period lifecycle, quarter state, period scores, clock state and scoring events.

**Recommendation:** investigate separately before implementation rather than mixing overlapping fields into existing match metadata.

### CFS `matchInterchange/{matchProviderId}`

Observed as authenticated match interchange history. Concluded and live responses may differ materially in richness and semantics.

| Collector | DB | Scheduler | Consumer API |
|---|---|---|---|
| **Production** (`afl_json/match_interchange.py`) | **Production** (`match_interchange_state` + `match_interchange_events` + `match_interchange_polls`, migration `0021`) | **Production** (`scheduler/match_interchange_production.py`, always-on, `AFL_INTERCHANGE_PRODUCTION_ENABLED`) | **Yes** -- `GET /api/v1/matches/{match_id}/interchanges` + `/interchanges/events` |

**Update (Issue #204):** promoted to a production-supported endpoint
contract from the Issue #193 diagnostic investigation (see
`docs/investigation/afl-json/ENDPOINT_CATALOG.md` §5 "Update (Issue #204)"
for the full detail). Production ingestion runs unconditionally via the
normal scheduler (`scheduler/scheduled_tasks.py`), independent of
`AFL_DIAGNOSTICS_ENABLED`/`AFL_DIAGNOSTIC_PROFILES` entirely, and persists
canonically-linked current per-player state plus meaningful transition
history. The initial promotion draft had only a single concluded-match
snapshot to review, materially weaker than the Issue #201 commentary
promotion's evidence basis; real Round 24 live diagnostic observations
across 7 matches were subsequently supplied and reviewed on PR #206,
confirming that array membership genuinely changes during LIVE play,
tightly correlated with each team's own `totalInterchangeCount`
incrementing (see `docs/architecture/api/interchange_api_design.md` §2.1
for the full evidence). The production contract exposes **`on_bench`**,
confirmed for LIVE play; POSTGAME/CONCLUDED behaviour and a full
individual-player round-trip citation from raw payloads remain unverified
-- see `docs/api_v1_interchange.md` for the exact caveat. Interchange state
remains non-authoritative for match finality, lifecycle, or player
statistics.

**Diagnostic evidence capture unchanged:** the `interchange` diagnostic
profile from Issue #193 (`collection/match_interchange_evidence.py`,
`scheduler/match_interchange_capture.py`,
`match_interchange_evidence_observations`, migration `0017`) is untouched
and keeps running independently, opt-in via `AFL_DIAGNOSTICS_ENABLED=true` +
`AFL_DIAGNOSTIC_PROFILES` including `interchange`. It remains useful for
parser-regression evidence and for gathering further evidence toward the
two residual open questions above (POSTGAME/CONCLUDED behaviour;
individual-player round-trip confirmation). It is not read by the
production collector, the scheduler, or `/api/v1`.

**Recommendation:** production-ready for consumer use, including the
confirmed `on_bench` semantic for LIVE play; revisit once POSTGAME/CONCLUDED
behaviour has been reviewed from further live evidence.

### CFS `commentaryFeed/{matchProviderId}`

Observed authenticated JSON contains match commentary linked to player/team events.

| Collector | DB | Scheduler | Consumer API |
|---|---|---|---|
| **Production** (`afl_json/match_commentary.py`) | **Production** (`match_commentary_events` + `match_commentary_polls`, migration `0019`) | **Production** (`scheduler/match_commentary_production.py`, always-on, `AFL_COMMENTARY_PRODUCTION_ENABLED`) | **Yes** -- `GET /api/v1/matches/{match_id}/commentary` |

**Update (Issue #201):** promoted to a production-supported endpoint
contract on real Round 24 evidence (see
`docs/investigation/afl-json/ENDPOINT_CATALOG.md` §5 "Update (Issue #201)"
for the full confirmed contract). Production ingestion runs unconditionally
via the normal scheduler (`scheduler/scheduled_tasks.py`), independent of
`AFL_DIAGNOSTICS_ENABLED`/`AFL_DIAGNOSTIC_PROFILES` entirely, and persists
canonically-linked, deduplicated events to `match_commentary_events`. The
consumer route returns a clean, chronological event stream -- never
diagnostic poll observations. Commentary remains non-authoritative for
match finality, lifecycle, or player statistics (see
`docs/architecture/api/commentary_api_design.md`).

**Diagnostic evidence capture unchanged:** the `commentary` diagnostic
profile from Issue #196 (`collection/match_commentary_evidence.py`,
`scheduler/match_commentary_capture.py`,
`commentary_evidence_polls`/`commentary_evidence_events`, migration `0018`)
is untouched and keeps running independently, opt-in via
`AFL_DIAGNOSTICS_ENABLED=true` + `AFL_DIAGNOSTIC_PROFILES` including
`commentary`. It remains useful for parser-regression evidence and replay
investigation, but is not read by the production collector, the scheduler,
or `/api/v1`.

**Recommendation:** production-ready for consumer use; see the API design
doc for filtering/ordering/identity-resolution semantics and known
limitations (event-identity heuristics, `lastUpdated` not being a reliable
change signal, no additional structured scoring fields beyond
`scoreEvent`).

## 5. HTML/rendered source implementation matrix

Structured JSON has replaced a number of historical HTML acquisition paths, but several rendered sources remain deliberately supported.

### AFL fixture/match HTML

| Collector | Persistence | Schedule | API impact |
|---|---|---|---|
| **Legacy/manual** | Yes, legacy/shared metadata tables | No normal production metadata routing | Potentially visible where consumers read persisted match/round data |

Current Scheduler/Admin metadata refresh uses public AFL JSON instead. HTML fixture/match collectors remain explicit diagnostic/compatibility paths.

**Recommendation:** retain until retirement/parity work is deliberately completed.

### AFL team-lineups page

**Page:** `https://www.afl.com.au/matches/team-lineups`

| Collector | Persistence | Manual | Scheduled | Consumer API |
|---|---|---|---|---|
| **Operational HTML** | **Yes - `lineups`** | Yes | **Yes** | **Yes, compatibility API** |

CFS rosters are not silently substituted for this writer. There is presently no equivalent `/api/v1` lineup/selection resource.

**Assessment:** operational and consumer-visible, but still HTML-dependent.

### AFL injury-list page

**Page:** `https://www.afl.com.au/matches/injury-list`

| Collector | Persistence | Manual | Scheduled | Consumer API |
|---|---|---|---|---|
| **Operational HTML** | **Yes - injury history/current state** | Yes | **Yes** | **Yes, compatibility API** |

HTML remains the operational source because no maintained structured injury endpoint has been established. No `/api/v1/injuries` resource currently exists.

**Assessment:** complete operational pipeline with browser-dependent acquisition.

### AFL match-centre HTML player statistics

**Page:** `/afl/matches/{id}#player-stats`

| Collector | Persistence | Manual | Scheduled | Consumer API |
|---|---|---|---|---|
| **Legacy/manual** | **Yes - legacy `player_stats` only** | Yes | **No** | **Yes - legacy `/api/player-stats` only** |

This path remains deliberately isolated from authoritative CFS statistics and does not supply the v1 match player-stat route.

**Assessment:** cleanly separated legacy path; candidate for eventual retirement once historical/support requirements are settled.

### AFL Stats Leaders - player identity scrape

| Collector | Persistence | Schedule | Consumer API |
|---|---|---|---|
| Existing HTML scraper | Legacy/enrichment path | Legacy refresh workflow | No canonical direct API |

Canonical bootstrap now has stronger structured sources through CFS season players and the public player ID map.

**Recommendation:** candidate for retirement or restriction to enrichment/diagnostics.

### AFL Stats Leaders - totals/averages export

| Collector | Persistence | Schedule | Consumer API |
|---|---|---|---|
| Manual HTML export | CSV artifact only | No | No |

**Assessment:** utility/export rather than application persistence.

### AFL club squad pages

| Collector | Persistence | Schedule | Consumer API |
|---|---|---|---|
| Existing HTML scraper | Legacy player/enrichment workflow | Not normal canonical schedule | Legacy relationships only |

These pages are no longer the preferred source for canonical player membership because structured CFS/public sources now exist.

**Assessment:** enrichment/legacy role rather than canonical authority.

## 6. Consumer API summary by persisted domain

| Data domain | Persisted source | `/api/v1` | Compatibility `/api` | Notes |
|---|---|---|---|---|
| Seasons | Public AFL JSON | **Yes** | No equivalent canonical route | v1 authoritative |
| Rounds | Public AFL JSON | **Yes** | **Yes** | v1 preferred |
| Teams | Public AFL JSON | **Indirect** | No dedicated team resource | Embedded in match/bye projections |
| Matches | Public AFL JSON | **Yes** | **Yes** | v1 preferred |
| Match lifecycle/status | Public match detail + metadata | **Yes** | **Yes** | Via match objects |
| Canonical players | CFS season players + public ID map | **Indirect** | **No canonical route** | No `/api/v1/players` yet |
| CFS match stats | CFS `playerStats` | **Yes** | **No** | `/api/v1/matches/{id}/player-stats` |
| Legacy HTML player stats | Match-centre HTML | **No** | **Yes** | `/api/player-stats` |
| Lineups/selections | AFL rendered HTML | **No** | **Yes** | CFS roster collector does not persist |
| Injuries | AFL rendered HTML | **No** | **Yes** | No structured source implemented |
| CFS round rosters | CFS `matchRosters/round` | **No** | **No** | Read-only |
| `matchRoster/full` | None | No | No | Investigation only |
| `matchItem` periods/events | None | No | No | Investigation only |
| Interchange state | `match_interchange_state`/`match_interchange_events`/`match_interchange_polls` (migration `0021`) | **Yes** | **Yes** | `/api/v1/matches/{id}/interchanges` + `/interchanges/events` (Issue #204, promoted from Issue #193); array-membership semantic still unconfirmed by live evidence, see `docs/api_v1_interchange.md`; diagnostic evidence table also still maintained separately |
| Commentary | `match_commentary_events` (migration `0019`) | **Yes** | **Yes** | `/api/v1/matches/{id}/commentary` (Issue #201); diagnostic evidence table also still maintained separately |
| Stats Centre players | None | No | No | Investigation only |
| Leader totals/averages | CSV artifact | No | No | Manual export |

The v1 implementation is intentionally narrower than the complete internal source inventory. This is useful separation: collector capability should not automatically expand the public contract.

## 7. Persistence and scheduling summary

There are four materially different persistence models.

### A. Canonical metadata

**Sources:** public AFL competition/season/round/team/match endpoints.

**Persisted:** yes.

**Acquisition paths:** manual season bootstrap, whole-season sync, and Scheduler/Admin operational metadata refresh.

**Consumer-visible:** strongly yes through `/api/v1`.

### B. Canonical player population

**Sources:** CFS season players plus public AFL player ID map.

**Persisted:** yes.

**Acquisition paths:** manual bootstrap and whole-season sync.

**Scheduled:** no dedicated player-membership refresh.

**Consumer-visible:** indirectly through player-stat identity.

This is the largest example of persisted canonical data that is not independently consumer-accessible.

### C. Authoritative match statistics

**Source:** CFS match player statistics, supplemented by public match-detail lifecycle reconciliation.

**Persisted:** `cfs_player_stats`.

**Acquisition paths:** one-match CLI, whole-season synchronization, Scheduler match polling and Admin operational triggers.

**Consumer-visible:** directly through `/api/v1`.

This is currently the strongest complete source -> DB -> schedule -> API pipeline.

### D. Supported HTML operational data

**Sources:** injuries and lineups.

**Persisted:** yes.

**Scheduled:** yes.

**Consumer-visible:** yes through compatibility routes.

These should not be mistaken for deprecated scrapers merely because they use HTML: current source policy deliberately selects them because a proven structured replacement is not yet available.

## 8. Main findings

### Finding 1 - Most foundational structured AFL sources are already implemented

Competition, season, round, team, match, player crosswalk, season-player population and match statistics all have maintained collectors. The largest gaps are no longer fundamental collection infrastructure.

### Finding 2 - CFS match statistics are considerably further developed than CFS rosters

`playerStats/match` has:

**collector -> normalisation -> persistence -> lifecycle reconciliation -> Scheduler -> Admin -> CLI -> season sync -> `/api/v1`**

whereas `matchRosters/round` currently stops at:

**collector -> normalisation -> manual inspection**

This distinction should remain explicit.

### Finding 3 - `matchItem` is probably the most valuable unimplemented discovered endpoint

Unlike Stats Centre or commentary, it appears to provide genuinely new canonical domains: periods, quarter state, match clock, period scoring and scoring events. Those are not presently represented by the consumer API.

It therefore deserves investigation, but not automatic implementation.

### Finding 4 - `matchRoster/full` deserves comparison with the existing roster collector

The full match-specific response appears richer than the current round roster endpoint and may supply useful supplementary match information.

Before implementation, establish whether it represents the same selection population as `matchRosters/round`, publication timing, late-change semantics, and whether overlapping match metadata adds any canonical value beyond the public API.

### Finding 5 - Canonical players need a consumer-surface decision

Canonical player identity and season membership exist in the database, but there is no canonical standalone player resource such as `/api/v1/players` or `/api/v1/seasons/{season_id}/players`.

The compatibility `/api/players` routes do not solve this because they use the legacy player model.

This is primarily a future consumer API design question rather than a collector problem.

### Finding 6 - Injuries and lineups remain deliberately outside v1

Both are collected, persisted and consumer-accessible, but only through compatibility routes.

A future design decision should determine whether v1 encompasses these domains or remains focused on canonical AFL match/stat data. Implementation should follow that contract decision rather than lead it.

### Finding 7 - Several discovered CFS sources are research evidence only

`matchItem`, `matchRoster/full` and Stats Centre player queries should not
be described as supported AFL-api endpoints. They are known upstream
endpoints, not maintained collector capabilities.

`commentaryFeed` and `matchInterchange` are the exceptions, as of Issues
#201 and #204 respectively: both have been promoted to production-supported
endpoint contracts (production collector, persistence and a consumer
route) -- see §4's "CFS `commentaryFeed/{matchProviderId}`" and
"CFS `matchInterchange/{matchProviderId}`" entries above.
`matchInterchange`'s promotion initially had a materially weaker evidence
basis than commentary's, before real Round 24 live diagnostic observations
were supplied and reviewed on PR #206, confirming the array-membership
semantic (`on_bench`) for LIVE play -- see `docs/api_v1_interchange.md` for
the confirmed semantic and its two residual caveats (POSTGAME/CONCLUDED
behaviour; individual-player round-trip confirmation from raw payloads).
Both endpoints' diagnostic evidence-capture
profiles (Issues #196 and #193) remain separately available for
debugging/replay, but are no longer the only pathway for either.

## 9. Recommended priorities

### Priority A - Maintain current production paths

Continue validating public metadata collection, player bootstrap, CFS player-stat polling/finality, injury collection and lineup collection. No broad collector rewrite is indicated by this audit.

### Priority B - Complete the roster decision

The next useful structured-source investigation is the relationship among HTML team lineups, CFS `matchRosters/round`, and CFS `matchRoster/full`.

The goal should not simply be to replace HTML. It should establish whether CFS data can faithfully model the product concept currently represented as a lineup/selection.

### Priority C - Investigate `matchItem` as a new domain

Treat period/clock/scoring-event data as a possible new feature rather than an alternate match-metadata source.

A dedicated investigation should determine lifecycle behaviour, polling/update frequency, period identifiers, quarter completion rules, score-event ordering/key stability, post-match corrections and whether clock data is authoritative enough to persist.

### Priority D - Decide canonical player API exposure

Before adding more upstream data, consider whether the already-persisted canonical player foundation should gain a canonical v1 consumer resource. This may provide more immediate consumer API value than commentary or interchange collection.

## 10. Documentation placement and maintenance

This document belongs in `docs/investigation/afl-json/` rather than `docs/architecture`.

The distinction is intentional:

- `ENDPOINT_CATALOG.md` records upstream discovery and contracts;
- this document records implementation status and gaps; and
- architecture documents should describe decisions that have become part of the supported system rather than every observed AFL endpoint.

This report should be reviewed whenever one of these changes materially:

- `afl_json/contracts.py`;
- operational source policy;
- persistence/schema for AFL source data;
- Scheduler source routing;
- CLI collection operations;
- `/api/v1` routes; or
- an upstream endpoint graduates from investigation to maintained collection.

## 11. Current system summary

At `b977c83`, AFL-api has moved substantially from an HTML-scraper-oriented system to a structured, source-separated architecture.

The principal production pipeline is:

**Public AFL JSON metadata -> canonical database hierarchy**

plus:

**CFS season players + public player ID map -> canonical player population**

plus:

**CFS match player stats + public status reconciliation -> `cfs_player_stats` -> Scheduler/CLI/Admin -> `/api/v1`**

while:

**HTML injuries and lineups remain intentional operational sources**

and:

**`matchItem`, `matchRoster/full` and Stats Centre remain investigation-only sources; commentary is production-supported as of Issue #201, and interchange is production-supported as of Issue #204 (see §4).**

Maintaining those distinctions allows future collection work to be driven by a clear consumer API or operational requirement rather than by endpoint availability alone.
