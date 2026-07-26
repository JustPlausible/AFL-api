# Match player-stat collection

The authenticated CFS player-stat collector is the preferred, independently
usable source of core match statistics; the rendered-HTML scraper remains as a
temporary fallback. Collect a supplied Champion Data match provider ID with:

```bash
python cli.py --collect-match-player-stats CD_M20260140101 --print-json
```

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
match ID when supplied by its caller, `teamId`, home/away affiliation,
collection time, endpoint, endpoint status, separately resolved canonical match
status, the original player entry, and unmapped statistics.

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
* `resolved_match_status` is populated only from caller-supplied canonical
  match metadata (the CLI labels whether that came from its database or the
  explicit option);
* `status` is the canonical player-stat publication classification used for
  persistence authority.

An endpoint status always takes precedence. When it is absent, resolved match
metadata is used. Conflicting values retain both provenance fields and produce
a diagnostic. A supplied single/null team array or effective live status is
`live_partial`; an effective `CONCLUDED`, `COMPLETED`, or `FINAL` status is
concluded. A non-empty response without either trustworthy status remains
`unknown` rather than being guessed final.

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
