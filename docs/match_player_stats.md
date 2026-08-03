# Match player-stat collection

The authenticated CFS player-stat collector is the preferred, independently
usable source of core match statistics; the rendered-HTML scraper remains as a
temporary fallback. Collect a supplied Champion Data match provider ID with:

```bash
python cli.py --collect-match-player-stats CD_M20260140101 --print-json
```

The argument must be an opaque Champion Data match provider ID beginning with
`CD_M` (for example, `CD_M20260142001`). Numeric AFL match identifiers are
rejected before any CFS authentication or request. The structured CLI summary
identifies `source_family=cfs_json`, `collector=MatchPlayerStatsCollector`, and
`persistence_target=cfs_player_stats` alongside `rows_written`.

This differs deliberately from `python -m cli --scrape-match 8216`, which is an
explicit legacy Playwright HTML operation and writes the separate
`player_stats` table. Scheduler and Admin match-stat operations use CFS JSON and
`cfs_player_stats` by default. Running the CFS command never invokes the legacy
scraper; running `--scrape-match` is an explicit manual choice, not fallback or
dual-writing. Reconciliation of the two table representations remains outside
Issue #73.

The CLI first looks for a matching `matches.match_provider_id` (or an optional
`--afl-match-id`) in the configured canonical database. For manual testing, an
explicit metadata fallback can be supplied without claiming it came from CFS:

```bash
python cli.py --collect-match-player-stats CD_M20260140101 --source-status CONCLUDED --print-json
```

Add `--afl-raw-directory PATH` for optional diagnostic capture. The raw writer
stores only the response JSON, under a deterministic match-scoped filename; it
does not store request headers, tokens or cookies. Automated tests use
sanitised fixtures and never contact AFL services.

## Canonical mapping and validation

Both `homeTeamPlayerStats` and `awayTeamPlayerStats` pass through the same
mapper and produce one collection. Natural identity is the requested Champion
Data match provider ID plus the deeply nested Champion Data `playerId` observed
at `player.player.player.playerId`. Each record also retains the AFL/internal
match ID when supplied by its caller, home/away affiliation,
collection time, endpoint, endpoint status, separately resolved canonical match
status, the original player entry, and unmapped statistics.

### Team identity investigation (Issue #126)

The currently verified endpoint responses do **not** provide independent team
identity. This is an observation about the retained states, not a claim that an
undocumented future endpoint response can never add an optional field.
The investigation checked each player-stat entry, its nested `player` and
`playerStats` objects, both `homeTeamPlayerStats` and `awayTeamPlayerStats`
containers, and top-level match metadata for `teamId`, `teamProviderId`,
`squadId`, `clubId`, and equivalents. Sanitised upcoming/live, postgame and
concluded captures showed no such field at any of those levels. In particular,
there is no endpoint value whose namespace can be confirmed as the same
Champion Data `CD_T...` namespace used by `afl_teams.provider_id`.

Consequently, normalisation sets `CanonicalPlayerStat.team_provider_id` to
null, the persistence DTO passes null, and the existing insert/upsert stores it
in nullable `cfs_player_stats.team_provider_id`. The `homeTeamPlayerStats` and
`awayTeamPlayerStats` collection names still produce `side=home|away`, but side
is placement context rather than independent provider identity. Neither the
canonical match participants nor current player-season membership is copied
into the statistic row; doing so would make participant reconciliation
circular. The nullable column remains available for a future documented source
contract without a schema migration.

The complete field trace is therefore:

| Stage | Team context |
| --- | --- |
| CFS response / sanitised fixture | No independent team field at player, collection-container, or match level |
| Payload traversal | The two arrays are traversed separately and assigned `side`; no team-identity path is read |
| Normalised record / writer DTO | `CanonicalPlayerStat.team_provider_id = None` |
| Persistence SQL | `upsert_player_stats` binds that value to the `team_provider_id` insert and conflict-update column |
| Schema | Migration `0006` defines `cfs_player_stats.team_provider_id TEXT` without `NOT NULL` |
| Reconciliation | `_team_context_checks` compares non-null values by side; null values produce informational unavailable context |

The upsert's snapshot-authority predicate applies to the entire row: repeated
concluded persistence is idempotent, and a later live or otherwise partial
snapshot cannot erase any concluded observation. In addition, null is treated
as absence rather than replacement for this optional identity: an accepted
same-authority update can refresh statistics while retaining an existing
non-null, independently sourced team provider ID. A documented future contract
can populate or replace the value with another non-null identity.

The season report compares a non-null independent statistic identity against
the canonical home or away participant and reports contradiction as
`stats.team_participant_mismatch`. Null source context remains informational as
`stats.team_provider_unavailable` and does not change completeness or exit
code. JSON retains per-match findings for auditability; human output aggregates
their row and match counts to avoid one repetitive line per match.

The initial central mapping is:

| Source `playerStats.stats` key | Canonical field |
| --- | --- |
| `goals` | `goals` |
| `behinds` | `behinds` |
| `kicks` | `kicks` |
| `handballs` | `handballs` |
| `disposals` | `disposals` |
| `marks` | `marks` |
| `tackles` | `tackles` |
| `hitouts` | `hitouts` |

Absent fields remain null and explicit zero remains zero. Integers, finite JSON
decimals and valid numeric strings use one `Decimal`-based validation path;
mathematically integral values become integers while non-integral precision is
retained. Empty/invalid strings, booleans, non-finite values and other types do
not become zero. Instead, that field remains null and a diagnostic names the
match, player and source field. Disposals are never derived from kicks and
handballs.

All keys in `playerStats.stats` outside the table above survive loss-consciously
in `extra_stats`, with original values, while `raw_player` preserves the entire
source entry. Known mapped keys are not duplicated in `extra_stats`.

## Publication and replacement semantics

JSON null, the known not-published HTTP response, and a metadata-only body with
an explicit unpublished status are `unavailable`, not authentication failures.
Published empty arrays are `empty`. Status provenance is deliberately split:

* `endpoint_source_status` is populated only from `status`, `matchStatus`, or
  `matchPhase` in the player-stat response;
* `resolved_match_status` is the latest recognised status after reconciling
  stored canonical metadata, direct public match detail, and endpoint status;
* `status` is the canonical player-stat publication classification used for
  persistence authority.

Lifecycle status advances monotonically as `SCHEDULED < LIVE < POSTGAME <
CONCLUDED`; no source can downgrade a later observation. The CLI reads the AFL
numeric ID and status from the canonical match row, consults the direct public
match-detail endpoint unless the row is already concluded, and persists an
accepted advance to `matches.status` and `updated_at` without changing the
metadata scraper's `scraped_at`. A supplied single/null team array, live, or
postgame status is `live_partial`; concluded status receives final authority.
A non-empty response without any recognised status remains `unknown`.

Migration `0006` adds a current-observation table with a unique constraint on
match provider ID and Champion Data player ID. Idempotent upsert permits a later
observation at the same authority and always permits concluded data to replace
live/unknown data. It refuses any live/unknown observation—regardless of its
timestamp—to downgrade a concluded row, and refuses older observations at the
same authority. `collected_at`, explicit source status and snapshot authority
make the current observation and its provenance visible.

Malformed individual records are rejected without discarding usable peers.
Diagnostics cover missing and duplicate IDs, a player on both sides, invalid
numeric values, malformed records, null/missing team arrays, and retained
partial publication. Unrecognised top-level shapes fail with a structured AFL
JSON invalid-response error.

## Source uncertainties and live verification

Live verification after the initial implementation confirmed valid upcoming,
live, and concluded player-stat payloads, but the tested responses did **not**
contain a usable top-level `status`, `matchStatus`, or `matchPhase`. The
collector therefore resolves canonical match metadata where available while
leaving `endpoint_source_status` null, rather than mislabelling external status
as endpoint data. Whether arrays/values are cumulative throughout every match
phase and whether wrapper depth varies remain unverified. The collector
tolerates observed identity wrapper-depth variants, preserves unknown fields,
and reports partial arrays.

For a future live check, run the command above during a match with a diagnostic
directory, repeat it later using the same match ID/directory, compare the two
sanitised JSON captures, and exercise persistence through `upsert_player_stats`.
Never commit token-bearing headers or an unsanitised large response.

The repository default database is `data/afl_players.db` (and may be overridden
with `DB_PATH`). Verify the effective configured path before live reconciliation:

```bash
python -c 'import config; print(config.DB_PATH)'
sqlite3 data/afl_players.db \
  "SELECT match_id, match_provider_id, status, updated_at, scraped_at FROM matches WHERE match_provider_id='CD_M20260142007';"
curl --fail --silent --show-error \
  'https://aflapi.afl.com.au/afl/v2/matches/8207' | python -m json.tool
python cli.py --collect-match-player-stats CD_M20260142007 --print-json
sqlite3 data/afl_players.db \
  "SELECT match_id, match_provider_id, status, updated_at, scraped_at FROM matches WHERE match_provider_id='CD_M20260142007';"
sqlite3 data/afl_players.db \
  "SELECT COUNT(*), MIN(snapshot_authority), MAX(snapshot_authority), MIN(resolved_match_status) FROM cfs_player_stats WHERE match_provider_id='CD_M20260142007';"
```

For a safe, read-only 2026 reconciliation summary against a populated database,
run the following query. It reports authoritative totals and null coverage,
distinct non-null formats, side/participant contradictions, and matches with no
independent team context; it does not expose raw payloads or credentials.

```sql
WITH authoritative AS (
  SELECT s.*, m.match_id, ht.provider_id AS home_provider_id,
         at.provider_id AS away_provider_id
  FROM cfs_player_stats s
  JOIN matches m ON m.match_provider_id = s.match_provider_id
  LEFT JOIN afl_teams ht ON ht.afl_id = m.home_team_id
  LEFT JOIN afl_teams at ON at.afl_id = m.away_team_id
  WHERE m.season_id = (SELECT afl_id FROM afl_seasons WHERE year = 2026)
    AND s.snapshot_authority = 2
)
SELECT COUNT(*) AS authoritative_rows,
       SUM(team_provider_id IS NOT NULL) AS with_team_provider_id,
       SUM(team_provider_id IS NULL) AS without_team_provider_id,
       COUNT(DISTINCT CASE WHEN team_provider_id IS NOT NULL
             THEN CASE WHEN team_provider_id GLOB 'CD_T[0-9]*' THEN 'CD_T<number>'
                       ELSE 'other' END END) AS distinct_provider_id_formats,
       SUM(team_provider_id IS NOT NULL AND
           ((side = 'home' AND team_provider_id IS NOT home_provider_id) OR
            (side = 'away' AND team_provider_id IS NOT away_provider_id)))
         AS participant_mismatches,
       COUNT(DISTINCT CASE WHEN team_provider_id IS NULL THEN match_id END)
         AS matches_lacking_independent_team_context
FROM authoritative;
```
