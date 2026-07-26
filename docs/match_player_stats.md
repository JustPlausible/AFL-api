# Match player-stat collection

The authenticated CFS player-stat collector is the preferred, independently
usable source of core match statistics; the rendered-HTML scraper remains as a
temporary fallback. Collect a supplied Champion Data match provider ID with:

```bash
python cli.py --collect-match-player-stats CD_M20260140101 --print-json
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
match ID when supplied by its caller, `teamId`, home/away affiliation,
collection time, endpoint, explicit source/match status, the original player
entry, and unmapped statistics.

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
Published empty arrays are `empty`; a supplied single/null team array or
explicit live status is `live_partial`; only an explicit `CONCLUDED`,
`COMPLETED`, or `FINAL` status is concluded. A non-empty response without a
trustworthy status remains `unknown` rather than being guessed final.

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

The committed shapes are sanitised representations of endpoint structure
already recorded in the endpoint catalogue: separate home/away arrays, nested
player identity and `playerStats.stats`. No live match was available during
Issue #59 implementation to confirm whether arrays/values are cumulative, which
top-level match-status key is consistently present, or whether team/player
wrapper depth varies by match phase. The collector therefore tolerates verified
wrapper-depth variants, uses only explicit status (or caller-supplied existing
match metadata), preserves unknown fields, and reports partial arrays.

For a future live check, run the command above during a match with a diagnostic
directory, repeat it later using the same match ID/directory, compare the two
sanitised JSON captures, and exercise persistence through `upsert_player_stats`.
Never commit token-bearing headers or an unsanitised large response.
