# Canonical player lookup and search API (`/api/v1`) — consumer reference

**Audience:** API consumers. The authoritative workflow is the
[Consumer API workflow design](architecture/workflows/consumer_api_design.md),
which establishes canonical player identity, provider crosswalks, and
current-season team context as the design these endpoints implement.

This resource has two endpoints that share the same canonical player
identity model:

* `GET /api/v1/players/{canonical_player_id}` — resolve one known
  `canonical_player_id` to its full identity.
* `GET /api/v1/players?search=` — discover a `canonical_player_id` from a
  human-readable name, for a consumer that does not yet know it.

## Get a canonical player by ID

```text
GET /api/v1/players/{canonical_player_id}
```

The endpoint resolves the internal canonical player identifier —
`canonical_players.id`, the same identifier already returned as
`canonical_player_id` by
[`GET /api/v1/matches/{match_id}/player-stats`](api_v1_player_stats.md) — and
returns the persisted canonical player identity resource. Send an API key in
`X-Api-Key`. A missing or invalid API key receives `401` with
`{"detail": "Invalid or missing API Key"}`, matching every other `/api/v1`
route. No capability beyond standard authentication is required.

### Parameters

| Name | Location | Type | Required | Description |
| --- | --- | --- | --- | --- |
| `canonical_player_id` | path | integer | Yes | Existing `canonical_players.id`. A non-integer value receives FastAPI's normal `422`. |

## Response

```json
{
  "player": {
    "canonical_player_id": 584,
    "display_name": "Nick Daicos",
    "current_team": {"team_id": 3, "name": "Collingwood"},
    "identifiers": {
      "afl_player_id": 5501,
      "champion_data_player_id": "CD_I1023261"
    }
  }
}
```

Field notes:

* `canonical_player_id` is the primary consumer identity and is always
  present — it is the same value used as the path parameter and as
  `canonical_player_id` on match player-stat rows.
* `display_name` comes from `canonical_players.display_name`, falling back to
  `given_name`/`family_name` when only those are populated (the same fallback
  already used by the match player-stats resource). It is `null` when no name
  is resolved yet.
* `identifiers` is a typed crosswalk object, not a guess. `afl_player_id` and
  `champion_data_player_id` are resolved from `player_provider_ids` by
  `provider`; an unresolved mapping is `null`, never inferred or synthesised
  from the other identifier.
* `current_team` reflects the player's `competition_season_players` row for
  the current AFL season only (`afl_seasons.is_current = 1`), joined to
  `afl_teams` for a name. It is `null` when there is no current season, the
  player has no membership row for that season, or that membership's team is
  unresolved. Historical (non-current-season) memberships are never used to
  populate this field — a club change must not be inferred as current from
  old data.

### Errors and status codes

Application errors use the common `/api/v1` shape:

```json
{"error": {"code": "player_not_found", "message": "Player not found."}}
```

| Status | Stable application code / meaning |
| --- | --- |
| `200` | The canonical player exists. |
| `404` | `player_not_found` when no `canonical_players` row matches `canonical_player_id`. |
| `401` | Missing or invalid API key. Message: `Invalid or missing API Key`. |
| `422` | Framework request/parameter validation, such as a non-integer `canonical_player_id`. |

## Search canonical players by name

```text
GET /api/v1/players?search=<name>
```

Discovers canonical player identity by human-readable name, so a consumer
can obtain a `canonical_player_id` without already knowing it — from the
database, a legacy endpoint, or AFL/Champion Data directly. Send an API key
in `X-Api-Key`; auth and versioning behave identically to every other
`/api/v1` route (`401` with `{"detail": "Invalid or missing API Key"}` for a
missing or invalid key). No capability beyond standard authentication is
required.

Each result uses the **same** `CanonicalPlayer` projection documented above
— `canonical_player_id`, `display_name`, `current_team`, and `identifiers`
all follow the identical rules described in the Field notes section. This
endpoint intentionally does not introduce a second representation of player
identity.

### Parameters

| Name | Location | Type | Required | Description |
| --- | --- | --- | --- | --- |
| `search` | query | string | **Yes** | Case-insensitive, partial-name search term. Must be non-blank. |

### Search semantics

* **Matching field:** each player's resolved display name — `display_name`
  when set, otherwise `given_name`/`family_name` joined with a space, i.e.
  exactly the same value returned as `display_name` on the result. No other
  field (provider IDs, club, etc.) is searched.
* **Case-insensitive, partial match:** `search=daicos` matches both "Josh
  Daicos" and "Nick Daicos"; `search=DAICOS` and `search=daic` match the
  same players.
* **No fuzzy/phonetic/AI matching, and no provider-ID inference.** A
  numeric-looking `search` value is treated as ordinary search text, never
  as an AFL or Champion Data player ID.
* **Deterministic ordering:** results are ordered by resolved display name
  (case-insensitive), then `canonical_player_id`, both ascending.
* **Bounded result size:** results are capped at 100 rows. This is a safety
  bound on an otherwise unbounded name query, not a general-purpose
  pagination contract; see Scope below.
* **No matches:** a valid, non-blank `search` with no matching players
  returns `200` with `{"players": []}` — this is not an error.

### Missing or blank `search` — design decision

An unfiltered `GET /api/v1/players` collection (returning every canonical
player) is out of scope for this endpoint: it would raise pagination and
performance questions this issue does not address, and silently serving the
full player collection when a consumer forgets `search` would be a worse
default than rejecting the request. `search` is therefore **required and
must be non-blank**:

* A missing `search` query parameter returns FastAPI's standard `422`
  validation response, consistent with other `/api/v1` parameter validation
  (e.g. a non-integer `canonical_player_id` on the by-ID lookup above).
* An empty (`search=`) or whitespace-only value is rejected with a
  structured `422` application error:
  `{"error": {"code": "search_required", "message": "A non-blank search query parameter is required."}}`.

### Response

```json
{
  "players": [
    {
      "canonical_player_id": 396,
      "display_name": "Josh Daicos",
      "current_team": {"team_id": 3, "name": "Collingwood"},
      "identifiers": {
        "afl_player_id": 1321,
        "champion_data_player_id": "CD_I1005054"
      }
    },
    {
      "canonical_player_id": 584,
      "display_name": "Nick Daicos",
      "current_team": {"team_id": 3, "name": "Collingwood"},
      "identifiers": {
        "afl_player_id": 5501,
        "champion_data_player_id": "CD_I1023261"
      }
    }
  ]
}
```

### Errors and status codes

| Status | Stable application code / meaning |
| --- | --- |
| `200` | Search executed, `players` holds zero or more matches. |
| `401` | Missing or invalid API key. Message: `Invalid or missing API Key`. |
| `422` | Missing `search` (framework validation), or blank/whitespace-only `search` (`search_required` application error). |

## Scope

These endpoints resolve or search canonical players by ID or name. They do
not provide season/match stat history, injury data, roster/lineup
information, fuzzy/phonetic/AI matching, or provider-ID-specific lookup
routes — those remain out of scope for this resource. They also make no
change to the existing
[match player-stat endpoint](api_v1_player_stats.md) or to the legacy
unversioned `/api/players` routes, which continue to read the legacy
`players` table unchanged.
