# Canonical match-interchange API (`/api/v1`) — consumer reference

**Audience:** API consumers. See
[Production CFS match-interchange persistence and consumer API design](architecture/api/interchange_api_design.md)
for the full implementation design, the array-membership semantic
investigation, and known limitations this reference summarises for
consumer use.

```text
GET /api/v1/matches/{match_id}/interchanges
GET /api/v1/matches/{match_id}/interchanges/events
```

Returns current per-player CFS matchInterchange state, and its meaningful
transition history, for one canonical match — backed by production
persistence (Issue #204, promoted from the Issue #193 diagnostic
investigation), not the separate diagnostic evidence table. Send an API key
in `X-Api-Key`; auth behaves identically to every other `/api/v1` route.

## Important: read this before using `on_interchange_list`

The only real evidence checked into this repository and reviewed when this
endpoint was promoted to production is a single captured **concluded**-match
snapshot — no live poll-to-poll sequence is checked in showing array
membership actually change as a player rotates on and off the ground during
a match. (This describes what was reviewable during promotion, not a claim
that the `interchange` diagnostic profile never observed a live match: any
observations it wrote during an actual live deployment run live only in
that deployment's own local `match_interchange_evidence_observations` table
-- `.gitignore`'d SQLite state, never committed to this repository -- and
were not available for review here. See the design doc §2.1.1 for the
specific questions such evidence would need to answer, and how to supply it
for re-review.) `on_interchange_list` is therefore a **conservative,
source-derived fact** ("this player's Champion Data id appeared in the
source `homeInterchange[]`/`awayInterchange[]` array as of the most recent
poll"), **not a confirmed "this player is currently off the ground"
signal**. Treat it as best-effort pending further live-match verification,
and do not build logic that assumes it is an authoritative bench/on-ground
state.

## `GET /api/v1/matches/{match_id}/interchanges`

Current state for every player observed in either interchange array at any
point in the match, including players who have since left the array — they
remain in the response with `on_interchange_list: false` and their last
known field values, rather than disappearing.

### Parameters

| Name | Location | Type | Required | Description |
| --- | --- | --- | --- | --- |
| `match_id` | path | integer | Yes | Canonical `matches.match_id` — the same identifier accepted by [`GET /api/v1/matches/{match_id}/player-stats`](api_v1_player_stats.md) and [`GET /api/v1/matches/{match_id}/commentary`](api_v1_commentary.md). Consumers never need a Champion Data match id. |
| `side` | query | string | No | Filter to one side (`home` or `away`). |
| `player_id` | query | integer | No | Filter to one canonical player identifier (the same `canonical_player_id` used by [`GET /api/v1/players/{canonical_player_id}`](api_v1_players.md)). |
| `on_interchange_list_only` | query | boolean | No | When `true`, return only players currently present in an interchange array. Defaults to `false`. |

### Response

```json
{
  "match": {
    "match_id": 9201,
    "match_provider_id": "CD_M20260142001",
    "round_id": 1,
    "season_id": 85,
    "status": "LIVE"
  },
  "interchanges": [
    {
      "champion_data_player_id": "CD_I1031792",
      "canonical_player_id": 501,
      "display_name": "Finnbar Maley",
      "side": "home",
      "team_id": 10,
      "champion_data_team_id": "CD_T10",
      "on_interchange_list": true,
      "interchange_count": 8,
      "bench_reason": "ROTATION",
      "time_on_ground_seconds": 4697,
      "time_on_bench_seconds": 568,
      "power_rating": 5,
      "first_observed_at": "2026-08-24T09:15:03.120000+00:00",
      "observed_at": "2026-08-24T09:31:03.120000+00:00"
    }
  ]
}
```

Field notes:

* **`champion_data_player_id`** is always present (source fact); **`canonical_player_id`**/**`display_name`** are `null` when that Champion Data id has no known crosswalk yet — never guessed from name or jumper number.
* **`on_interchange_list`** — see the callout above. Refreshed every poll.
* **`interchange_count`**, **`bench_reason`**, **`time_on_ground_seconds`**, **`time_on_bench_seconds`**, **`power_rating`** are persisted and returned exactly as supplied by CFS. `bench_reason` is never inferred (e.g. never turned into "injury" or "substitution") — it is either the source's literal string (`"ROTATION"` is the only value observed to date) or `null` when CFS did not supply one.
* **`first_observed_at`**/**`observed_at`** are UTC poll-observation timestamps, not exact game-clock instants — `matchInterchange` supplies no `periodNumber`/`periodSeconds` to build one from.
* A valid `match_id` with no interchange data yet (or ever) returns `200` with an empty `interchanges` collection — this is not an error.

## `GET /api/v1/matches/{match_id}/interchanges/events`

Chronological (oldest-first) history of **meaningful** transitions only. A
poll where only `time_on_ground_seconds`/`time_on_bench_seconds`/
`power_rating` changed never produces an event — use the current-state
route above for those values.

### Parameters

| Name | Location | Type | Required | Description |
| --- | --- | --- | --- | --- |
| `match_id` | path | integer | Yes | Same canonical match id as above. |
| `player_id` | query | integer | No | Filter to one canonical player identifier. |
| `event_type` | query | string | No | Filter to one of `appeared`, `disappeared`, `interchange_count_changed`, `bench_reason_changed`. |

### Response

```json
{
  "match": {
    "match_id": 9201,
    "match_provider_id": "CD_M20260142001",
    "round_id": 1,
    "season_id": 85,
    "status": "LIVE"
  },
  "events": [
    {
      "id": 17,
      "match_id": 9201,
      "champion_data_player_id": "CD_I1031792",
      "canonical_player_id": 501,
      "display_name": "Finnbar Maley",
      "side": "home",
      "team_id": 10,
      "champion_data_team_id": "CD_T10",
      "event_type": "interchange_count_changed",
      "interchange_count": 9,
      "previous_interchange_count": 8,
      "bench_reason": "ROTATION",
      "previous_bench_reason": "ROTATION",
      "time_on_ground_seconds": 5012,
      "time_on_bench_seconds": 568,
      "power_rating": 5,
      "observed_at": "2026-08-24T09:34:10.442000+00:00"
    }
  ]
}
```

Field notes:

* **`id`** is a stable identifier AFL-api generates for this event row — not a Champion Data identifier.
* **`event_type`** is one of `appeared` (a player's row is newly created, or transitions from off-list back onto the list), `disappeared` (a previously on-list player is missing from a poll whose array for that side was known), `interchange_count_changed`, `bench_reason_changed`.
* **`previous_interchange_count`**/**`previous_bench_reason`** are populated on the matching `*_changed` event type; otherwise they mirror the current value (no meaningful prior value to show, e.g. on `appeared`).
* **`time_on_ground_seconds`**/**`time_on_bench_seconds`**/**`power_rating`** on an event row are a **non-triggering context snapshot** at the moment the event fired — never themselves the reason the event exists.
* **`observed_at`** is the UTC time AFL-api's poll detected the transition — the poll-observation time, not an exact in-game clock instant. See the design doc §4 for how this can still be approximately correlated with match-period/commentary timelines via nearest UTC timestamp.
* A valid `match_id` with no transitions yet returns `200` with an empty `events` collection.

## Errors and status codes

Application errors use the common `/api/v1` shape:

```json
{"error": {"code": "match_not_found", "message": "Match not found."}}
```

| Status | Stable application code / meaning |
| --- | --- |
| `200` | The match exists; the collection holds zero or more rows. |
| `404` | `match_not_found` when no `matches` row matches `match_id`. |
| `401` | Missing or invalid API key. |
| `422` | Framework request/parameter validation (e.g. a non-integer `match_id` or `player_id`, or an invalid `side`/`event_type` value). |

## What this endpoint is not

* **Not a confirmed on-ground/off-ground signal.** See the callout above —
  `on_interchange_list` is a conservative source-membership fact, pending
  further live-match verification.
* **Not authoritative for match finality, lifecycle, or player
  statistics.** Match state comes from `matches.status`; player statistics
  come from [`GET /api/v1/matches/{match_id}/player-stats`](api_v1_player_stats.md).
* **Not an inference engine.** `bench_reason` is never turned into an
  injury/substitution/tactical/medical interpretation.
* **Not a write API.**
* **Not backed by the diagnostic evidence table** from Issue #193
  (`match_interchange_evidence_observations`). That remains separately
  available for internal debugging/investigation, opt-in via
  `AFL_DIAGNOSTICS_ENABLED`, and is never exposed through `/api/v1`.

## Scope

This resource resolves interchange state for one match. It does not
provide cross-match search, live push/streaming updates, or
fantasy-league-specific interpretation of bench state (e.g. scoring or
lineup-legality implications) — those remain out of scope for AFL-api and,
where applicable, are a downstream consumer concern (e.g.
BBBFFL-specific semantics).
