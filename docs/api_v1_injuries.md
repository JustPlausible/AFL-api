# Canonical current-injury API (`/api/v1/injuries`) — consumer reference

**Audience:** API consumers. Issue #213 introduced this resource so current
AFL injury data can be consumed using the same canonical player/team identity
already established by [`GET /api/v1/players/{canonical_player_id}`](api_v1_players.md).

```text
GET /api/v1/injuries
GET /api/v1/injuries?team_id=<canonical_team_id>
GET /api/v1/injuries?canonical_player_id=<canonical_player_id>
```

Send an API key in `X-Api-Key`. A missing or invalid API key receives `401`
with `{"detail": "Invalid or missing API Key"}`, matching every other
`/api/v1` route. No capability beyond standard authentication is required.

## Parameters

| Name | Location | Type | Required | Description |
| --- | --- | --- | --- | --- |
| `team_id` | query | integer | No | Filter to one canonical AFL team id -- the same identifier used by `home_team`/`away_team` on [the match resource](api_v1_matches.md). |
| `canonical_player_id` | query | integer | No | Filter to one canonical AFL-api player id -- the same identifier returned by `GET /api/v1/players/{canonical_player_id}`. |

Both filters are optional. Supplying both is conjunctive (`AND`): a
combination naming a player who does not belong to that team deterministically
returns an empty `injuries` list, not an error, consistent with every other
`/api/v1` filter that matches no rows.

## Response

```json
{
  "injuries": [
    {
      "canonical_player_id": 584,
      "player": {"display_name": "Nick Daicos"},
      "team": {"team_id": 3, "name": "Collingwood"},
      "injury": "Knee",
      "estimated_return": "Test",
      "source_updated": "August 18, 2026",
      "observed_at": "2026-08-24T00:00:00+00:00",
      "current": true
    }
  ]
}
```

Field notes:

* `canonical_player_id` is the primary consumer identity and is always
  present -- the same identifier accepted by `GET
  /api/v1/players/{canonical_player_id}`. A source row whose player identity
  did not resolve at collection time (unresolved or ambiguous) has no safe
  canonical identity to expose and is **omitted** from this resource
  entirely, rather than returned under an invented or provider-only
  identity. This resource never introduces a second, competing player
  identity representation.
* `player.display_name` is the canonical player's resolved display name
  (falling back to `given_name`/`family_name`, the same rule used by the
  player resource), or `null` when not yet resolved.
* `team` is the canonical team identity resolved from the source club marker
  at collection time, or `null` when that marker did not resolve to a
  canonical team. It is never guessed from the player's name or history.
* `injury` and `estimated_return` are the source injury description and
  estimated-return text, persisted verbatim -- never normalised, parsed into
  a structured duration, or guessed when absent.
* `source_updated` is the source page's own `Updated:` text for that
  player's team block at the time it was collected, or `null` when the
  source omitted it. It is source-supplied free text (e.g. `"August 18,
  2026"`), not a parsed date.
* `observed_at` is the UTC time this row was last (re)collected -- i.e. when
  the collector last saw this player listed on the source page.
* `current` reflects whether this is the player's latest observed injury
  state. Every row returned by this resource today has `current: true`; see
  Scope below.

## Ordering

Results are ordered by canonical `team_id` ascending (rows with no resolved
team sort last), then by `canonical_player_id` ascending -- deterministic
regardless of insertion or collection order.

## Errors and status codes

| Status | Stable application code / meaning |
| --- | --- |
| `200` | The request is valid; `injuries` holds zero or more current rows. |
| `401` | Missing or invalid API key. Message: `Invalid or missing API Key`. |
| `422` | Framework request/parameter validation, such as a `team_id` or `canonical_player_id` outside the supported integer range. |

A valid request that matches no rows -- including an empty database, a
`team_id`/`canonical_player_id` with no current injuries, or a filter
combination naming inconsistent identities -- returns `200` with
`{"injuries": []}`, never an error.

## Source-coverage semantics (read this before treating an absence as meaningful)

**A team not appearing in this resource's results does not mean that team has
zero injuries.** The upstream AFL injury page is not always authoritative for
every AFL team -- during finals, for example, it has been observed to list
only the teams still competing. The collector only ever expires a team's
previously-current rows when that team was actually observed (present, with
a resolving club marker) on the latest source page; a team's absence from the
page leaves its last-known state untouched rather than clearing it. See
[`docs/architecture/injury_collector_pipeline.md`](architecture/injury_collector_pipeline.md)
for the full persistence semantics, and
[`docs/scraper_source_inventory.md`](scraper_source_inventory.md) for the
acquisition-method evidence.

This resource itself has no notion of "team known to have no injuries" versus
"team not currently observed" -- that distinction lives in collection
provenance (the scrape-run audit's `diagnostic_summary`, per the architecture
doc above), not in this per-row read contract. A consumer that needs to know
whether a specific team was actually covered by the most recent collection
should not infer it from this endpoint's absence of rows for that team.

## Scope

This resource returns **current injuries only**. Historical injury querying
(e.g. "what was this player's injury history over a season") is explicitly
out of scope for Issue #213 and may be addressed as a follow-up; the
underlying `injuries` table retains non-current rows (`current = 0`), but no
`/api/v1` route exposes them yet. This resource also does not expose raw
source/provenance fields (source URL, AFL numeric id, source club code) --
those remain internal to collection and its audit trail, not part of the
consumer contract.
