# Canonical match-commentary API (`/api/v1`) — consumer reference

**Audience:** API consumers. See
[Production CFS match-commentary persistence and consumer API design](architecture/api/commentary_api_design.md)
for the full implementation design, event-identity semantics, and known
limitations this reference summarises for consumer use.

```text
GET /api/v1/matches/{match_id}/commentary
```

Returns normalized commentary events for one canonical match, backed by
production persistence (Issue #201) — not the separate diagnostic evidence
tables from Issue #196. Send an API key in `X-Api-Key`; auth behaves
identically to every other `/api/v1` route.

## Parameters

| Name | Location | Type | Required | Description |
| --- | --- | --- | --- | --- |
| `match_id` | path | integer | Yes | Canonical `matches.match_id` — the same identifier accepted by [`GET /api/v1/matches/{match_id}/player-stats`](api_v1_player_stats.md). Consumers never need a Champion Data match id. |
| `period` | query | integer | No | Filter to one `period_number` (`0` for pre-match commentary, `1`–`4` for regulation quarters). |
| `player_id` | query | integer | No | Filter to one canonical player identifier (the same `canonical_player_id` used by [`GET /api/v1/players/{canonical_player_id}`](api_v1_players.md)). |
| `team_id` | query | integer | No | Filter to one canonical team identifier. |
| `score_events_only` | query | boolean | No | When `true`, return only events with `score_event=true`. Defaults to `false`. |

## Response

```json
{
  "match": {
    "match_id": 9101,
    "match_provider_id": "CD_M20260142409",
    "round_id": 1,
    "season_id": 85,
    "status": "POSTGAME"
  },
  "events": [
    {
      "id": 42,
      "match_id": 9101,
      "period_number": 1,
      "period_seconds": 59,
      "comment": "GOAL - Hawks (Jack Gunston)",
      "score_event": true,
      "player": {"id": 501, "name": "Jack Gunston", "provider_id": "CD_I291351"},
      "team": {"id": 80, "name": "Hawthorn", "provider_id": "CD_T80"},
      "observed_at": "2026-08-23T09:21:29.024399+00:00",
      "possible_edit_of_event_id": null
    }
  ]
}
```

Field notes:

* **`events` ordering is chronological, oldest-first** by default
  (`period_number` then `period_seconds` ascending) for consumer usability
  — even though the upstream CFS feed itself is observed newest-first.
  Where two events share the exact same `(period_number, period_seconds)`,
  ordering falls back to original source-array position as a
  documented-best-effort tiebreaker (see the design doc §5.2).
* **`id`** is a stable identifier AFL-api generates for this event row. It
  is **not** a Champion Data identifier — the source feed supplies none.
* **`comment`** and **`score_event`** are persisted and returned exactly as
  supplied by the source. No goal/behind/points outcome is parsed from the
  comment text; `score_event` is the only structured scoring fact CFS
  provides.
* **`player`/`team`** are `null` when the source event carries no
  `playerId`/`teamId` at all (e.g. most narrative commentary, quarter
  markers), or when that provider id has no known canonical crosswalk yet.
  Never guessed or inferred from `comment`. When present, `provider_id` is
  the source Champion Data id preserved alongside the resolved canonical
  `id`/`name`.
* **`possible_edit_of_event_id`** is a heuristic, non-authoritative link to
  an earlier event this one likely republishes or corrects — most notably
  an official score-review reversal (a `GOAL` later followed by a `BEHIND`,
  or vice versa, at the identical match-clock/player/team/`score_event`
  combination). **Both** events always remain in the response; this field
  never causes the earlier event to be merged, hidden, or removed — AFL-api
  represents what the source feed actually published over time. `null` when
  no such link was detected.
* A valid `match_id` with no commentary yet (or ever) returns `200` with an
  empty `events` collection — this is not an error.

## Errors and status codes

Application errors use the common `/api/v1` shape:

```json
{"error": {"code": "match_not_found", "message": "Match not found."}}
```

| Status | Stable application code / meaning |
| --- | --- |
| `200` | The match exists; `events` holds zero or more commentary rows. |
| `404` | `match_not_found` when no `matches` row matches `match_id`. |
| `401` | Missing or invalid API key. |
| `422` | Framework request/parameter validation (e.g. a non-integer `match_id`, `period`, `player_id`, or `team_id`). |

## What this endpoint is not

* **Not authoritative for match finality, lifecycle, or player
  statistics.** Match state comes from `matches.status`/
  `afl_json.match_status`; player statistics come from
  [`GET /api/v1/matches/{match_id}/player-stats`](api_v1_player_stats.md).
  Commentary text and `score_event` are source facts about what CFS
  published, never a substitute for either.
* **Not a write API.** There is no way to POST commentary through this or
  any other `/api/v1` route — AFL-api only ever ingests from CFS. See the
  design doc §7.1 for the internal-only replay mechanism used for
  development/testing.
* **Not backed by the diagnostic evidence tables** from Issue #196
  (`commentary_evidence_polls`/`commentary_evidence_events`). Those remain
  separately available for internal debugging/replay investigation, opt-in
  via `AFL_DIAGNOSTICS_ENABLED`, and are never exposed through `/api/v1`.

## Scope

This resource resolves commentary events for one match. It does not provide
cross-match search, a global commentary feed, live push/streaming updates,
or fantasy-league-specific interpretation of scoring events — those remain
out of scope for AFL-api and, where applicable, are a downstream consumer
concern (e.g. BBBFFL-specific semantics).
