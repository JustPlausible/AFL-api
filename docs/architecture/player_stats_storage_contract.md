# Player-stat persistence and authority contract

**Status:** active architecture contract  
**Scope:** match player statistics only

## Decision

`cfs_player_stats` is the authoritative persistence model for current match
player-stat collection and for all new application reads. Authenticated CFS JSON
is the preferred source. `player_stats` is a legacy, rendered-HTML scraper model
retained for its explicit scraper workflow and the existing compatibility API.

The models are not interchangeable. Collection never dual-writes, never copies
legacy rows into CFS storage, and never invokes HTML automatically when CFS is
unavailable, empty, unpublished, malformed, or fails authentication/transport.
An operator must explicitly request the legacy HTML workflow.

## Model comparison

| Contract | `player_stats` | `cfs_player_stats` |
| --- | --- | --- |
| Authority | Legacy/scraper-specific compatibility data | **Authoritative for current collection and new reads** |
| Source family | Playwright-rendered AFL match-centre HTML | Authenticated Champion Data/CFS JSON |
| Writer | `scraper.scrape_afl_player_stats` via `save_player_stats_to_db` | `MatchPlayerStatsCollector` via `upsert_player_stats` |
| Match identity | Numeric AFL/internal `match_id`; optional `round_id` | Opaque `CD_M...` `match_provider_id`; optional textual `afl_match_id` |
| Player identity | Numeric AFL `afl_id`; optional image-derived `champion_id` | Required opaque `CD_I...` `champion_data_player_id`; optional `canonical_player_id` crosswalk |
| Natural key | `(match_id, afl_id)` | `(match_provider_id, champion_data_player_id)` |
| Provenance/lifecycle | `scraped_at`, scraper `status` (`LIVE` or `COMPLETED`) | `collected_at`, `source_endpoint`, endpoint and resolved status, `snapshot_authority` |
| Fields | Legacy named columns including AFL Fantasy score, clearances, metres gained, assists and time on ground | Eight canonical fields plus `extra_stats_json` and lossless `raw_player_json` |
| Current readers | `GET /api/player-stats` compatibility route | No production API/report/scoring reader yet; direct operational queries only |

The legacy uniqueness constraint permits a nullable `afl_id` under SQLite rules;
the scraper normally requires and parses that AFL player ID. This is not a safe
cross-model identity guarantee. Likewise, `champion_id` and
`champion_data_player_id` must not be assumed equivalent without a validated
provider crosswalk.

## Writers and entry points

### Authoritative CFS path

All operational entry points write only `cfs_player_stats`:

* CLI: `python cli.py --collect-match-player-stats CD_M20260142001 --print-json`.
* Scheduler: match-stat jobs call `run_stats_scraper`, which dispatches the
  `MATCH_PLAYER_STATS` operational domain.
* Admin: `POST /scheduler/manual/player-stats/match` queues the same operational
  domain for an internal numeric match ID.
* Shared policy: `collection/source_policy.py` resolves the internal match to a
  `match_provider_id`, reconciles match status, runs `MatchPlayerStatsCollector`,
  and calls `upsert_player_stats`.

Despite its generic function name, `afl_json.player_stats.upsert_player_stats`
writes only `cfs_player_stats`. It does not inspect or update `player_stats`.

### Explicit legacy HTML path

`python cli.py --scrape-match 8216` (or deliberate direct execution of
`scraper/scrape_afl_player_stats.py`) renders match-centre HTML and writes only
`player_stats`. CLI diagnostics state `source_family=html`, the legacy collector,
`persistence_target=player_stats`, and `fallback_occurred=false`. This path is
supported for compatibility/diagnostics; it is not current operational authority.

The scheduler and Admin do not invoke this scraper for player-stat collection.

## CFS publication and snapshot states

The CFS collector preserves these distinct outcomes:

| State | Meaning and persistence |
| --- | --- |
| `unavailable` | Resource-unavailable response, `null`, or an explicitly unpublished lifecycle; zero records and zero writes. |
| `empty` | Recognised, published team arrays contain no valid player records; zero writes and not treated as permission to scrape HTML. |
| `live_partial` | Live/non-concluded snapshot, including one missing team array or partial fields; rows use `snapshot_authority=1`. Diagnostics preserve partial/malformed evidence. |
| `concluded` | Final snapshot; rows use `snapshot_authority=2`. A later live snapshot cannot replace it. |
| `unknown` | Records exist but lifecycle cannot prove conclusion; treated as non-final authority (`1`). |

For equal authority, a newer changed observation may update a row. Repeating an
identical observation is idempotent. Extra and raw CFS fields remain stored so a
future mapping does not erase source evidence.

## Read contract

New downstream APIs, external scoring applications, reports, exports, Admin
views, and other application features **must read `cfs_player_stats`** and use
CFS provider/canonical identity explicitly. Code can use
`authoritative_player_stats_table()` from
`collection.player_stats_storage` when it needs the named authority boundary.
Do not implement “whichever table has rows,” unions, implicit preference, or
silent legacy fallback.

`GET /api/player-stats` is the only production reader found for either table. It
continues to query `player_stats` because its public filters and response use
legacy numeric IDs/columns. It must be described as a legacy compatibility API,
not evidence that `player_stats` is authoritative. Changing it to CFS requires a
separate versioned response/identity compatibility design.

No repository API, downstream scoring or reporting integration, export, or other
production database-access path currently reads `cfs_player_stats`. New work
must not copy the legacy route's table choice.

## Operator verification queries

After CFS CLI, scheduler, or Admin collection:

```sql
SELECT match_provider_id, champion_data_player_id, canonical_player_id,
       endpoint_source_status, resolved_match_status, snapshot_authority,
       collected_at
FROM cfs_player_stats
WHERE match_provider_id = 'CD_M20260142001'
ORDER BY side, champion_data_player_id;
```

After an explicit legacy HTML scrape:

```sql
SELECT match_id, afl_id, champion_id, player_name, status, scraped_at
FROM player_stats
WHERE match_id = 8216
ORDER BY team_code, afl_id;
```

To compare counts without implying row identity or equivalence:

```sql
SELECT 'legacy_html_player_stats' AS source_model, COUNT(*) AS rows
FROM player_stats
UNION ALL
SELECT 'authoritative_cfs_player_stats', COUNT(*)
FROM cfs_player_stats;
```

Command output and structured operational logs also identify `source_family`,
collector, persistence target/rows written, status, and `fallback_occurred`.
Audit `scrape_runs` for trigger, target, row counts, and failures. A zero-row CFS
result does not imply that data was written elsewhere.

## Compatibility, deprecation, and migration direction

`player_stats` remains actively required only for the explicit legacy scraper
and `/api/player-stats` compatibility response. Do not delete it or migrate user
data in this issue. New features must not add dependencies on it.

A follow-up retirement or canonical read-model issue should inventory consumers,
version the API as needed, and define all of the following before any merge,
view, backfill, or table consolidation. The
[canonical CFS player-stat read API design](api/player_stats_api_design.md)
proposes exactly this versioned, additive `/api/v1` surface for `cfs_player_stats`
reads; it does not itself change `/api/player-stats` or authorise a table
merge:

1. validated AFL, Champion Data, and canonical player identity mapping;
2. numeric AFL/internal and opaque provider match identity mapping;
3. per-row source provenance and collection time;
4. exact field/nullability mapping, including legacy-only and unmapped CFS fields;
5. unpublished, empty, live-partial, unknown, and concluded authority semantics;
6. conflict, deduplication, history, rollback, and compatibility behavior.

**The two tables must not be merged without that explicit identity, provenance,
and field-mapping design.** Until then, retain legacy data in place, keep HTML
execution explicit and identifiable, and treat CFS storage as the sole authority
for new development.
