# Canonical v1 rounds

Round navigation is available through two authenticated endpoints:

- `GET /api/v1/seasons/{season_id}/rounds`
- `GET /api/v1/rounds/{round_id}`

Send an API key in the `X-API-Key` header. Missing or invalid credentials return
`401` through the shared API-key authentication behaviour.

## Contract

Each round contains only reviewed, persisted round facts:

| Field | Type | Meaning |
| --- | --- | --- |
| `round_id` | integer | Canonical numeric AFL round identifier. |
| `season_id` | integer | Persisted parent-season identifier. |
| `round_number` | integer or null | Provider-persisted sequence number. |
| `name` | string or null | Persisted round label, such as `Round 1`. |
| `abbreviation` | string or null | Short persisted label. |
| `start_time` | string or null | Persisted round start time. |
| `end_time` | string or null | Persisted round end time. |
| `byes` | array or null | Typed canonical teams known to have a bye. |

Each bye item has `team_id`, plus the canonical persisted `name` and
`abbreviation` (either may be null). Provider IDs, provider names, and the raw
bye payload are not exposed.

The collector persists AFL's `byes` array. Observed populated entries are
objects shaped like `{"id": 2, "providerId": "CD_T20", "name": "Bye Team"}`.
Only the numeric `id` is accepted as a canonical team identity; public names
are read from canonical team persistence rather than copied from that payload.
Duplicate IDs are collapsed in source order. An explicit persisted empty array
is returned as `[]`. A null, invalid, or non-array stored value is returned as
`null`, because bye availability is unknown. A non-empty array containing any
entry without a valid numeric team ID also returns `null`: this conservative
rule prevents either an entirely unresolvable or a partially resolved source
array from being presented as a complete bye list.

The season listing reads `rounds.season_id`; it does not derive membership from
dates or matches. Numbered results are ordered by `round_number` ascending
(including Opening Round as `0`), then `round_id` ascending as a stable
tie-breaker. Rounds whose number is unknown (`null`) appear last, ordered by
`round_id`. The response does not embed match or fixture summaries.

## Examples

```http
GET /api/v1/seasons/85/rounds
X-API-Key: your-key
```

```json
{
  "rounds": [
    {
      "round_id": 1300,
      "season_id": 85,
      "round_number": 0,
      "name": "Opening Round",
      "abbreviation": "OR",
      "start_time": "2026-03-05T08:00:00Z",
      "end_time": "2026-03-08T12:00:00Z",
      "byes": [{"team_id": 2, "name": "Dogs", "abbreviation": "DOG"}]
    }
  ]
}
```

The detail route returns that same round object without a wrapper:

```http
GET /api/v1/rounds/1301
X-API-Key: your-key
```

```json
{
  "round_id": 1301,
  "season_id": 85,
  "round_number": 1,
  "name": "Round 1",
  "abbreviation": "R1",
  "start_time": "2026-03-12T08:00:00Z",
  "end_time": "2026-03-15T12:00:00Z",
  "byes": []
}
```

An unknown `round_id` returns `404` using the shared v1 application error:

```json
{"error": {"code": "round_not_found", "message": "Round not found."}}
```

The season-scoped route first validates the canonical persisted season. A
known season with no rounds returns `200` with `{"rounds": []}`; an unknown
season returns `404` with code `season_not_found` in the same error shape.
