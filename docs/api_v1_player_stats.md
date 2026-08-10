# Canonical player-stat API (`/api/v1`) — consumer reference

**Audience:** API consumers. For the collection-side contract (how
`cfs_player_stats` is populated), see
[Match player-stat collection](match_player_stats.md) and
[Player-stat persistence and authority contract](architecture/player_stats_storage_contract.md).
For the full accepted design, see
[Canonical CFS player-stat read API design](architecture/api/player_stats_api_design.md).
For the broader human-led API target, see the
[Consumer API workflow design](architecture/workflows/consumer_api_design.md).

## Endpoint

```text
GET /api/v1/matches/{match_id}/player-stats
```

Returns authoritative Champion Data (CFS) player statistics for one match,
resolved from the existing numeric `matches.match_id` identifier space (the
same identifier already used by `GET /api/matches/{match_id}`). Authentication
reuses the existing scheme: send `X-Api-Key: <your key>`.

### Path parameter

| Name | Type | Description |
| --- | --- | --- |
| `match_id` | integer | The canonical/legacy numeric match identifier (`matches.match_id`). |

### Query parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `side` | `home` \| `away` | No | Return only players on one side of the match. Any other value returns `422`. |
| `champion_data_player_id` | string | No | Return only the single player with this opaque Champion Data player ID. |

### Status codes

| Status | Meaning |
| --- | --- |
| `200` | Match found. `players` may be empty — see [Lifecycle and freshness](#lifecycle-and-freshness) below for what an empty array means. |
| `401` | Missing/invalid `X-Api-Key`, or `422` if the header is omitted entirely — identical to every other route in this API (`verify_api_key` is reused unchanged). |
| `404` | No match exists with the given `match_id`. |
| `422` | An invalid `side` value was supplied. |

## Example request/response

```bash
curl -H "X-Api-Key: $AFL_API_KEY" \
  "https://<host>/api/v1/matches/8216/player-stats?side=home"
```

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

## Response fields

`match.status` is the current canonical `matches.status`; it can briefly
disagree with an individual `players[].resolved_match_status` right after a
lifecycle transition (see [Lifecycle and freshness](#lifecycle-and-freshness)).

Any identifier this API cannot resolve from an existing validated crosswalk is
returned as `null` — never guessed or synthesised. In particular:

* `canonical_player_id` is `null` when the Champion Data player has no
  `player_provider_ids(provider='champion_data')` crosswalk yet.
* `afl_player_id` is `null` whenever `canonical_player_id` is `null`, or when a
  resolved canonical player has no `player_provider_ids(provider='afl')` row.
* `display_name` is `null` when the canonical player has no usable name.
* `team_id` (the numeric `afl_teams.afl_id` for that player's side) is `null`
  when the match has no resolved participant for that side. It is **not** a
  club code or name — there is no validated crosswalk from team to club yet.

Only the eight canonical stat fields are returned: `goals`, `behinds`,
`kicks`, `handballs`, `disposals`, `marks`, `tackles`, `hitouts`. Provider
fields outside this set, and the internal `raw_player_json` forensic column,
are never exposed by this endpoint at any stage.

## Lifecycle and freshness

Every stat row carries a per-row `snapshot_authority` (`1` = live/partial or
unknown, `2` = concluded/authoritative). Match-level `lifecycle.finality` is
computed **fresh on every request** from the full current row set and is one
of:

* `"final"` — authoritative (`snapshot_authority=2`) rows exist for both
  sides, and no row is at a lower authority. Safe to treat as settled.
* `"partial"` — authoritative rows exist, but coverage is mixed (only one
  side, or a mix of authority levels). Numbers may still change.
* `"not_available"` — no authoritative rows exist yet. This covers a future
  match with no resolved `match_provider_id`, a match not yet collected, and a
  match with only live/partial rows so far. **`players` can still be
  non-empty in this state** — read it for a live view, but treat any field as
  provisional until `finality` becomes `"final"`.

`lifecycle.authoritative_rows`, `authoritative_sides`,
`min_snapshot_authority`, and `max_snapshot_authority` are the underlying
evidence, so a consumer can distinguish "nothing collected yet" from
"one-sided" from "mixed authority" instead of only seeing one enum value.

This endpoint never fetches from Champion Data on demand — it only reflects
whatever the scheduler has already persisted to `cfs_player_stats`. Do not
poll faster than the live collection cadence (60 seconds per live match, see
[`match_window_planner.md`](architecture/match_window_planner.md)); polling
faster will not return fresher data.

## Versioning policy

* `/api/v1` is additive-only: new optional fields or filters may be added
  without a version bump.
* Any breaking change (removed/renamed field, changed type or meaning,
  changed default filter behaviour) ships as `/api/v2` with a deprecation
  window for `/api/v1` — never as an in-place change.
* The unversioned `/api/...` routes (including `GET /api/player-stats` over the
  separate legacy `player_stats` table) are pre-v1 behaviour. This endpoint
  does not change them, but they may be retired once the
  [legacy capability checklist](architecture/workflows/consumer_api_design.md#16-legacy-capability-inventory-and-migration-checklist)
  is satisfied; they are not a permanently supported parallel contract.

## Related documentation

* [Canonical CFS player-stat read API design](architecture/api/player_stats_api_design.md)
  — the full accepted design this endpoint implements.
* [Consumer API workflow design](architecture/workflows/consumer_api_design.md)
  — the broader target that governs future v1 evolution.
* [AFL data authority and identity map](architecture/data_authority_map.md)
  — the general identifier/crosswalk rules this endpoint follows.
* [Player-stat persistence and authority contract](architecture/player_stats_storage_contract.md)
  — why `cfs_player_stats` is authoritative and `player_stats` is not.
