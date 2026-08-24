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

## Evidence behind `on_bench`

`on_bench` means the player is currently on the interchange bench (off the
ground), as of the most recent poll. This is confirmed, not a guess: real
Round 24 live diagnostic observations across **7 matches**
(`CD_M20260142401`, `CD_M20260142403`–`CD_M20260142406`, `CD_M20260142408`,
`CD_M20260142409`), polled roughly every 15 seconds through each match's
full LIVE window, show `homeInterchange[]`/`awayInterchange[]` membership
(an exact Champion Data `playerId` set, not an inference) changing
continuously and repeatedly — hundreds of paired appear/disappear events
per match — tightly correlated with each team's own `totalInterchangeCount`
incrementing. This rules out the alternative reading this endpoint
originally had to leave open (a fixed, always-listed bench pool that never
actually changes). See the design doc §2.1 for the full evidence and
citations.

A follow-up, individually-cited real evidence set (a full per-poll export
for `CD_M20260142409`) confirmed two more things directly: Champion Data
player `CD_I1028561` ("Tom Gross") genuinely appears, disappears, and
reappears in `homeInterchange[]` five separate times across that one match
— a real, named round trip, not just an aggregate set-membership change —
and `matchInterchange` state (every field, not just membership) freezes
byte-for-byte across 40 real `POSTGAME` polls spanning ~10 minutes after
full-time, with zero further transitions.

One thing remains **not** confirmed, and `on_bench` should be read with
this in mind:

* **`CONCLUDED` behaviour.** The reviewed evidence confirms `on_bench`
  through `LIVE` and at least 10 minutes of `POSTGAME` (frozen, not stale —
  see above); no match's capture has yet reached `CONCLUDED`, so whether
  the endpoint stays queryable/frozen or becomes unavailable at that point
  is still unverified.

## `GET /api/v1/matches/{match_id}/interchanges`

Current state for every player observed in either interchange array at any
point in the match, including players who have since left the array — they
remain in the response with `on_bench: false` and their last known field
values, rather than disappearing.

### Parameters

| Name | Location | Type | Required | Description |
| --- | --- | --- | --- | --- |
| `match_id` | path | integer | Yes | Canonical `matches.match_id` — the same identifier accepted by [`GET /api/v1/matches/{match_id}/player-stats`](api_v1_player_stats.md) and [`GET /api/v1/matches/{match_id}/commentary`](api_v1_commentary.md). Consumers never need a Champion Data match id. |
| `side` | query | string | No | Filter to one side (`home` or `away`). |
| `player_id` | query | integer | No | Filter to one canonical player identifier (the same `canonical_player_id` used by [`GET /api/v1/players/{canonical_player_id}`](api_v1_players.md)). |
| `on_bench_only` | query | boolean | No | When `true`, return only players currently on the interchange bench. Defaults to `false`. |

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
      "on_bench": true,
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
* **`on_bench`** — see the evidence section above. Refreshed every poll.
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

* **Not verified for CONCLUDED.** `on_bench` is confirmed against real
  LIVE-play and POSTGAME evidence; see the evidence section above for the
  one residual open question once a match reaches CONCLUDED.
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
