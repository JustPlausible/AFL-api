# Canonical match player-stat API (`v1`)

`GET /api/v1/matches/{match_id}/player-stats` is the stable consumer endpoint
for the latest player statistics persisted from Champion Data's CFS source for
one match. It is read-only: a request never contacts CFS or triggers collection.

The older `GET /api/player-stats` endpoint remains a separate compatibility
surface backed by legacy HTML-scraped data. Consumers that need authoritative
CFS observations should use the versioned endpoint documented here.

## Authentication and request

Send the existing API credential in the `X-Api-Key` header:

```console
curl -H 'X-Api-Key: YOUR_KEY' \
  'https://example.test/api/v1/matches/8216/player-stats?side=home'
```

`match_id` is the numeric ID exposed by the existing match API. Two optional
query parameters may narrow the result:

| Parameter | Accepted value | Meaning |
| --- | --- | --- |
| `side` | `home` or `away` | Return only observations for that side. |
| `champion_data_player_id` | Opaque string | Return only the player with that CFS identity. |

Filters affect only `players`. The `lifecycle` block always describes the
complete persisted row set for the match so its finality cannot be mistaken
for the finality of a filtered subset.

## Response

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

Only the eight fields shown under `stats` are stable canonical statistics.
Provider-shaped extra fields and raw player payloads are deliberately not
returned. Numeric statistics can be integers or non-integer JSON numbers, and
individual values can be `null` when the source did not publish them.

Identity is never guessed. `canonical_player_id`, `afl_player_id`, and
`display_name` are `null` when their validated mappings are unavailable.
`team_id` is the numeric AFL team identity resolved from the player's match
side; it is not an editorial club identity.

## Finality and freshness

`lifecycle.finality` describes the complete current snapshot:

* `final`: concluded-authority rows cover both sides and all persisted rows
  have the same concluded authority;
* `partial`: at least one concluded-authority row exists, but authority is
  mixed across rows or only one side has concluded-authority coverage;
* `not_available`: no concluded-authority row exists. `players` may still
  contain live observations with `snapshot_authority: 1`.

The evidence fields report concluded-authority row and side counts and the
minimum/maximum authority across every persisted observation. A player's
`snapshot_authority`, `resolved_match_status`, and `collected_at` describe that
specific observation. `match.status` is the independently refreshed canonical
match lifecycle, so it can briefly be ahead of a player's persisted status.

The API reflects the latest scheduler-persisted state and does not fetch data
on demand. Consumers should not poll faster than once every 60 seconds for a
live match, the default live collection cadence. Stop live polling when
`finality` becomes `final`; a non-final response can still change.

## Status codes

| Status | Meaning |
| --- | --- |
| `200` | Match found. `players` can be empty when no provider identity or persisted observations are available. |
| `401` | The supplied API key is invalid. |
| `404` | No match exists for the numeric `match_id`. |
| `422` | A required header/path value is missing or invalid, or `side` is not `home`/`away`. |

## Versioning policy

The `/api/v1` response is additive-only: new optional fields or filters may be
added compatibly. Removing or renaming a field, changing its type or meaning,
or changing filter defaults requires a new API version and a documented
deprecation window. Existing unversioned `/api/...` endpoints are frozen
compatibility routes; they are not aliases of this endpoint and their backing
sources and response shapes are unchanged.
