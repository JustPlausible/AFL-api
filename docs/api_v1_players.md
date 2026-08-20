# Canonical player lookup API (`/api/v1`) — consumer reference

**Audience:** API consumers. The authoritative workflow is the
[Consumer API workflow design](architecture/workflows/consumer_api_design.md),
which establishes canonical player identity, provider crosswalks, and
current-season team context as the design this endpoint implements.

## Endpoint and authentication

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

## Errors and status codes

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

## Scope

This endpoint resolves one canonical player by ID. It does not provide name
search or listing, season/match stat history, injury data, or roster/lineup
information — those remain out of scope for this resource. It also makes no
change to the existing
[match player-stat endpoint](api_v1_player_stats.md) or to the legacy
unversioned `/api/players` routes, which continue to read the legacy
`players` table unchanged.
