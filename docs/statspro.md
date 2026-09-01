# AFL StatsPro ingestion (Issue #237)

StatsPro is a maintained, authenticated AFL source family. Authentication is
transient: `AflJsonClient` obtains, caches and refreshes `WMCTok` and sends it
as `x-media-mis-token`; neither value is persisted or included in operational
logs. The maintained contracts are:

* `GET /statspro/playersStats/seasons/{season_provider_id}` with
  `includeBenchmarks=false`, empty name/position/team filters — `SEASON_TOTAL`;
* `GET /statspro/playersStats/rounds/{round_provider_id}` with an empty team
  filter — `LEAGUE_ROUND_TOTAL`.

## Authority and semantics

| Fact | Authority |
|---|---|
| Canonical player/team identity | Existing canonical identity and provider mappings |
| Live, operational match player statistics | CFS `playerStats/match` and `cfs_player_stats` |
| AFL-published finals-inclusive player season summary | StatsPro `SEASON_TOTAL` |
| AFL-published post-round bulk facts | StatsPro `LEAGUE_ROUND_TOTAL` |

StatsPro embedded profile metadata does not override canonical profiles. A
provider player ID is resolved only through `player_provider_ids`, never by
name. Unknown IDs remain retained with a null canonical identity for operator
reconciliation and are omitted from canonical consumer routes. Round facts
remain in a dedicated table and never mutate CFS match facts.

`full_season` is explicitly finals-inclusive. AFL's `totals` and `averages`
are stored and returned separately without recomputation. Null and zero remain
distinct, and zero-game listed players are valid summaries.

## Operations

After metadata/player bootstrap and database migration:

```console
python cli.py --collect-statspro-season 2025
python cli.py --collect-statspro-season 2024  # historical backfill
python cli.py --collect-statspro-round CD_R202601407
```

Commands report returned/resolved/unresolved and zero-game player counts plus
insert/update/unchanged counts. Refreshes validate the complete response before
opening an atomic persistence savepoint. Empty, malformed, HTTP, authentication
or persistence failures therefore leave the previous good snapshot intact.
There is intentionally no scheduler integration or high-frequency polling.

## Consumer API

```http
GET /api/v1/seasons/{season_id}/player-stat-summaries?scope=full_season&limit=100&offset=0
GET /api/v1/players/{canonical_player_id}/seasons/{season_id}/player-stat-summary?scope=full_season
```

The collection supports optional `team_id` and `canonical_player_id` filters;
`limit` is bounded to 1–250. Normal responses expose canonical identity,
source/context/scope, published facts and timestamps. `advanced=true` requires
the existing `advanced-read` capability and additionally exposes Champion Data
player, season and team provider IDs. A valid zero-game summary is returned
normally, not as not-found.

Round-history API exposure and both active-season and finality scheduling are
intentionally deferred. Operators can safely collect active season-to-date or
settled final data explicitly; no calendar finality date is assumed.
