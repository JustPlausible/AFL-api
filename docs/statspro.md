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

## Derived Home & Away scope

The same resource family also serves finalized local summaries with
`scope=home_and_away` and `source=DERIVED_MATCH_STATS`. These are not StatsPro
records: the authority chain is canonical CFS match player statistics → derived
Home & Away artifact. StatsPro `SEASON_TOTAL` remains AFL-published and includes
finals (`scope=full_season`, `source=AFL_STATSPRO`,
`source_context=SEASON_TOTAL`).

Round selection uses only `rounds.competition_phase`: `HOME_AND_AWAY` is
included and `FINALS` excluded. During the supported fixture collection flow,
that phase is classified from AFL match fixture metadata's explicit
`finals_match_label` semantic marker; neither round numbers nor labels have a role. A null
or unknown classification blocks finalization rather than guessing; this makes
Opening Round and historical/nonstandard numbering safe when their canonical
phase is persisted.

Build locally (there is no HTTP collector or upstream request in this path):

```bash
python cli.py --build-player-stat-summaries 2025 --scope home_and_away
```

The population is `competition_season_players LEFT JOIN H&A appearances`, so
historical season-team membership is retained and a valid member with no
appearance receives `games_played=0` and zero additive totals. The explicit
additive contract is goals, behinds, kicks, handballs, disposals, marks,
tackles, and hitouts. Goal accuracy is the sole reproduced rate and is computed
as aggregate goals divided by aggregate scoring shots; a zero denominator is
null. Match percentages lacking proven numerator/denominator semantics and
opaque `ratingPoints`/`ranking` are intentionally absent, never averaged or
summed.

Every concluded H&A fixture must have satisfactory two-sided, concluded CFS
authority or an active reviewed `stats_not_expected` disposition. Real facts
take precedence over a stale review. A review permits finalization but creates
no appearances. Calculation completes before a transactional scope replacement;
rebuilds are deterministic, update corrected facts, and preserve the last valid
summary when validation fails.

```http
GET /api/v1/seasons/{season_id}/player-stat-summaries?scope=home_and_away
GET /api/v1/players/{canonical_player_id}/seasons/{season_id}/player-stat-summary?scope=home_and_away
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
