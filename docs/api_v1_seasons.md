# AFL-api v1 discovery and seasons

The v1 discovery root and seasons resource are the starting point for the
consumer navigation hierarchy. Start at `GET /api/v1`, follow the documented
seasons resource, and use a returned `season_id` with later round and match
resources as those resources become available.

Both endpoints are read-only and require an active API key in the
`X-Api-Key` request header:

```http
X-Api-Key: your-api-key
```

A missing or invalid key returns HTTP `401`:

```json
{"detail": "Invalid or missing API Key"}
```

## Discover the API

```http
GET /api/v1
```

This deliberately small response identifies the API and points to its
generated documentation. It does not disclose database, collector, scheduler,
health, or other operational state.

```json
{
  "name": "AFL-api",
  "version": "0.5.0",
  "documentation": "/docs"
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | Public API name. |
| `version` | string | Running AFL-api release version. |
| `documentation` | string | Location of generated interactive API documentation. |

## List seasons

```http
GET /api/v1/seasons
```

The response lists the seasons persisted by canonical AFL season sync, newest
first. It does not calculate the current season or round at request time and it
does not return raw provider payloads or internal metadata.

```json
{
  "seasons": [
    {
      "season_id": 85,
      "year": 2026,
      "name": "2026",
      "is_current": true,
      "current_round_number": 1
    },
    {
      "season_id": 84,
      "year": 2025,
      "name": "2025",
      "is_current": false,
      "current_round_number": 24
    }
  ]
}
```

| Field | Type | Nullable | Meaning |
| --- | --- | --- | --- |
| `seasons` | array | No | Persisted AFL seasons, ordered by descending year and season ID. |
| `season_id` | integer | No | Numeric AFL season identifier, mapped from canonical `afl_id`. |
| `year` | integer | No | Calendar year recorded for the season. |
| `name` | string | No | Consumer-facing persisted season name. |
| `is_current` | boolean | No | Current-season indicator maintained by season sync. |
| `current_round_number` | integer | Yes | Current round maintained by season sync, or `null` when unavailable. |

The response intentionally omits provider identifiers, `metadata_json`,
`source_json`, timestamps, and competition-selection details. Version 1
currently targets the AFL men's competition and does not accept a competition
selector.

## Related resources

- [Canonical match player statistics](api_v1_player_stats.md)
- [Consumer API workflow design](architecture/workflows/consumer_api_design.md)
- Interactive OpenAPI documentation at `/docs` on a running deployment
