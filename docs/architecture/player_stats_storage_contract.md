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

## Append-only history and period checkpoints (Issue #195)

Two additional tables, added by migration `0020`, capture the live evolution
of `cfs_player_stats` without weakening or duplicating its authority:

```text
cfs_player_stats
    = authoritative current/final state

cfs_player_stat_history
    = append-only observed field transitions

cfs_player_stat_checkpoints
    = sparse full-canonical-line snapshots at shared period/finality markers
```

**AFL-api records what CFS reported. It does not infer why a value went
backwards.** A decrease (`tackles: 6 -> 5`) or a scoring-outcome reversal
(`goals: 1 -> 0`, `behinds: 0 -> 1` in the same poll) is stored exactly as
observed, with no "correction", "score review", "goblin", or provider-fix
label attached unless CFS itself states that semantic. Downstream consumers
are free to interpret negative deltas however they wish.

### Integration point

History/checkpoint capture is not a second collector or scheduler path: it is
built into `afl_json.player_stats.upsert_player_stats` itself, the single
write function every existing caller already uses (the live scheduler poller,
the CLI, `collection/source_policy.py`, and `afl_json/season_sync.py`). No new
`playerStats` polling, endpoint, or scheduler window was added. When the two
new tables are absent (an older, pre-migration-0020 database), the function's
behaviour and return value are byte-for-byte unchanged.

### Authority-derived comparison, not independent detection

For each record, `upsert_player_stats` reads the player's existing canonical
row (if any) *before* running its existing `INSERT ... ON CONFLICT` upsert,
then inspects the same upsert's `cursor.rowcount`:

* **Rejected** (stale/lower authority than the existing row): `rowcount == 0`
  and the observation also fails a same-authority/time re-check -- no history,
  no checkpoint. A stale or lower-authority observation can never fabricate
  history or move a checkpoint, matching the existing snapshot-authority
  protection on `cfs_player_stats` itself.
* **Accepted with an applied change** (`rowcount == 1`): the pre-image is
  diffed against the eight canonical fields (`goals`, `behinds`, `kicks`,
  `handballs`, `disposals`, `marks`, `tackles`, `hitouts`); every field that
  genuinely changed gets one `cfs_player_stat_history` row with
  `previous_value`, `new_value`, and `delta` (`NULL` when either side is
  unobserved, since a delta across a null is not a numeric fact).
* **Accepted as a no-op** (same authority, same-or-newer collection time, but
  every compared column already matched -- e.g. a player whose line is
  unchanged since the last poll): no history row (nothing changed), but the
  observation is still eligible for a checkpoint, because a checkpoint is a
  full-state snapshot, not a change event.

The very first accepted observation for a player in a match is a **baseline**,
not history: it never produces field-level `NULL -> value` rows (which would
otherwise flood the table for every field a player happens to start with), and
instead becomes the `BASELINE` checkpoint.

A canonical field is skipped entirely for history (in every case above) when
this same poll recorded an `invalid_numeric` diagnostic for that
player/field. `cfs_player_stats` still stores `NULL` for that field
(unchanged, existing behaviour) but that reflects a rejected/malformed source
value, not a genuine observation -- history must not turn a validation
rejection into a fabricated statistic-removal event.

### Null-transition semantics

* A field genuinely absent from the source `stats` object (no key present) is
  a legitimate `NULL` observation. A later poll that reports a numeric value
  for that field is a real `NULL -> value` transition, and vice versa.
* A field present in the source but failing numeric validation is **not**
  treated as an observation at all for history purposes (see above) -- it is
  conservatively ignored, not turned into a `value -> NULL` event.

### Checkpoint markers, triggers, and deduplication

Checkpoint markers: `BASELINE`, `QT`, `HT`, `3QT`, `FT`, `CONCLUDED`. `QT`/
`HT`/`3QT`/`FT` come directly from the shared, already-normalized
`afl_json.match_period.MatchPeriodState` vocabulary (Issue #187) -- this
module never derives period state itself, and active-quarter states
(`Q1`-`Q4`) never trigger a checkpoint, so a full snapshot is not written on
every poll. `CONCLUDED` fires when the accepted observation's status is
`PlayerStatsStatus.CONCLUDED`.

**`FT` is the internal `MatchPeriodState` marker for Q4 completing; `CONCLUDED`
is the separate, later authoritative match-lifecycle marker.** Issue #187
established that Q4/`FT` completing does not itself imply `CONCLUDED` --
`POSTGAME -> CONCLUDED` is a distinct, later lifecycle transition. If a
postgame provider adjustment changes the canonical line between the two, both
checkpoints persist independently (`FT` keeps its own values; `CONCLUDED`
records the later, different values) so the distinction is queryable rather
than overwritten.

`cfs_player_stat_checkpoints` has a `UNIQUE(match_provider_id,
champion_data_player_id, checkpoint_marker)` constraint, and each write is
itself an authority-guarded upsert (same authority/meaningful-change shape as
`cfs_player_stats`'s own guard). Repeatedly polling during a break therefore
never creates duplicate checkpoint rows for that marker; a genuine same- or
higher-authority correction while still sitting at that marker updates the one
row in place.

### Match-period-state wiring

`upsert_player_stats(conn, result, *, match_period_state=None)` accepts an
optional `MatchPeriodState`. The live scheduler
(`scheduler/player_stat_polling.py`) exposes an optional
`period_state_provider` hook on `PlayerStatPollingWorker`, resolved once per
claimed attempt (outside the write-lane transaction, alongside the existing
player-stat collection call) and passed straight through. It is deliberately
**unset by default**: CFS `matchItem`/`score.matchClock.periods` is not yet a
maintained, verified production endpoint (`afl_json/match_period.py` remains
informational-only per Issue #187), and wiring a live fetch would add a second
network call to every polling attempt, which is a bigger step than this
narrowly-scoped integration point. With no provider configured, behaviour and
network-call volume are completely unchanged; history and `BASELINE`/
`CONCLUDED` checkpoints are still recorded, just without `QT`/`HT`/`3QT`/`FT`
tagging until a provider is supplied. A lookup failure inside a supplied
provider is swallowed and never affects finality, cadence, or whether an
observation is accepted.

### Transaction and error semantics

History/checkpoint writes execute on the same `sqlite3.Connection` as the
`cfs_player_stats` upsert, inside whatever transaction the caller already
owns. In the live scheduler this is `scheduler/write_lane.py`'s single
commit-on-success/rollback-on-exception transaction per attempt: a history- or
checkpoint-write failure raises, the whole attempt's transaction rolls back,
and `cfs_player_stats` is left exactly as it was before the attempt (no
authoritative update without its matching history, and no history claiming a
change that was never applied). No feature flag, partial commit, or
best-effort fallback is used for this path.

### Evidence gap

A recent `commentaryFeed` capture for `CD_M20260142406` records a live
`GOAL - Bulldogs (Cody Weightman)` later presented as `BEHIND - Bulldogs
(Cody Weightman)` at the same period/second (see
`tests/fixtures/afl/commentary/`). No matching CFS `playerStats` snapshot
pair (before/after that reversal) exists anywhere in this repository's
fixtures or diagnostic captures, so the automated goal/behind-reversal test in
`tests/test_player_stat_history.py` is a clearly labelled **synthetic**
fixture exercising the same shape, not a replay of that real event. Closing
this evidence gap requires a genuine CFS `playerStats` capture spanning that
transition, which does not currently exist in this repository.

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
