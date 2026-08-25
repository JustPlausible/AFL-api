# Canonical match-roster API (`/api/v1`) — consumer reference

**Audience:** API consumers. See [`docs/match_rosters.md`](match_rosters.md)
for the collector/persistence implementation detail (source contract,
replacement-safety rules, and unresolved semantics) this reference
summarises for consumer use, and
[`docs/architecture/data_authority_map.md`](architecture/data_authority_map.md)
for how this resource relates to the separate legacy HTML `lineups` model.

```text
GET /api/v1/matches/{match_id}/rosters
```

Returns the current canonical CFS `matchRosters` selection for one canonical
match — backed by production persistence (Issue #219), not a live CFS call
and not the separate legacy rendered-HTML `lineups` compatibility route/table
(see "Legacy `lineups` boundary" below). Send an API key in `X-Api-Key`; auth
behaves identically to every other `/api/v1` route.

## Selection is not participation

**A roster selection is a team's choice, not evidence the player took the
field.** Use
[`GET /api/v1/matches/{match_id}/player-stats`](api_v1_player_stats.md) for
actual match participation and statistics. Nothing in this resource's
response — `selections`, `ins`, `outs`, `late_changes`, `club_debuts`, or
`milestones` — should be read as confirming a player played, was
interchanged, or contributed statistics.

## Response shape

```json
{
  "match": {
    "match_id": 9201,
    "match_provider_id": "CD_M20260142001",
    "round_id": 1,
    "season_id": 85,
    "status": "SCHEDULED"
  },
  "metadata": {
    "match_status_at_observation": "PUBLISHED",
    "source_updated_at": "2026-07-25T08:30:00Z"
  },
  "home_team": {
    "team": {"team_id": 10, "name": "Adelaide Crows"},
    "champion_data_team_id": "CD_T10",
    "team_status": "CONFIRMED",
    "selections": [
      {
        "player": {
          "champion_data_player_id": "CD_I1",
          "canonical_player_id": 501,
          "display_name": "Ada Able"
        },
        "position": "FORWARDS",
        "jumper_number": 7,
        "captain": true
      }
    ],
    "context": {
      "ins": [
        {
          "player": {
            "champion_data_player_id": "CD_I1",
            "canonical_player_id": 501,
            "display_name": "Ada Able"
          },
          "reason": "Selected"
        }
      ],
      "outs": [],
      "late_changes": [],
      "club_debuts": [],
      "milestones": []
    }
  },
  "away_team": {
    "team": {"team_id": 40, "name": "Collingwood Magpies"},
    "champion_data_team_id": "CD_T40",
    "team_status": "CONFIRMED",
    "selections": [],
    "context": {"ins": [], "outs": [], "late_changes": [], "club_debuts": [], "milestones": []}
  }
}
```

### Parameters

| Name | Location | Type | Required | Description |
| --- | --- | --- | --- | --- |
| `match_id` | path | integer | Yes | Canonical `matches.match_id` — the same identifier accepted by [`GET /api/v1/matches/{match_id}/player-stats`](api_v1_player_stats.md), [`.../commentary`](api_v1_commentary.md), and [`.../interchanges`](api_v1_interchange.md). Consumers never need a Champion Data match id. |

### `metadata`

* **`match_status_at_observation`** is the source `matchRoster.status`
  (e.g. `PUBLISHED`, `CONCLUDED`) for the most recent replacement-safe
  observation, persisted exactly as CFS supplied it — distinct from, and
  never a substitute for, the canonical `matches.status` lifecycle field.
* **`source_updated_at`** is the source `matchRoster.lastUpdated` for the
  most recent observation, or `null` when no roster has been persisted for
  this match yet.

### `home_team` / `away_team`

Either is `null` when no roster observation has been persisted for that
side yet — never inferred from the other side or from the match itself.
Each present side carries:

* **`team`** — canonical team identity (`team_id`/`name`), or `null` when
  the Champion Data team id has no resolved crosswalk yet.
* **`champion_data_team_id`** — the source team id, always present when a
  roster has been observed for that side, independent of whether `team`
  resolved.
* **`team_status`** — source `teamStatus` (e.g. `CONFIRMED`), persisted
  verbatim; never interpreted.
* **`selections`** — the selected positional lineup. `position` is the CFS
  positional-group name exactly as supplied (e.g. `FORWARDS`,
  `INTERCHANGE`) — never translated into a speculative enum. `captain` is
  the source flag exactly as supplied, or `null` when not supplied.
* **`context`** — the five supported change/context collections
  (`ins`, `outs`, `late_changes`, `club_debuts`, `milestones`), each a list
  of `{player, reason}`. Deliberately never merged into `selections`: a
  player can appear in both (e.g. selected *and* an `in`) as two separate
  records in two separate collections.

### Player identity

Every `player` object carries `champion_data_player_id` (always present, the
source fact) and `canonical_player_id`/`display_name` (`null` when that
Champion Data id has no known crosswalk yet). Canonical identity is **never
guessed from name or jumper number** — an unresolved player renders with a
`null` `canonical_player_id` and `display_name`, and self-heals to the
resolved value on a later valid roster observation once the crosswalk
exists (no separate repair call is needed).

## Replacement safety

This resource reflects only the most recent **replacement-safe** observation
persisted for each side. A previously persisted roster is never erased by:

* an unpublished/unavailable (`null`) upstream response;
* a published empty list (`[]]`) — its live semantics are not yet
  distinguished from `null`, so it is conservatively treated the same way;
* a malformed or partial upstream response;
* a transient authentication or transport failure.

A genuine later publish — a real position change, a new `in`/`out`, or a
selection updated closer to first bounce — **does** replace the prior
selection/context state for that side. See
[`docs/match_rosters.md`](match_rosters.md#replacement-and-supersession-safety-issue-219)
for the full collector/persistence-level rules this resource is built on.

## Errors and status codes

Application errors use the common `/api/v1` shape:

```json
{"error": {"code": "match_not_found", "message": "Match not found."}}
```

| Status | Stable application code / meaning |
| --- | --- |
| `200` | The match exists; `home_team`/`away_team` are populated or `null` per side. |
| `404` | `match_not_found` when no `matches` row matches `match_id`. |
| `401` | Missing or invalid API key. |
| `422` | Framework request/parameter validation (e.g. a non-integer `match_id`). |

## Legacy `lineups` boundary

This resource is backed by canonical CFS roster persistence
(`cfs_match_rosters`/`cfs_match_roster_selections`/`cfs_match_roster_context`,
migration `0024`) and **never** reads, writes, or falls back to the
separate, pre-existing rendered-HTML `lineups` table or its unversioned
compatibility routes. The two remain independent authorities — see
[`docs/architecture/data_authority_map.md`](architecture/data_authority_map.md).
A consumer that has been using the legacy HTML lineup route is not
automatically migrated by this resource's existence, and should evaluate the
two independently; the legacy route's data shape, identifiers, and
availability are unaffected by this change.

## What this endpoint is not

* **Not participation evidence.** See "Selection is not participation" above.
* **Not authoritative for match finality, lifecycle, or player statistics.**
  Match state comes from `matches.status`; player statistics come from
  [`GET /api/v1/matches/{match_id}/player-stats`](api_v1_player_stats.md).
* **Not a raw CFS payload.** Only reviewed canonical fields are exposed —
  provider-only wrapper values (`venue`, `weather`, `umpires`,
  `operationHeader`, `recentMatches`, `recentMatchScores`, `teamPlayers`) are
  persisted at collector level for traceability but are never part of this
  consumer contract.
* **Not an emergencies list.** No emergency/reserve concept is inferred from
  `teamPlayers` or any other ambiguous source structure — see
  [`docs/match_rosters.md`](match_rosters.md).
* **Not a write API.**

## Scope

This resource resolves the current roster for one match. It does not
provide cross-match search, roster history/snapshots over time, or
fantasy-league-specific interpretation of selection state (e.g. scoring or
lineup-legality implications) — those remain out of scope for AFL-api and,
where applicable, are a downstream consumer concern.
