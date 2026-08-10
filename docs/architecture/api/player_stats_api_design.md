# Canonical CFS Player-Stat Read API Design

**Status:** Stage 1 implementation design and current-behaviour record

**Architectural authority:** [Consumer API workflow design](../workflows/consumer_api_design.md)

> This document remains the endpoint-specific design record for the implemented
> Stage 1 player-stat route. The human-led consumer API workflow now governs the
> broader v1 direction. Where the documents differ, the workflow supersedes
> this design for future work: unversioned routes may retire after capability
> migration; advanced access is permission-controlled; ordinary freshness is a
> resource-level `source_updated_at`; and collector-level authority, resolved
> status, and collection timestamps move to advanced metadata. The shipped
> [consumer reference](../../api_v1_player_stats.md) remains authoritative for
> current behaviour until those follow-up changes are implemented.

**Related reviews:** [Post-v0.5.0 engineering status review](../project_status_post_v0_5_0.md)
(§13 Option A — canonical read API for downstream consumers),
[Engineering status and scheduler-readiness review](../project_status_scheduler_readiness.md)
(§1 and §10 — canonical read API as the secondary next-phase milestone, after
scheduler assurance)

**Related contracts:** [AFL data authority and identity map](../data_authority_map.md),
[Player-stat persistence and authority contract](../player_stats_storage_contract.md)

**Related issue:** none yet. §12 Stage 1 of this document is intended to become
the first canonical-API implementation Issue.

**Intended milestone:** Canonical read API (the secondary milestone identified
in the scheduler-readiness review; scheduler assurance work — PRs #135–#149 —
has since landed timezone/SQLite coordination, durable match-window leases,
conservative player-stat polling, and interrupted-attempt recovery, which is
the freshness/finality evidence that review said this milestone should wait
for).

## 1. Background and current limitations

AFL-api persists authoritative match player statistics in `cfs_player_stats`
(migration `0006_cfs_player_stats.py`, extended by `0009_canonical_player_seasons.py`).
[`player_stats_storage_contract.md`](../player_stats_storage_contract.md) already
establishes this as the sole authoritative persistence target for current
collection and all new reads, populated by `MatchPlayerStatsCollector` /
`upsert_player_stats` (`afl_json/player_stats.py`) from authenticated CFS JSON.

`api/routes.py` is the entire current consumer-facing surface. It has no
version prefix — every route is a bare `/api/...` path — and it has these
concrete limitations relevant to player statistics:

* `GET /api/player-stats` reads the **legacy** `player_stats` table (Playwright-
  rendered AFL match-centre HTML), not `cfs_player_stats`. Its own docstring
  and `player_stats_storage_contract.md` both say it "does not expose
  authoritative CFS player statistics." It is explicitly documented as a
  compatibility endpoint that must not be silently repointed at CFS storage
  without "a separate versioned response/identity compatibility design" —
  that follow-up design is what this document provides.
* No route reads `cfs_player_stats` at all today. There is no canonical
  player-stat reader in the API surface.
* `GET /api/players`, `GET /api/players/{afl_id}`, and
  `GET /api/players/club/{club_slug}` read the legacy `players`
  profile/enrichment table, not `canonical_players` /
  `competition_season_players`.
* Every route returns `SELECT * FROM <table>` dumped through
  `dict(row)`/`JSONResponse`. Response shape is whatever the current table
  schema happens to contain; there is no versioning, no field stability
  guarantee, and adding a database column silently changes every consumer's
  response.
* There is no identifier crosswalk in any response: numeric AFL IDs, opaque
  Champion Data IDs, and canonical IDs are never resolved or related for a
  caller.
* There is no lifecycle/finality signal. A consumer cannot tell from any
  existing response whether a given row is a live/partial observation or a
  concluded, authoritative one.
* Auth is a single flat scheme (`verify_api_key`, `X-Api-Key` header, one
  active-key table, no scopes). This is adequate for a first canonical surface
  and is reused rather than replaced (§4).

### 1.1 Pre-v1 routes unchanged by this Stage 1 design

None of the following routes, tables, or response shapes change as a result of
this document or its Stage 1 (§12):

| Route | Backing table | Why Stage 1 left it unchanged |
| --- | --- | --- |
| `GET /api/player-stats` | `player_stats` (legacy) | Explicitly named the legacy compatibility endpoint in `player_stats_storage_contract.md`; changing its table/response is out of scope here and requires its own versioned identity-compatibility design. |
| `GET /api/players`, `/api/players/{afl_id}`, `/api/players/club/{club_slug}` | `players` (legacy) | Legacy profile/enrichment model, not `canonical_players`. Superseding this is a separate design (player identity/profile API), not part of the CFS player-stat surface. |
| `GET /api/lineups/latest/{afl_id}`, `/api/lineups/{round_number}`, `/api/lineups/{round_number}/{afl_id}` | `lineups` (legacy, HTML-backed) | `data_authority_map.md` records that canonical CFS lineup/roster persistence is **not implemented**; there is no canonical source to read instead yet. |
| `GET /api/injuries`, `/api/injuries/{afl_id}` | `injuries` | Different domain and already canonical-resolved; unaffected by CFS player-stat design. |
| `GET /api/rounds`, `/api/rounds/{round_id}`, `GET /api/matches`, `/api/matches/{match_id}` | `rounds`, `matches` | Already populated by public AFL JSON (mostly-canonical source per `data_authority_map.md`), but unversioned `SELECT *` responses with no stability contract. A canonical `/api/v1` equivalent is useful but is **not** part of the CFS player-stat surface; it is named as a later stage (§12, Stage 3) so it can reuse the versioning policy established here, not designed in detail by this document.

Stage 1 did not touch `api/routes.py`; it added the first `/api/v1` surface
alongside it. The broader consumer workflow now permits those pre-v1 routes to
retire after their useful capabilities have been accounted for.

## 2. Goals

1. Give consumers a canonical, versioned way to read authoritative CFS match
   player statistics without direct database access or legacy-table ambiguity.
2. Make canonical player identity, Champion Data identity, and existing
   numeric AFL/internal identity all visible and explicitly related in one
   response, without inventing new crosswalks.
3. Make the live/partial vs. concluded/authoritative distinction a first-class,
   explicit part of every response.
4. Keep the first implementation stage small enough to file as one bounded
   GitHub Issue, using only already-authoritative persistence and already-
   implemented, already-tested join/finality logic.
5. Establish a versioning and stability policy the next canonical routes
   (players, matches, rounds) can reuse without re-litigating it.

## 3. Non-goals

This design does not:

* change, rename, or repoint any existing `/api/...` route (§1.1);
* merge, retire, or reconcile `player_stats` and `cfs_player_stats`;
* add fallback between CFS and legacy HTML data, or dual-write;
* introduce an ORM, a second database access pattern, or a separate consumer
  API service/container — the new routes live in the existing FastAPI app,
  using the existing `sqlite3`/`get_db_connection()` access pattern;
* add pagination, caching, or rate-limiting policy beyond what a single
  match's roster requires;
* design canonical roster/lineup, competition/season/team, or player-profile
  endpoints (candidates for later stages, not designed here);
* change authentication, add scopes, or add a new credential type;
* depend on scheduler-internal state (`match_stat_windows`, leases, cadence).

§13 restates these as explicit deferred items alongside what each depends on.

## 4. API versioning and architectural position

New canonical routes are added under an explicit version path prefix,
`/api/v1/...`, in a new module `api/routes_v1.py`, following the same style as
`api/routes.py` (full path strings on `@router.get(...)`, `client_label: str =
Depends(verify_api_key)`, `db.connection.get_db_connection()`). `main.py`
mounts it with `app.include_router(api_v1_router)` alongside the existing
`api_router`.

```text
Consumer
   |
   v
FastAPI app (main.py)
   |-- health_router            (unchanged)
   |-- api_router  (api/routes.py)      -- unversioned pre-v1 routes
   |-- api_v1_router (api/routes_v1.py) -- NEW: versioned canonical routes
                        |
                        v
        Existing authoritative tables and pure helpers
        (cfs_player_stats, matches, afl_teams, canonical_players,
         player_provider_ids; afl_json.season_report finality predicate)
```

Versioning policy for `/api/v1`:

* Additive, backward-compatible changes (new optional response fields, new
  optional filters) do not require a new version.
* Any breaking change (removing/renaming a field, changing a field's type or
  meaning, changing default filter behaviour) requires `/api/v2` and a
  documented deprecation window for `/api/v1`; it must not be made in place.
* Unversioned `/api/...` routes are pre-v1 behaviour rather than part of the
  versioned contract. They are not renamed or silently repointed in place; a
  useful capability receives a canonical `/api/v1` (or later) replacement.
  Once the consumer workflow's legacy checklist is satisfied, the old route
  may be retired.
* Authentication is unchanged: `verify_api_key` / `X-Api-Key`, reused as-is.
  No scope or key-type distinction is introduced by this design.

## 5. Identifier semantics

This table extends `data_authority_map.md`'s identifier guide for exactly the
identifiers this API surface exposes. It does not redefine any of them.

| Identifier | Source column | Nullable in response? | Notes |
| --- | --- | --- | --- |
| `champion_data_player_id` | `cfs_player_stats.champion_data_player_id` | No — `NOT NULL` in the schema and required by the collector (`_normalise_entry` rejects entries with no player ID). | Always present; the primary correlation key to CFS. Never parse or synthesise a numeric value from it (it is opaque, per `data_authority_map.md`). |
| `canonical_player_id` | `cfs_player_stats.canonical_player_id` | Yes | Denormalised copy of the `player_provider_ids(provider='champion_data')` crosswalk, populated by `upsert_player_stats` **at write time**. It is not guaranteed to reflect a provider mapping added after that row was last written; the repository's own season report already monitors this as `player.provider_mapping_conflict` (`afl_json/season_report.py`), so the API reads the stored column directly rather than re-joining `player_provider_ids` on every request (§7 for why this join is authoritative to read as-is). |
| `afl_player_id` | `player_provider_ids` where `provider='afl'` and `provider_player_id` matches the resolved `canonical_player_id` | Yes | Resolved by joining `canonical_player_id` (once known) to `player_provider_ids(provider='afl')`, mirroring the pattern in `afl_json/season_report.py::_identity_checks`. Never guessed from `champion_data_player_id`. |
| `display_name` | `canonical_players.display_name` (fallback `given_name`/`family_name`) | Yes | `canonical_players` rows may have no usable name yet (season report already flags `player.incomplete_identity`); the API must not raise on a null name, it must return `null`. |
| `match_id` | `matches.match_id` | No (path parameter; 404 if absent, see §6) | The existing numeric identifier consumers already use via `/api/matches/{match_id}` and `/api/rounds/{round_id}`. This is the identifier space Stage 1 uses for match lookup — no new match-identifier vocabulary is introduced for consumers who already navigate via existing routes. |
| `match_provider_id` | `matches.match_provider_id` (resolved from the path `match_id`), matching `cfs_player_stats.match_provider_id` | Yes (a match may not have a resolved provider ID yet) | Exposed in the response for consumers who need to correlate with CFS directly, but is **not** the path identifier (see above). |
| `team_id` | `matches.home_team_id` / `matches.away_team_id`, matched to a player row by `cfs_player_stats.side` | Yes | Numeric `afl_teams.afl_id`. See §7 for why `side` is required to resolve this, and why it is *not* the same thing as a `clubs.code`. |

`cfs_player_stats.afl_match_id` (a denormalised, collection-time-supplied TEXT
copy of the numeric match id) is deliberately **not** surfaced directly in the
response. It is a different type (`TEXT`) from `matches.match_id` (`INTEGER`)
and may be stale relative to the match actually resolved from the path. The
response instead reports the numeric `match_id` that was actually used to
resolve the request, taken from the `matches` row.

No field in this design is synthesised, inferred, or guessed across
namespaces. An unresolved crosswalk is `null`, exactly as
`data_authority_map.md` requires ("leave identity unresolved when no mapping
exists").

## 6. Resource: match player statistics (Stage 1 surface)

```text
GET /api/v1/matches/{match_id}/player-stats
```

* `match_id` (path, integer, required) — `matches.match_id`, the existing
  canonical/legacy-numeric identifier space (same as `/api/matches/{match_id}`).
* `side` (query, optional, `home` or `away`) — filters returned players to one
  side. Invalid values return `422` (FastAPI's standard `Query` validation via
  an `Enum` or `Literal`).
* `champion_data_player_id` (query, optional, string) — filters to a single
  player already known by CFS ID.

### 6.1 Resolution and status codes

1. Look up `matches` by `match_id`.
   * Not found → `404` with a body identifying the reason
     (`{"detail": "Match not found"}`), consistent with the existing
     `/api/matches/{match_id}` behaviour.
2. If found but `matches.match_provider_id IS NULL` (no CFS identity resolved
   yet), return `200` with `players: []` and
   `lifecycle.finality = "not_available"` (§8) — this is a legitimate state
   (e.g. a future/unresolved fixture), not an error.
3. If a `match_provider_id` is resolved, query `cfs_player_stats` for that
   `match_provider_id` (§7 for the exact join). Zero rows is also `200` with
   `players: []` and `lifecycle.finality = "not_available"` (not yet
   collected, or collected as `unavailable`/`empty` and therefore never
   written — see `player_stats_storage_contract.md`'s publication-state table).
4. Otherwise return `200` with the populated `players` array and lifecycle
   block.

A match that exists with a resolved `match_provider_id` and zero stat rows is
indistinguishable, from this endpoint alone, between "not yet collected" and
"collected as empty/unavailable" — both are legitimately zero-write outcomes
per `upsert_player_stats`. Distinguishing them would require exposing
collection-attempt/audit evidence, which is explicitly out of scope for a
consumer read API (§13).

### 6.2 Response model

```json
{
  "match": {
    "match_id": 8216,
    "match_provider_id": "CD_M20260142001",
    "round_id": 12,
    "season_id": 2026,
    "status": "CONCLUDED"
  },
  "lifecycle": {
    "finality": "final",
    "authoritative_rows": 44,
    "authoritative_sides": 2,
    "min_snapshot_authority": 2,
    "max_snapshot_authority": 2
  },
  "players": [
    {
      "champion_data_player_id": "CD_I1004321",
      "canonical_player_id": 4821,
      "afl_player_id": 12345,
      "display_name": "J. Smith",
      "side": "home",
      "team_id": 15,
      "stats": {
        "goals": 3,
        "behinds": 1,
        "kicks": 12,
        "handballs": 8,
        "disposals": 20,
        "marks": 5,
        "tackles": 4,
        "hitouts": 0
      },
      "snapshot_authority": 2,
      "resolved_match_status": "CONCLUDED",
      "collected_at": "2026-08-09T09:32:11+00:00"
    }
  ]
}
```

Field notes:

* `match.status` is the **current canonical** `matches.status` (public AFL
  JSON, kept fresh by existing match-status reconciliation), not any single
  stat row's `resolved_match_status`. The two can legitimately disagree
  briefly — see §8.
* `players[].resolved_match_status` and `players[].snapshot_authority` are the
  values persisted on that specific row (per-row, not per-response — see §8
  for why rows are not guaranteed to share one authority).
* `players` is ordered by `side, champion_data_player_id` (consistent with the
  operator verification query already documented in
  `player_stats_storage_contract.md`).
* Numeric CFS stat fields (`goals`, `behinds`, ...) may be stored as SQLite
  `NUMERIC` (including a small number of `Decimal`-derived non-integers per
  `afl_json/player_stats.py::_number`); serialise them as JSON numbers as-is,
  do not coerce to `int`.

## 7. Authoritative vs. assumption-requiring joins

### 7.1 Currently authoritative (safe to reuse as designed; already implemented and exercised elsewhere in the repository)

* `matches.match_id → matches.match_provider_id → cfs_player_stats.match_provider_id`
  — the natural key used by `upsert_player_stats` itself.
* `cfs_player_stats.champion_data_player_id → player_provider_ids(provider='champion_data') → canonical_players.id`
  — "the only validated player crosswalk" per `data_authority_map.md`; the
  same pattern `afl_json/season_report.py::_identity_checks` and
  `upsert_player_stats` itself already use.
* `cfs_player_stats.canonical_player_id` read directly (not re-joined) — see
  §5; its drift from a live join is already an existing, monitored data-
  quality signal (`player.provider_mapping_conflict`), not something this API
  needs to solve.
* `canonical_player_id → player_provider_ids(provider='afl')` for the numeric
  AFL player ID — same pattern as above, different provider value.
* **Team identity per stat row**: `cfs_player_stats.side` joined through
  `matches.home_team_id` / `matches.away_team_id` to `afl_teams.afl_id`. This
  is *not* a shortcut — `cfs_player_stats.team_provider_id` is currently
  **always `NULL`** at the source (`afl_json/player_stats.py` sets it to
  `None` unconditionally, with an explicit comment that the CFS payload
  supplies no independent team identity). The side-based join through
  `matches`/`afl_teams` is the only populated path today, and it is the exact
  pattern `afl_json/season_report.py::_team_context_checks` already uses and
  tests.
* **Match-level finality**: call
  `afl_json.season_report.authoritative_stats_finality_for_match(conn, match_provider_id)`
  directly rather than reimplementing its predicate. It is a pure, already-
  tested function that returns `has_authoritative_snapshot`,
  `is_partial_authoritative_snapshot`, `has_satisfactory_concluded_coverage`,
  and the row-count evidence needed for the `lifecycle` block in §6.2 and §8.
  Reusing it also means a future change to finality semantics (e.g. the
  `MIN_CONCLUDED_AUTHORITATIVE_PLAYER_ROWS` floor) automatically stays
  consistent between season completeness reporting and this API.

### 7.2 Requires assumptions today — must not be joined implicitly

* **Player's current club** (`clubs.code`/`clubs.name`). There is no validated
  crosswalk from `afl_teams.afl_id`/team participation to a `clubs` row —
  `data_authority_map.md` states plainly that "a club and a provider
  team/season entry are related concepts, not interchangeable identities."
  Stage 1 exposes only the numeric `team_id` (`afl_teams.afl_id`); it must
  never expose a club code or name derived by assumption.
* **Photo, listed position, jumper number**. These live in
  `competition_season_players`, keyed by `(player_id, competition_season_id)`,
  not on the stat row. Joining them requires resolving `matches.season_id`
  (itself present but not guaranteed populated for every legacy row) and
  accepting that `competition_season_players.team_id` "may remain null when
  unresolved" per `data_authority_map.md`. Not needed for the match-stat
  resource in Stage 1; deferred to a future player-resource stage (§12, Stage
  2) where it can be designed with its own null-handling and season-scoping
  rules rather than bolted onto this endpoint.
* **`players` / `player_stats` (legacy tables)**. Must never be joined into
  this surface as a fallback for missing canonical identity or missing stats
  — this is the exact fallback `player_stats_storage_contract.md` prohibits
  ("Do not implement 'whichever table has rows'... or silent legacy
  fallback").
* **`match_stat_windows`** (scheduler control-plane; `db/migrations/0013...`).
  This table's lifecycle/lease/cadence fields describe *orchestration* state
  (is a poll due, who owns the lease), not *data* authority, and
  `match_window_planner.md` scopes its inspection to
  `/scheduler/match-windows` for operators. This API must not read it or
  expose any of its fields (§13).

## 8. Lifecycle and snapshot semantics

Consumers need to distinguish "this is a live/partial observation that may
still change" from "this is concluded, authoritative data" without guessing
from timing.

* Every persisted row already carries `snapshot_authority` (`1` = non-final —
  covers both `live_partial` and `unknown` collector states — or `2` =
  `concluded`; `unavailable`/`empty` results are never persisted, per
  `upsert_player_stats`). The API surfaces this per row as
  `players[].snapshot_authority`, unchanged.
* `upsert_player_stats`'s `WHERE excluded.snapshot_authority > ... OR (== AND
  newer AND changed)` guard makes authority **monotonic per row** but does
  **not** guarantee every row for a match advances in lockstep — one side's
  array can be published/collected before the other's, or a single collection
  attempt can reject some players. A response must therefore never claim
  match-level finality from a single row or from `matches.status` alone.
* The response computes match-level `lifecycle.finality` from the **current,
  full row-set** at request time via
  `authoritative_stats_finality_for_match()` (§7.1), read fresh on every
  request — exactly as `afl_json/season_report.py` already does, and for the
  same reason (a cached/derived flag could go stale the moment a new poll
  writes one more row). Map its result to:
  * `"final"` — `has_authoritative_snapshot` and not
    `is_partial_authoritative_snapshot` (two-sided, fully concluded-authority
    coverage).
  * `"partial"` — `has_authoritative_snapshot` but
    `is_partial_authoritative_snapshot` (mixed authority across rows, or only
    one side at concluded authority). Mirrors the season report's own
    `match.partial_authoritative_stats` warning.
  * `"not_available"` — no authoritative (`snapshot_authority=2`) rows at all;
    this includes matches with only `live_partial`/`unknown` rows, matches
    with no rows, and matches with no resolved `match_provider_id` (§6.1). A
    consumer building a live view should still read the `players` array (it
    can be non-empty with live data) but must treat `finality != "final"` as
    "may still change."
* `lifecycle.authoritative_rows`, `authoritative_sides`,
  `min_snapshot_authority`, and `max_snapshot_authority` are the evidence
  fields from the same predicate, exposed so a consumer can distinguish
  "zero rows" from "one-sided" from "mixed authority" instead of only getting
  a single opaque enum.
* Per-row `collected_at` and `resolved_match_status` are exposed so a
  consumer can reason about freshness directly rather than polling faster
  than the source. Consumer guidance to document alongside this endpoint: do
  not poll faster than the default live collection cadence recorded in
  `match_window_planner.md` (60 seconds per live match); this API reflects
  whatever the scheduler has most recently persisted; it does not fetch from
  CFS on demand.
* `match.status` (from `matches`, §6.2) is the canonical match lifecycle and
  may be one step ahead of what a given stat row's `resolved_match_status`
  shows immediately after a lifecycle transition (e.g. `match.status =
  CONCLUDED` while some `players[].resolved_match_status` still reads
  `POSTGAME` until the next poll writes that row) — this is expected,
  documented behaviour, not a bug, matching
  `stats.authority_lifecycle_conflict`'s inverse (a genuinely wrong direction,
  concluded-authority stats before the match is concluded) already being a
  season-report error condition. This API surfaces both fields so consumers
  can see the (usually momentary) disagreement rather than papering over it.

## 9. Stable contract fields, `extra_stats_json`, and `raw_player_json`

The initial stable `/api/v1` contract for `stats` includes exactly the eight
fields in `afl_json/player_stats.py::CANONICAL_STAT_FIELDS` — `goals`,
`behinds`, `kicks`, `handballs`, `disposals`, `marks`, `tackles`, `hitouts`.
These are the only fields the collector maps through an explicit, reviewed
canonical mapping; every other CFS field is provider-shaped and currently
lives only in `extra_stats_json` without a reviewed name, unit, or type
guarantee.

Treatment of the two catch-all columns:

* **`extra_stats_json` is not part of the Stage 1 response at all.** It is not
  spread into the top-level object (doing so would silently change the
  contract every time CFS adds, removes, or renames a field upstream — the
  opposite of a versioned contract) and it is not returned as a nested object
  by default.
* If a later stage exposes it, it must be under a clearly non-stable key such
  as `unstable_extra_stats`, opt-in only (e.g. `?include=extra_stats`),
  documented as provider-shaped and exempt from the `/api/v1` additive-only
  guarantee (§4) — fields may appear, disappear, or change type without a
  version bump. This is deferred (§12), not part of Stage 1.
* **`raw_player_json` is not exposed by this API at any stage designed here.**
  It exists, per `player_stats_storage_contract.md`, as lossless forensic
  evidence "so a future mapping does not erase source evidence" — an internal
  safety net, not a consumer contract. It may be large and its shape is
  entirely upstream-controlled. Exposing it is out of scope; revisit only with
  its own explicit design if a concrete consumer need appears (§13).
* **Promotion path**: a field only moves from `extra_stats_json` into the
  stable `stats` contract by first being added to `CANONICAL_STAT_FIELDS` in
  `afl_json/player_stats.py` (which also makes it a first-class persisted
  column, per that module's own comment: "Adding a confirmed canonical AFL
  field should require changing this mapping, the record, and persistence
  only") and then to this API's documented response model in the same
  reviewed change. The API layer must never infer a promotion on its own.

## 10. Testing requirements

Offline `TestClient` tests, following the existing pattern in
`tests/test_health.py` (isolated SQLite fixture DB, no live network), should
cover at least:

* `match_id` not found → `404`.
* Match found, `match_provider_id IS NULL` → `200`, `players: []`,
  `lifecycle.finality = "not_available"`.
* Match found, provider id resolved, zero `cfs_player_stats` rows → `200`,
  same empty/`not_available` shape.
* Rows present, all `snapshot_authority=1` (live/partial) →
  `lifecycle.finality = "not_available"` per §8, `players` populated with
  `snapshot_authority: 1` visible per row.
* Rows present, mixed authority or one-sided concluded coverage →
  `lifecycle.finality = "partial"` — reuse or mirror the fixture patterns
  already in `tests/test_afl_season_report.py` for
  `authoritative_stats_finality_for_match`/`is_partial_authoritative_snapshot`
  rather than inventing new ones.
* Two-sided, fully concluded rows → `lifecycle.finality = "final"`, correct
  `authoritative_rows`/`authoritative_sides`.
* A `champion_data_player_id` with no `player_provider_ids` mapping →
  `canonical_player_id` and `afl_player_id` both `null`, no error.
* `side` filter narrows correctly; invalid `side` value → `422`.
* `champion_data_player_id` filter narrows to one player.
* Missing/invalid `X-Api-Key` → `401`, matching existing `verify_api_key`
  behaviour (no separate auth path introduced).
* A response-shape regression test that asserts the exact top-level and
  per-player key set, so an accidental future `SELECT *`-style change is
  caught immediately — the gap this design exists to close relative to the
  legacy routes.
* Confirm no existing route, table read, or response shape in `api/routes.py`
  changed (a regression guard, not new behaviour to test).

## 11. Documentation requirements

* A new consumer-facing reference page (e.g. `docs/api_v1_player_stats.md`),
  analogous in scope to the existing collection-side
  `docs/match_player_stats.md` but describing the endpoint, parameters,
  example request/response, status codes, the `/api/v1` versioning policy
  (§4), and freshness/polling guidance (§8) — for API consumers, not
  operators.
* FastAPI `summary=`/`description=` on the new route (as every existing route
  in `api/routes.py` already does), so the generated `/docs` OpenAPI page
  stays accurate.
* Link the new reference page from `docs/README.md` and this document's entry
  in `docs/architecture/workflows/README.md` (done by this PR, see below).
* No change to any existing documentation describing `/api/player-stats` or
  other current routes — they are unaffected (§1.1).

## 12. Staged implementation plan

### Stage 1 — `GET /api/v1/matches/{match_id}/player-stats` (the well-bounded first Issue)

Scope, deliberately minimal:

* One new file, `api/routes_v1.py`, with exactly the one route described in
  §6, mounted from `main.py` alongside the existing router.
* Reuse `verify_api_key`, `get_db_connection`, and
  `afl_json.season_report.authoritative_stats_finality_for_match` as-is — no
  new persistence, no new migration, no new table, no scheduler dependency.
* `side` and `champion_data_player_id` filters as specified in §6.
* Tests per §10, documentation per §11.
* No pagination (a single match's roster is bounded — see `MIN_CONCLUDED_AUTHORITATIVE_PLAYER_ROWS`
  and typical AFL team-sheet sizes for scale expectations).

Acceptance for Stage 1: the endpoint returns exactly the response/status-code
behaviour in §6 and §8 for a fixture DB covering every case in §10, with zero
changes to any file under `api/routes.py`'s existing routes, `collection/`,
`afl_json/`, or any migration.

### Stage 2 — `GET /api/v1/players/{canonical_player_id}/stats`

Cross-match, paginated (limit/offset or cursor — decide against the actual row
volumes at implementation time), with season/round filters. Larger than Stage
1 because it needs pagination design, season-scoping, and a decision on
querying an unresolved/unknown `canonical_player_id`.

### Stage 3 — Canonical `/api/v1/matches` and `/api/v1/rounds`

Stable, versioned response models replacing the current ad hoc `SELECT *`
shape, reusing the `/api/v1` versioning policy from §4. Includes a proposed
deprecation timeline for the *legacy* `/api/matches`/`/api/rounds` (not
`/api/player-stats` or `/api/players`, which have their own separate identity-
compatibility prerequisite per `player_stats_storage_contract.md`).

### Stage 4 — Opt-in extended stats

`unstable_extra_stats` inclusion (§9) and the promotion process for moving
specific fields into the stable contract, driven by real consumer demand
rather than speculatively exposing everything now.

### Stage 5+ (not designed here)

Pagination/caching/rate-limit hardening once real usage exists; a canonical
roster/lineup read API once that persistence design and implementation land;
auth scopes if/when multiple consumer classes with different trust levels
emerge. None of these are prerequisites for Stage 1.

## 13. Deferred work and explicit non-goals

Restating and consolidating §3, with what each depends on:

* **No legacy/CFS table merge.** `player_stats` and `cfs_player_stats` remain
  separate, per the explicit prohibition in `player_stats_storage_contract.md`
  ("The two tables must not be merged without that explicit identity,
  provenance, and field-mapping design"). This document's identifier/join
  clarifications (§5, §7) are not that design and do not authorise a merge.
* **No implicit fallback** between CFS and legacy HTML sources, and **no
  dual-write** — unchanged from existing policy.
* **No ORM migration** — the new route uses the same raw `sqlite3`/
  `get_db_connection()` pattern as every existing route.
* **No separate consumer API container/service** — the new routes are mounted
  in the existing FastAPI app/process.
* **No broad API rewrite** — only one new route is added in Stage 1; every
  existing route is untouched.
* **No club/team editorial identity join** (`clubs` table) until a validated
  `afl_teams` ↔ `clubs` crosswalk exists (§7.2).
* **No canonical roster/lineup API** — canonical persistence for that domain
  does not exist yet (`data_authority_map.md`).
* **No fantasy/scoring logic** — this project remains consumer-neutral per
  `README.md`'s stated project scope; scoring belongs in a downstream
  application over this API, not inside it.
* **No exposure of `raw_player_json`**, and `extra_stats_json` only ever as an
  explicitly unstable, opt-in field in a later stage (§9).
* **No new auth scheme or scopes** — `verify_api_key` is reused unchanged.
* **No dependency on `match_stat_windows`** or any other scheduler-internal
  state (§7.2).
* **No pagination design beyond what Stage 1 needs** — deferred to Stage 2,
  where it is actually required.

## 14. Implementation questions to resolve against current code

As with the season-sync and scheduler workflow designs, the implementation PR
should resolve these against the current codebase without changing the
contract agreed above:

1. Confirm the exact current column set/order of `cfs_player_stats` and
   `matches` at implementation time (via `PRAGMA table_info`) in case a
   migration has landed between this document and implementation.
2. Confirm `afl_json.season_report.authoritative_stats_finality_for_match`'s
   signature and import path are still current, and whether it needs a small,
   backward-compatible export tweak (e.g. re-exporting from a more API-
   appropriate module) rather than being imported directly from
   `afl_json.season_report` into `api/routes_v1.py`.
3. Decide the exact FastAPI mechanism for the `side` filter's validation
   (`Literal["home", "away"]` vs. a small `Enum`) consistent with any other
   query-parameter validation already established elsewhere in the codebase.
4. Confirm whether `main.py` should mount `api_v1_router` with an explicit
   `prefix="/api/v1"` on `include_router` or continue the existing convention
   of hardcoding full paths per route (as `api/routes.py` does); prefer
   whichever keeps `api/routes_v1.py` most consistent with the existing file's
   style.
5. Confirm current test fixture conventions for building a minimal
   `cfs_player_stats`/`matches`/`canonical_players`/`player_provider_ids`
   dataset (there may already be a shared fixture helper used by
   `tests/test_afl_season_report.py` worth reusing rather than duplicating).

These are implementation investigations, not license to change the response
model, identifier semantics, join choices, or lifecycle semantics defined in
§5–§9.

## 15. Acceptance boundary

This design is ready to implement when Stage 1 (§12) can be filed as a single
GitHub Issue whose acceptance criteria are exactly §6 (resource/response),
§7.1 (join reuse), §8 (lifecycle semantics), §9 (stable-field boundary), §10
(tests), and §11 (docs) — with §13 confirming to a reviewer that nothing
outside that bounded scope is implied. Stage 2 onward should each cite this
document and state which stage and section they deliver, the same convention
used by `scheduler_workflow_design.md`.
