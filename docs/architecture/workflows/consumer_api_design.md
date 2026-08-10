# Consumer API Workflow Design

**Status:** Draft for implementation review

**Related implementation design:** [Canonical CFS player-stat read API design](../api/player_stats_api_design.md)

**Related contracts:** [AFL data authority and identity map](../data_authority_map.md),
[Player-stat persistence and authority contract](../player_stats_storage_contract.md),
[Scheduler workflow design](scheduler_workflow_design.md), and
[Season synchronisation workflow design](season_sync_design.md)

**Intended milestone:** A coherent, consumer-focused `/api/v1` read surface

## 1. Background

AFL-api already collects and persists fixtures, rounds, players, injuries,
lineups, and authoritative match player statistics. It also has an
unversioned `/api/...` surface and one implemented versioned endpoint:
`GET /api/v1/matches/{match_id}/player-stats`.

The existing player-stat API design was deliberately narrow. It specified how
to expose one authoritative table safely, with explicit identities and
finality. It did not establish the human-led architecture for the whole
consumer API. In particular, its assumption that unversioned endpoints would
remain permanently alongside `/api/v1` no longer reflects the intended
product direction.

This document is the authoritative human-designed target for the broader
consumer API. It records what consumers should be able to rely on, why those
guarantees matter, how the API relates to collection and persistence, which
legacy capabilities must be accounted for, and how the surface should grow.
It is not an endpoint-by-endpoint implementation specification and does not
claim that all target behaviour is already shipped.

The central principle is:

> The consumer API promises stable AFL data semantics and consumer-visible
> behaviour. It does not promise how collectors, providers, database tables,
> or scheduler processes produce that result.

BBBFL is the first motivating consumer, but the API is not a BBBFL scoring
service. It should remain useful to an individual, spreadsheet, hobby project,
or other application that needs dependable AFL data without understanding the
collection system beneath it.

## 2. Goals

The consumer API should:

1. provide a stable, source-independent interface to useful AFL data;
2. let a consumer navigate from a season to rounds, matches, players, rosters,
   injuries, and statistics without obtaining identifiers elsewhere;
3. expose canonical identities while retaining useful provider crosswalks;
4. make availability, lifecycle, freshness, and statistical finality explicit;
5. expose authoritative persisted facts as soon as they are available;
6. keep ordinary responses concise, predictable, and understandable;
7. offer permission-controlled advanced provenance without coupling consumers
   to raw storage or provider payloads;
8. preserve meaningful roster and injury history even when the first API
   release exposes only the latest state;
9. support incremental delivery under one coherent `/api/v1` contract; and
10. retain enough observability to guide later performance and product work.

## 3. Non-goals

Version 1 does not:

- calculate BBBFL or other fantasy-league scores;
- interpret AFL facts into fantasy concepts such as `late_withdrawal`, form,
  eligibility, or player value;
- expose write operations, collection triggers, corrections, or administrative
  mutations through the consumer surface;
- expose raw provider JSON, database rows, table names, SQL structure, or
  internal collector payloads as an API contract;
- offer arbitrary resource expansion such as `include=matches,rosters,stats`;
- add arbitrary multi-player batch requests without a demonstrated need;
- provide cursor pagination, user-selectable sorting, CSV output, application-
  level caching, conditional requests, or service-level objectives;
- promise a full history of every live player-stat observation;
- persist independently maintained season-stat aggregates;
- infer missing participants or manufacture zero-stat rows;
- introduce multiple competition support; or
- expose the legacy diagnostic header-echo route in `/api/v1`.

These boundaries keep the first contract focused on correctness and useful
data. They do not prevent separately designed future capabilities.

## 4. Architectural position

The consumer API is a read-only presentation layer over authoritative
persisted state.

```text
Authenticated consumer
          |
          v
  FastAPI /api/v1 routes
          |
          v
Consumer response models and query services
          |
          v
Canonical persistence and shared authority/finality rules
          ^
          |
Collectors, source policy, scheduler, and season sync
```

The API is responsible for:

- authentication and capability checks;
- validating consumer requests;
- resolving canonical and provider identities through validated crosswalks;
- presenting authoritative state through stable response models;
- calculating deterministic generic aggregates from authoritative rows;
- applying consistent empty, null, error, ordering, and pagination semantics;
- exposing factual lifecycle and freshness information; and
- documenting the shipped contract through OpenAPI and consumer references.

The API must not:

- fetch from AFL or Champion Data on demand;
- select or promote source authority independently;
- recreate scheduler, collector, finality, or persistence decisions;
- replace valid persisted data because a newer collection attempt failed;
- infer source facts that were not observed; or
- make database schema the consumer schema.

The database remains the hand-off point between collection and consumption.
Once a valid authoritative observation is persisted, a read should be able to
return it immediately. The actual delay before persistence remains a scheduler
and collector concern rather than an API freshness promise.

## 5. Contract boundary

### 5.1 Stable consumer contract

Within a supported API version, consumers may rely on documented:

- endpoint paths and HTTP methods;
- request parameters and authentication behaviour;
- response field names, types, meanings, and units;
- canonical and provider identifier semantics;
- AFL-domain terminology;
- null-versus-zero behaviour;
- lifecycle, availability, freshness, and finality semantics;
- error shapes and machine-readable codes;
- deterministic collection ordering;
- pagination behaviour where it applies; and
- aggregate definitions.

### 5.2 Implementation detail

Consumers may not rely on:

- table, column, index, or migration names;
- SQL or database access patterns;
- collector names or module structure;
- scheduler cadence or planning state;
- provider endpoint URLs or raw payload shapes;
- which collector produced a canonical result;
- internal storage timestamps; or
- the representation of audit and operational evidence.

Selected provenance can be exposed through advanced metadata without turning
those internal structures into the normal contract.

## 6. Scope and resource model

Version 1 initially targets the AFL men's competition. The design should avoid
unnecessary barriers to another competition later, but v1 does not add a
competition selector or cross-competition identity rules without a real
consumer requirement.

The foundational navigation model is:

```text
season
  |-- rounds (including byes where known)
  |     `-- matches
  |            |-- match details
  |            |-- latest published rosters
  |            `-- participating-player statistics
  |-- season player lists
  `-- season player-stat summaries/details

players
  |-- identity and current profile
  |-- provider-ID crosswalks
  |-- season memberships
  |-- match/round/season statistics
  `-- current and historical injury reports

teams
  |-- lightweight identity/profile
  |-- season lists
  `-- matches and rosters
```

Venues are supporting fixture data and may become a lightweight resource when
that improves reuse. Consumer-useful fields include identity, name, location,
state, and timezone. Ticket links, social tags, land ownership, stadium-app
links, and other source fields do not enter the stable contract merely because
the upstream payload contains them.

The API should support both canonical resource navigation and a small number
of practical convenience views. Convenience views must read the same canonical
data and must not implement parallel business rules. Version 1 favours focused,
explicit routes over large expanded responses.

## 7. Identity and historical context

### 7.1 Canonical identity

A canonical player identifier represents the person and remains stable across
seasons and team changes. Canonical identifiers are the preferred keys for new
consumer workflows.

Known provider identifiers, including AFL and Champion Data identifiers, remain
visible as typed crosswalks. They allow consumers to correlate AFL-api with
other known datasets, but no identifier is parsed, guessed, or synthesised
across namespaces. An unresolved mapping is `null`.

Player search is an identity-discovery aid, not an identity authority. A simple
v1 search may use case-insensitive partial names and return enough context to
choose a canonical player. It does not guess which same-named person a consumer
intended. Deterministic lookup by a known provider ID is a complementary target.

### 7.2 Team context over time

A player's current profile may identify a current team. Historical match and
season records must retain the team context that applied to that record. A club
change must not rewrite the apparent team for an earlier match.

The API therefore keeps `team_id` on a player-stat record even where match side
could be used to derive it. The redundant context makes the consumer record
self-contained and historically correct.

### 7.3 Season list, selected roster, and participant

These are distinct facts:

- a **season player list** contains officially listed players, including those
  who never play a match;
- a **match roster** represents the latest published team selection and named
  position/status; and
- a **match player-stat result** contains actual participants for whom the
  authoritative source published a stat row.

Stat endpoints omit non-participants. They do not create synthetic rows with
zeros or nulls for every listed or selected player.

## 8. AFL facts, history, and interpretation

AFL-api normalises AFL facts; it does not reinterpret them.

- Use AFL terminology while applying a consistent technical naming convention.
- Count a game played when authoritative AFL match data contains that player's
  performance, even if participation was very limited.
- Preserve the published roster position/status and observations needed to
  recognise later changes, but let BBBFL decide whether those facts constitute
  a late withdrawal.
- Preserve meaningful injury observations and when AFL reported them.
- When an injury disappears from the published list, report the factual current
  state as `not_listed`; do not infer `recovered`.
- When a live source publishes only some participants, return those valid
  participants. Do not guess who is missing or label the response incomplete
  unless the authority model actually establishes that fact.

The normal roster and injury views should present the latest known state.
Persistence must retain enough meaningful observation history to support later
history/change endpoints. That is a preserve-now, expose-deliberately strategy.

Player statistics follow a different rule: the normal contract represents the
latest authoritative fact, not a history of every value observed during live
polling. If an official value is corrected from 11 to 12, consumers receive 12.
Quarter snapshots or time-series stat history are possible future products, not
a v1 promise.

## 9. Response conventions

### 9.1 Resource-shaped JSON

JSON is the only v1 response format. Successful responses are resource-shaped,
not forced under a universal `data` wrapper. A match player-stat response can
naturally contain `match`, `lifecycle`, `players`, and `metadata`; a player
response can contain `player` and `metadata`.

Common conventions still apply across every shape:

- singular resources and plural collections use consistent names;
- `metadata` contains resource-level consumer metadata where useful;
- `pagination` appears only on paginated collections;
- timestamps and identifiers follow the common rules in this document;
- each collection has a documented deterministic order; and
- exact Pydantic response models prevent accidental `SELECT *` contracts.

### 9.2 Null, zero, missing resources, and empty results

`0` means a known stat existed and the participant recorded none of it.
`null` means unknown, unavailable, uncollected, historically inapplicable, or
not supplied. These meanings are never interchangeable.

Expected stable fields remain present as `null` when their values are not yet
available. This lets consumers distinguish unavailable data from a field that
is not part of the contract.

The general result rule is:

- a specifically addressed resource that does not exist returns `404`;
- a real resource with no currently applicable data returns `200` with an
  empty collection or factual empty state; and
- a valid collection query with no matches returns `200` with an empty
  collection.

For example, a nonexistent match is `404 match_not_found`; an upcoming match
with no player statistics is `200` with `players: []` and lifecycle metadata.
A real player who is absent from the current injury list has the factual state
`not_listed`, rather than a player-not-found error.

### 9.3 Validation and errors

Contradictory or invalid supplied parameters return a structured `400`, such as
`conflicting_parameters`. The API should not attempt excessive inference, but
it should reject contradictions it can establish reliably. Framework-level
request-shape validation may use `422` where FastAPI applies it consistently.

All application errors use one safe, predictable shape:

```json
{
  "error": {
    "code": "advanced_access_required",
    "message": "This API key does not permit access to advanced metadata."
  }
}
```

The HTTP status provides the broad category; the stable code provides the
machine reason; the message helps a human debug. Errors never expose SQL,
tables, stack traces, secrets, provider URLs, or raw collector failures.

### 9.4 Ordering and pagination

Every collection publishes a fixed deterministic default ordering. Version 1
does not provide arbitrary sort parameters.

Naturally bounded resources such as one match, one round, or a season summary
normally return complete results. Potentially large detail/history collections
may use simple `limit`/`offset` pagination, with documented defaults and maxima
chosen from actual row volumes. Cursor pagination is deferred.

Season statistics should distinguish a useful per-player summary from detailed
player-match rows. The summary may contain `totals` and `summary` objects on
each player; detailed history is an explicit view rather than an unpredictable
shape change.

## 10. Lifecycle, freshness, and finality

### 10.1 Report facts, not a stale judgement

Normal responses expose one resource-level `source_updated_at`: the newest
authoritative source observation used to produce that response. It is not the
time the API served the request.

The API does not return a generic `stale: true`. Different consumers can make
different decisions from lifecycle and `source_updated_at`. Advanced metadata
may additionally expose collection and persistence timestamps when useful.

### 10.2 Match lifecycle and statistic finality

Match status and player-stat finality are related but not identical. A match
being `CONCLUDED` does not prove that the persisted stat rows came from an
authoritative concluded observation.

The API must use the existing shared scheduler/storage finality rule. The
scheduler continues post-match collection until an authoritative concluded
snapshot exists, then backs off or raises a reconciliation finding as designed.
The API presents the result of that authority decision; it does not count scans
or invent a second definition of final.

Normal consumers need understandable facts such as:

```json
{
  "match": {"status": "CONCLUDED"},
  "lifecycle": {"finality": "final"},
  "metadata": {"source_updated_at": "2026-08-09T09:25:06Z"}
}
```

Collector-level evidence such as numeric snapshot authority, row-level
resolved status, source identity, and persistence timing belongs in advanced
metadata rather than ordinary player rows.

### 10.3 Protect valid persisted data

A failed, malformed, lower-authority, or invalid new observation must never
degrade previously valid authoritative data. The API continues to serve the
best valid persisted state with its honest older `source_updated_at` while
collector health and failure details remain operational concerns.

## 11. Advanced metadata

Consumers request advanced metadata with:

```text
?advanced=true
```

Advanced mode is additive. It must never change the values or meaning of the
normal response; it only adds selected provenance and diagnostic context.

Suitable advanced fields can include:

- provider/source identifiers;
- collector or source-policy identity;
- authority/finality evidence;
- source observation, collection, and persistence timestamps; and
- selected experimental source fields awaiting possible promotion.

Advanced mode is not `SELECT *`, a database-row dump, or access to raw provider
JSON. Raw JSON remains internal forensic evidence for debugging and future
reprocessing. Database schema is not API schema even for advanced consumers.

A credential without advanced capability requesting `advanced=true` receives
`403 advanced_access_required`. The API does not silently ignore the request.

New upstream fields may appear first as selected advanced metadata. A field is
promoted into the stable normal contract only after its meaning, history,
availability, and consumer value are understood.

## 12. Authentication, access, and administration

All `/api/v1` endpoints, including the lightweight API root, require an API key.
Generated OpenAPI/Swagger documentation should be available to normal
authenticated consumers while excluding internal/admin endpoints and secrets.

API keys represent client identities and capabilities, not end-user accounts
or API-version-specific credentials. A practical initial capability model is:

- standard read; and
- advanced read.

The model should not hard-code an assumption that only two capabilities can
ever exist. Consumer credentials never grant write or collection authority.

Credential records should support a human-readable client label, enabled or
revoked state, replacement/rotation, capabilities, and an optional per-key rate
limit. Rate limiting is modelled from the beginning but disabled by default
unless configured. The secret key itself must not appear in ordinary logs.

Missing or invalid credentials return `401`; valid credentials without the
requested capability return `403`. Deployed or network-accessible environments
accept keys only over HTTPS, and keys are sent in a header rather than a query
parameter. Localhost development may use HTTP.

The CLI can be the primary v1 management path. The existing Admin key-
management page should eventually support the same capability model, but its UI
redesign does not block the consumer API.

## 13. Time semantics

UTC is the canonical machine time representation throughout AFL-api. Timestamps
use unambiguous ISO 8601 values with an offset or `Z`.

Fixture/match resources may additionally expose the AFL-published venue-local
start time and the venue's IANA timezone. This lets a consumer reproduce the
advertised local time, including daylight-saving behaviour. Player-stat
responses should not repeat venue detail that belongs to the match resource.

Every timestamp name and field description must state what event it represents.
In particular, `source_updated_at`, `collected_at`, and `persisted_at` must not
be presented as interchangeable.

## 14. Aggregates and calculations

The API may calculate generic, deterministic AFL aggregates such as games
played, totals, per-game averages, minima, maxima, or best games when those are
useful across consumers and documented precisely.

Aggregates are calculated on demand from current authoritative match-stat rows.
They are not persisted as an independent source of truth. An official match
correction therefore flows naturally into season totals and summaries.

Fantasy scoring, subjective form measures, opponent-adjusted analytics,
consumer-specific rolling windows, and other application logic remain outside
the core consumer API.

## 15. Versioning and compatibility

`/api/v1` is the first published consumer contract. The existing unversioned
`/api/...` routes are pre-v1 legacy behaviour, not a permanently supported
parallel API. They may be retired once their useful capabilities have been
accounted for under v1. No backward-compatibility promise is made for those
routes.

Within v1:

- additive optional fields, filters, and new resources may be introduced;
- existing field names, types, meanings, requiredness, defaults, and identifier
  semantics do not change incompatibly; and
- a breaking contract change requires a new version.

The opening part of each AFL season is an operational observation period for
upstream field and shape changes. Useful additive fields may be incubated in
advanced metadata and promoted into v1 when safe. A breaking upstream change
requires a new API version where possible.

When a replacement version is released, the superseded version should remain
available through at least the remainder of the current AFL season where
technically practical. A precise minimum deprecation duration should be chosen
before the first version retirement. AFL-api must not manufacture inaccurate
data when an upstream change makes the old contract impossible to satisfy.

## 16. Legacy capability inventory and migration checklist

The current unversioned surface is an inventory of capabilities to assess, not
a response-shape compatibility requirement.

| Legacy route | Consumer capability | v1 disposition |
| --- | --- | --- |
| `GET /api/players` | List player profiles | Replace with canonical players and season-list resources. |
| `GET /api/players/{afl_id}` | Address a player by AFL ID | Replace with canonical player lookup plus provider-ID resolution. |
| `GET /api/players/club/{club_slug}` | List current players by club | Replace with team/season-list navigation or a canonical team filter. |
| `GET /api/injuries` | Current or historical injuries for all players | Replace with canonical latest and history views while preserving AFL observations. |
| `GET /api/injuries/{afl_id}` | Current or historical injuries for one player | Replace with canonical player injury views; an empty current state is not a missing player. |
| `GET /api/lineups/latest/{afl_id}` | Latest lineup entry for a player | Replace through canonical latest-roster/player convenience access when persistence supports it. |
| `GET /api/lineups/{round_number}` | Round lineups | Replace with canonical round/match roster resources. |
| `GET /api/lineups/{round_number}/{afl_id}` | Player lineup entry for a round | Replace with a canonical player-within-roster view if consumer demand justifies it. |
| `GET /api/rounds` | List round metadata | Replace with versioned season/round navigation, including bye information where known. |
| `GET /api/rounds/{round_id}` | Round details | Replace with a canonical versioned round resource. |
| `GET /api/matches` | List matches, optionally by round | Replace with versioned fixture/match navigation and stable response models. |
| `GET /api/matches/{match_id}` | Match details | Replace with a canonical versioned match resource. |
| `GET /api/player-stats` | Legacy HTML-backed stats filtered by match, round, or AFL player ID | Supersede with authoritative CFS-backed match, round, season, and player access. Do not repoint the old response in place. |
| `GET /api/echo-headers` | Diagnostic redacted-header echo | Intentionally retire from the consumer API; retain diagnostics only in an admin/developer surface if still useful. |

Before a legacy route is removed, its row in this checklist must have an
implemented replacement, an explicitly accepted deferral, or an explicit
retirement decision. The migration may reuse good implementation ideas, but it
does not preserve accidental `SELECT *` fields, legacy identifiers, or old
error behaviour.

## 17. Incremental v1 roadmap

Version 1 is delivered incrementally. The existing match player-stat endpoint
is the reference implementation and baseline, not the authority for the entire
architecture.

### Stage 1 — Reconcile the player-stat baseline

- retain canonical/provider identity, team context, explicit finality, stable
  Pydantic response models, and the shared finality predicate;
- add the common structured error contract and resource-level
  `source_updated_at`;
- move snapshot authority, row-level resolved status, source, and collector/
  persistence timestamps behind permission-controlled `advanced=true`; and
- align normal lifecycle vocabulary and empty-state behaviour with this
  document.

### Stage 2 — Foundational navigation

- seasons;
- rounds, including known bye context;
- fixtures and match details;
- lightweight API root and authenticated generated documentation; and
- complete in-API navigation from season to match-stat resource.

### Stage 3 — Player identity and season lists

- canonical player resources;
- known provider-ID crosswalk resolution;
- simple human-friendly name discovery; and
- team/season player membership distinct from match participation.

### Stage 4 — Match rosters

- latest published roster per match;
- named position/status and resource freshness; and
- persistence of meaningful observation history before a history endpoint is
  promised.

### Stage 5 — Injuries

- canonical current injury state;
- factual `not_listed` semantics; and
- injury observation history.

### Stage 6 — Broader statistics and convenience views

- player, round, and season access;
- per-player season totals and generic summaries calculated on demand;
- explicit detailed player-match history; and
- team filters or other convenience views where actual consumer use justifies
  them.

Low-priority team/venue resources, exports, caching, conditional polling,
additional competitions, and richer analytics follow only from demonstrated
need. Implementation order may bring forward a capability that is already
nearly complete, provided its contract follows this document.

## 18. Documentation and discoverability

The consumer API has three documentation layers:

1. this architecture document explains intent, principles, and system
   boundaries;
2. endpoint implementation designs describe how a bounded resource is built;
   and
3. consumer references plus generated OpenAPI describe exactly how to call the
   shipped API.

`GET /api/v1` should be an authenticated, lightweight discovery response with
the API name, version, and documentation location. It must not expose
operational or internal state.

Every shipped route requires a typed response model, documented parameters,
field descriptions, examples, status/error responses, and links from the
consumer documentation. A field glossary/data dictionary should record stable
name, meaning, unit, nullability, and whether a field is normal or advanced.

The broad consumer workflow belongs in `docs/architecture/workflows/`. The
endpoint-specific player-stat implementation design belongs in
`docs/architecture/api/`. This preserves the workflow folder as the home of
human-authored cross-component targets while retaining the useful technical
design as a subordinate implementation record.

## 19. Observability and evidence-based optimisation

Version 1 instruments before it optimises. Internal metrics should distinguish,
at minimum:

- client identity without logging the secret;
- endpoint and response status;
- request duration;
- response size where practical; and
- rate-limit outcomes when configured.

Collection freshness comes from resource metadata; API and collector health
remain operational concerns. Ordinary consumers do not receive internal
metrics or a collector-failure feed.

No application cache or conditional-request mechanism is required initially.
If measurements later show that historical aggregates or polling traffic are
expensive, caching, `ETag`, or `Last-Modified` can receive a separate design
based on evidence.

## 20. Testing principles

Each resource implementation needs deterministic offline tests covering:

- exact stable response keys and types;
- canonical/provider identity resolution and null crosswalks;
- null versus zero behaviour;
- deterministic ordering and pagination boundaries;
- missing resources versus valid empty states;
- contradictory and invalid parameters;
- structured `400`, `401`, `403`, and `404` errors;
- ordinary versus advanced field separation;
- advanced capability enforcement;
- lifecycle/finality transitions using shared authority logic;
- resource-level freshness timestamps;
- partial live observations without invented participants;
- preservation of last valid authoritative data; and
- aggregate recalculation after corrected authoritative match rows.

Tests must use isolated databases and no live network. OpenAPI and consumer
reference examples should be checked against the same response models.

## 21. Reconciliation with the player-stat implementation design

The relocated `api/player_stats_api_design.md` remains useful as the technical
record for the implemented Stage 1 route. The following parts align with and
should be retained:

- authoritative `cfs_player_stats` reads;
- explicit canonical, AFL, and Champion Data identity;
- contextual team identity;
- exact response models instead of database-row dumping;
- shared match-stat finality logic;
- a bounded match resource with no pagination; and
- no raw provider JSON exposure.

The following broader assumptions are superseded by this document and require
follow-up changes rather than silent reinterpretation:

- unversioned routes are not permanently supported compatibility routes;
- v1 authentication evolves to client capabilities, including advanced read;
- snapshot authority, row-level resolved status, and collection timestamps
  move out of the normal response into advanced metadata;
- normal freshness is one resource-level `source_updated_at`;
- advanced metadata never exposes raw JSON or automatic database columns; and
- the complete v1 roadmap begins with season/round/match navigation after the
  player-stat baseline, then identities, rosters, injuries, and broader views.

Until the relevant implementation follow-up lands, the shipped consumer
reference remains the accurate description of the current endpoint. This
architecture governs future changes; it does not pretend those changes are
already deployed.

## 22. Implementation questions

Implementation Issues should resolve these details against current code without
weakening the agreed contract:

1. What common response/error models should be introduced first, and where
   should they live without creating a framework-heavy abstraction?
2. Which persisted timestamp is currently the best factual
   `source_updated_at` for each resource, and where must source observation time
   be added explicitly?
3. How should existing API-key storage evolve to capabilities, revocation, and
   optional per-key limits while preserving current credentials safely?
4. Which roster and injury observation history is already retained, and what
   minimal persistence change is needed before promising history?
5. What actual row volumes justify `limit`/`offset` defaults for season detail
   and history endpoints?
6. Which canonical team and venue crosswalks are sufficiently authoritative to
   expose without guessing?
7. What minimum deprecation duration should complement the commitment to avoid
   retiring a supported version during an AFL season?
8. What is the exact legacy-removal sequence once every inventory row has a
   disposition?

These are repository investigations and implementation choices, not reasons to
change the consumer-facing principles above.

## 23. Acceptance summary

The v1 consumer API is complete when an authenticated consumer can start at the
API root, discover an AFL season, navigate through its rounds and matches,
resolve canonical players and known provider IDs, retrieve current rosters and
injuries, and consume live-to-final player statistics without knowing the
underlying collectors or database.

Responses must be stable, factual, self-contained, explicit about lifecycle and
freshness, and safe for both people and applications. Advanced provenance must
be permission-controlled and additive. Legacy routes may then be retired
according to the documented capability checklist, leaving `/api/v1` as the
first supported published consumer contract.
