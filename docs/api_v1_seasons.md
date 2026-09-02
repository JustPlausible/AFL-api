# AFL-api v1 discovery and seasons

The v1 discovery root and seasons resource are the starting point for the
consumer navigation hierarchy. Start at `GET /api/v1`, follow the documented
seasons resource, and use a returned `season_id` with later round and match
resources as those resources become available.

Both endpoints are read-only and require an active API key in the
`X-Api-Key` request header:

```http
X-Api-Key: your-api-key
```

A missing or invalid key returns HTTP `401`:

```json
{"detail": "Invalid or missing API Key"}
```

## Discover the API

```http
GET /api/v1
```

This deliberately small response identifies the API and points to its
generated documentation. It does not disclose database, collector, scheduler,
health, or other operational state.

```json
{
  "name": "AFL-api",
  "version": "0.7.0",
  "documentation": "/docs"
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | Public API name. |
| `version` | string | Running AFL-api release version. |
| `documentation` | string | Location of generated interactive API documentation. |

## List seasons

```http
GET /api/v1/seasons
```

The response lists the seasons persisted by canonical AFL season sync, newest
first. It does not calculate the current season or round at request time and it
does not return raw provider payloads or internal metadata.

```json
{
  "seasons": [
    {
      "season_id": 85,
      "year": 2026,
      "name": "2026",
      "is_current": true,
      "current_round_number": 1
    },
    {
      "season_id": 84,
      "year": 2025,
      "name": "2025",
      "is_current": false,
      "current_round_number": 24
    }
  ]
}
```

| Field | Type | Nullable | Meaning |
| --- | --- | --- | --- |
| `seasons` | array | No | Persisted AFL seasons, ordered by descending year and season ID. |
| `season_id` | integer | No | Numeric AFL season identifier, mapped from canonical `afl_id`. |
| `year` | integer | No | Calendar year recorded for the season. |
| `name` | string | No | Consumer-facing persisted season name. |
| `is_current` | boolean | No | Current-season indicator maintained by season sync. |
| `current_round_number` | integer | Yes | Current round maintained by season sync, or `null` when unavailable. |

The response intentionally omits provider identifiers, `metadata_json`,
`source_json`, timestamps, and competition-selection details. Version 1
currently targets the AFL men's competition and does not accept a competition
selector.

## List a season's canonical player membership

```http
GET /api/v1/seasons/{season_id}/players?limit=250&offset=0
```

Returns the canonical player membership persisted for one AFL season (Issue
#247) — the reverse direction of
[`GET /api/v1/players/{canonical_player_id}/seasons`](api_v1_players.md#list-a-canonical-players-seasonteam-memberships):

```text
season -> season player memberships   (this endpoint)
player -> player season memberships   (api_v1_players.md)
```

### Population authority

This resource enumerates `competition_season_players` directly and **only**
`competition_season_players` — it is never derived from, supplemented by, or
back-filled from:

* AFL StatsPro `SEASON_TOTAL` full-season summaries;
* derived Home & Away summaries;
* match player-stat appearances (`cfs_player_stats`);
* match rosters;
* injuries;
* editorial player-movement records;
* the legacy `players` table.

Because none of those are involved, this endpoint can be populated and
served **before** the requested season has played a match or produced any
statistical summary, as long as `competition_season_players` has already
been populated for it. This is membership/identity data, not statistics.
Completeness and timing reflect exactly what the upstream season-player
collection has persisted — a season with no ingested player list simply
returns an empty collection; this is not automatically a point-in-time
preseason snapshot, and no future-membership source or provenance question is
addressed by this resource.

Enrichment beyond the raw membership row is limited to three things: the
player's canonical identity (`display_name`, using the identical fallback as
[`GET /api/v1/players/{canonical_player_id}`](api_v1_players.md)), **that
membership's own season-specific team**, and existing `player_provider_ids`
crosswalks.

Send an API key in `X-Api-Key`; auth behaves identically to every other
`/api/v1` route. No capability beyond standard authentication is required —
the same ordinary access level as the canonical player identity resource.

### Parameters

| Name | Location | Type | Required | Description |
| --- | --- | --- | --- | --- |
| `season_id` | path | integer | Yes | Existing `afl_seasons.afl_id`. Unknown values return `404`. |
| `limit` | query | integer | No | Maximum rows per page. Default and maximum `250`, minimum `1`. |
| `offset` | query | integer | No | Rows to skip. Default `0`, minimum `0`. |

### Response

```json
{
  "players": [
    {
      "canonical_player_id": 584,
      "display_name": "Nick Daicos",
      "team": {"team_id": 3, "name": "Collingwood"},
      "identifiers": {
        "afl_player_id": 5501,
        "champion_data_player_id": "CD_I1023261"
      }
    }
  ],
  "limit": 250,
  "offset": 0
}
```

Field notes:

* `players` is ordered by `canonical_player_id` ascending, then paginated —
  a deterministic order chosen specifically so repeated pagination across
  the same unchanged membership never reorders or skips rows.
* `team` is **this season's own** `competition_season_players.team_id`,
  resolved against `afl_teams` — never the current/latest-season team
  projection used by `current_team` on
  [`GET /api/v1/players/{canonical_player_id}`](api_v1_players.md). A player
  whose 2026 membership is Team A and whose 2027 membership is Team B always
  reports Team A from `GET /api/v1/seasons/{2026_id}/players`, regardless of
  which season is currently marked current. `team` is `null` only when that
  season's own membership row has no resolved `team_id` — never borrowed
  from another season.
* `display_name` and `identifiers` follow exactly the same rules as
  [`GET /api/v1/players/{canonical_player_id}`](api_v1_players.md#get-a-canonical-player-by-id) —
  no parallel identity projection is introduced by this resource.

### Pagination

`limit` defaults to and caps at `250`; `offset` defaults to `0`. A typical
AFL season has roughly 800–900 persisted players, so four requests at the
default/maximum page size are the expected and intentional way to retrieve a
full season's membership — this resource deliberately does not add cursor
pagination, page numbers, `next` links, or a required total count.

### Errors and status codes

| Status | Stable application code / meaning |
| --- | --- |
| `200` | The season exists; `players` holds zero or more membership rows for the requested page. |
| `404` | `season_not_found` when no `afl_seasons` row matches `season_id`. |
| `401` | Missing or invalid API key. Message: `Invalid or missing API Key`. |
| `422` | Framework/parameter validation — a non-integer `season_id`, or `limit`/`offset` outside their documented bounds. |

A valid season with zero persisted membership rows, and an `offset` beyond
the end of the population, both return `200` with `{"players": [], ...}` —
neither is an error.

## Related resources

- [Canonical match player statistics](api_v1_player_stats.md)
- [Canonical v1 player lookup, search, and season history](api_v1_players.md)
- [Consumer API workflow design](architecture/workflows/consumer_api_design.md)
- Interactive OpenAPI documentation at `/docs` on a running deployment
