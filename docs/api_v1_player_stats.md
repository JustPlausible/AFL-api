# Canonical player-stat API (`/api/v1`) — consumer reference

**Audience:** API consumers. The authoritative workflow is the
[Consumer API workflow design](architecture/workflows/consumer_api_design.md);
the [player-stat API design](architecture/api/player_stats_api_design.md)
provides endpoint-specific background.

## Endpoint and authentication

```text
GET /api/v1/matches/{match_id}/player-stats
```

The endpoint resolves the numeric `matches.match_id` used elsewhere by the
API and returns the persisted canonical player-stat resource. Send an API key
in `X-Api-Key`. An ordinary authenticated credential can use normal mode. A
missing or invalid API key receives `401` with
`{"detail": "Invalid or missing API Key"}`.

### Parameters

| Name | Location | Type | Required | Description |
| --- | --- | --- | --- | --- |
| `match_id` | path | integer | Yes | Existing numeric match identifier. |
| `side` | query | `home` \| `away` | No | Return only players on that match side. Other values receive FastAPI's normal `422`. |
| `canonical_player_id` | query | integer | No | Return only the player with this AFL-api canonical player ID, matched directly against the persisted stat row. The preferred consumer-facing identifier. A non-integer value receives FastAPI's normal `422`. |
| `champion_data_player_id` | query | string | No | Return only the player with this opaque Champion Data ID. Provider-specific; retained for compatibility and explicit provider workflows. |
| `advanced` | query | boolean | No | Add selected provenance. Defaults to `false` and requires the `advanced-read` capability when `true`. |

### Choosing a player filter

`canonical_player_id` is the preferred identifier for consumers navigating the
canonical AFL-api graph: a canonical player resolved via `GET
/api/v1/players/{canonical_player_id}` or `GET /api/v1/players?search=...`
carries a `canonical_player_id` directly usable here, without needing to look
up a Champion Data ID first:

```text
GET /api/v1/players?search=Smith
  -> GET /api/v1/players/{canonical_player_id}/seasons   (season_id)
  -> GET /api/v1/seasons/{season_id}/rounds               (round_id)
  -> GET /api/v1/rounds/{round_id}/matches                (match_id)
  -> GET /api/v1/matches/{match_id}/player-stats?canonical_player_id={canonical_player_id}
```

`champion_data_player_id` remains fully supported with unchanged semantics
for provider-specific workflows, e.g. correlating against a CFS export:

```text
GET /api/v1/matches/{match_id}/player-stats?champion_data_player_id=CD_I1004321
```

Both filters match directly against the values persisted on the
`cfs_player_stats` row; neither is resolved from the other. Supplying both is
conjunctive (`AND`) -- the row must satisfy both predicates. For the same
player this is a no-op that returns the same row; for two different players
it deterministically returns `players: []`, the endpoint's normal
no-match/empty-result outcome (see below), rather than a new error type. A
row whose persisted `canonical_player_id` is `null` (an unresolved crosswalk)
never matches `canonical_player_id` but remains reachable via
`champion_data_player_id`, exactly as before this filter existed.

## Normal response

```json
{
  "match": {
    "match_id": 8216,
    "match_provider_id": "CD_M20260142001",
    "round_id": 12,
    "season_id": 2026,
    "status": "CONCLUDED"
  },
  "lifecycle": {"finality": "final"},
  "metadata": {"source_updated_at": "2026-08-09T09:32:11+00:00"},
  "players": [
    {
      "champion_data_player_id": "CD_I1004321",
      "canonical_player_id": 4821,
      "afl_player_id": 12345,
      "display_name": "J. Smith",
      "side": "home",
      "team_id": 15,
      "stats": {
        "goals": 3, "behinds": 1, "kicks": 12, "handballs": 8,
        "disposals": 20, "marks": 5, "tackles": 4, "hitouts": 0
      }
    }
  ]
}
```

The identity fields are explicit crosswalks, not guesses. Unresolved
`canonical_player_id`, `afl_player_id`, `display_name`, or contextual
historical `team_id` values remain present as `null`. A known zero stat is `0`;
an unavailable stat is `null`. The stable stat set is `goals`, `behinds`,
`kicks`, `handballs`, `disposals`, `marks`, `tackles`, and `hitouts`.

Normal player rows deliberately exclude collection and persistence provenance.
Raw provider JSON and unselected database columns are never exposed.

## Freshness and filtering

`metadata.source_updated_at` is the newest authoritative source observation
used to produce the returned resource, represented here by the maximum
`collected_at` among the **returned player-stat rows**. It is not request serve
time, current time, or scheduler-run time. Consequently, `side`,
`canonical_player_id`, and `champion_data_player_id` filters calculate
freshness only from rows surviving the filter. A valid result with no
returned rows has `"source_updated_at": null`; the API never fabricates
freshness. This includes a `canonical_player_id` that identifies a real
canonical player with no persisted stat row for this match, and a
`canonical_player_id` that conflicts with a supplied `champion_data_player_id`.

## Lifecycle and empty states

`lifecycle.finality` is calculated on every request with the repository's
shared player-stat authority rule over the match, independently of response
filters:

* `final`: authoritative concluded rows cover both sides with no lower-authority row;
* `partial`: some authoritative rows exist but coverage is one-sided or mixed; or
* `not_available`: no authoritative snapshot exists yet.

A real match with an unresolved provider ID, no persisted rows, or no rows
matching a valid filter returns `200` and `players: []`. Live rows can be
returned while finality is `not_available`. Only a nonexistent `match_id`
returns `404`.

Collector evidence used to explain the finality decision is not part of the
normal lifecycle contract; it is available only in advanced mode.

## Advanced metadata

An API key with `advanced-read` may request `?advanced=true`. Advanced mode is
strictly additive: all normal values and shapes remain the same, while each
player gains only:

```json
"advanced": {
  "snapshot_authority": 2,
  "resolved_match_status": "CONCLUDED",
  "collected_at": "2026-08-09T09:32:11+00:00"
}
```

The response also gains an `advanced.finality_evidence` object containing
`authoritative_rows`, `authoritative_sides`, `min_snapshot_authority`, and
`max_snapshot_authority`. This selected evidence explains the shared finality
result without turning the endpoint into a database-row dump. Advanced mode
does not expose raw JSON, arbitrary source fields, provider URLs, or collector
failures.

## Errors and status codes

Application errors use this common `/api/v1` shape:

```json
{"error": {"code": "match_not_found", "message": "Match not found."}}
```

| Status | Stable application code / meaning |
| --- | --- |
| `200` | Match exists; `players` may legitimately be empty. |
| `403` | `advanced_access_required` when a valid key without `advanced-read` requests advanced mode. Message: `This API key does not permit access to advanced metadata.` |
| `404` | `match_not_found` when the addressed match does not exist. |
| `401` | Missing or invalid API key. Message: `Invalid or missing API Key`. |
| `422` | Framework request or parameter validation, such as an invalid `side` value or a non-integer `canonical_player_id`. |

Application errors do not expose SQL, table names, stack traces, secrets,
provider URLs, or raw collector failures. Authentication failures use `401`;
FastAPI request and parameter validation remains in its standard `422` shape.

## Ordering and scope

Players retain the deterministic order `side`, then
`champion_data_player_id`. The endpoint does not fetch upstream on demand and
does not alter collection, scheduling, persistence authority, match resolution,
or canonical identity. It adds no pagination because one match is naturally
bounded. The legacy unversioned routes remain unchanged.
