# Canonical v1 match navigation

The match resources complete the versioned consumer navigation path:

```text
GET /api/v1
  → GET /api/v1/seasons
  → GET /api/v1/seasons/{season_id}/rounds
  → GET /api/v1/rounds/{round_id}/matches
  → GET /api/v1/matches/{match_id}
  → GET /api/v1/matches/{match_id}/player-stats
```

All requests require the shared API key in the `X-API-Key` header.

## List a round's matches

`GET /api/v1/rounds/{round_id}/matches` returns:

```json
{
  "matches": [
    {
      "match_id": 8216,
      "round_id": 1301,
      "season_id": 85,
      "status": "CONCLUDED",
      "start_time_utc": "2026-08-02T00:00:00+00:00",
      "home_team": {"team_id": 10, "name": "Home team"},
      "away_team": {"team_id": 11, "name": "Away team"},
      "score_home": 88,
      "score_away": 72
    }
  ]
}
```

The route first validates `round_id` against canonical round persistence. An
unknown round returns `404 round_not_found`; an existing round with no persisted
matches returns `200` and `{"matches": []}`.

Matches with a known `start_time_utc` appear first, ordered by that timestamp
ascending. Matches with an unknown start follow. `match_id` ascending is the
stable tie-breaker in both groups.

## Get one match

`GET /api/v1/matches/{match_id}` returns the same match object used in the
collection. An unknown identifier returns the shared structured
`404 match_not_found` application error. The returned `match_id` is exactly the
identifier accepted by `GET /api/v1/matches/{match_id}/player-stats`; there is
no separate fixture or provider identifier in the public contract.

## Field semantics

- `match_id`, `round_id`, and `season_id` are canonical numeric identifiers.
- `status` is the persisted match lifecycle status. It is not a live-update or
  polling guarantee.
- `start_time_utc` is the persisted UTC scheduled start, or `null` when unknown.
- `home_team` and `away_team` contain only canonical `team_id` and canonical
  persisted `name`. Each side is `null` if its `matches.home_team_id` or
  `matches.away_team_id` cannot resolve to `afl_teams.afl_id`; provider payload
  names are never substituted.
- `score_home` and `score_away` are persisted scores and remain `null` when not
  available. They are not derived from player statistics.

Inspection of the persisted bootstrap shape found that `venue_json` stores the
provider venue object (typically identifiers and names), while a local
`startTime` can occur separately in some upstream match records and an IANA
timezone is not consistently present. Accordingly, this contract deliberately
ships only `start_time_utc`: it does not infer timezone or local time, and it
does not expose `venue_json` or other raw provider/collector payloads.

Application errors use the shared shape:

```json
{"error": {"code": "match_not_found", "message": "Match not found."}}
```

