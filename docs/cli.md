# Operator CLI command reference

The root `cli.py` flag parser is the supported operator interface. JSON sources
are preferred. HTML collection remains explicit and is never an automatic
fallback or dual-write path.

## General usage and help

Run commands from the repository root in the configured project environment:

```bash
python cli.py --help
python cli.py --version
```

`--version` prints only the bare, script-friendly version number and exits
without loading the database, application, browser, or collector stack. The
authoritative declaration lives in [`version.py`](../version.py); see the
[v0.5.0 release notes](releases/v0.5.0.md) for release detail.

The interface is flag-based rather than subcommand-based. Consequently,
`python cli.py --collect-afl-metadata --help` displays the same complete help;
there is no command-specific help screen. Only the first operation flag is
dispatched, so invoke one collection or database operation at a time.

Network collectors need outbound AFL access. CFS commands additionally require
the configured CFS/WMC credentials. Persistent operations use `DB_PATH` (by
default `data/afl_players.db`) and require an initialized, migrated database.

## Preferred AFL public JSON commands

### Read-only metadata collection

```bash
python cli.py --collect-afl-metadata --afl-season 2026 --print-json
```

`--collect-afl-metadata` collects and normalises competition, season, round,
team, and match metadata from AFL public JSON. It does **not** write the
database. `--afl-season SEASON` accepts a year, AFL numeric ID, Champion Data
provider ID, or exact season name. The competition defaults can be overridden:

```bash
python cli.py --collect-afl-metadata --afl-season 2026 --afl-competition-code AFL --afl-competition-provider-id CD_C014
```

### Persistent season bootstrap

```bash
python cli.py --bootstrap-afl-season 2026
```

This collects public metadata, persists the canonical competition hierarchy,
then collects authenticated CFS season players and persists canonical players,
provider mappings, team links where the collected team resolves, and season
membership in one transactional player batch. This implemented bootstrap is the
supported canonical player-persistence path; it is not a Scheduler or Admin
player-refresh job. The value is a season selector
(normally a four-digit year), not a round ID. It requires a migrated database
and CFS authentication for the player phase.

## Preferred Champion Data/CFS JSON commands

Provider identifiers are opaque strings. A round provider ID must begin
`CD_R`; a match provider ID must begin `CD_M`. Numeric AFL IDs and the wrong
provider-ID family are rejected before network access.

```bash
python cli.py --collect-match-rosters CD_R202601421 --print-json
```

This collects CFS round selections and change records. It is **read-only**:
there is no canonical, publication-safe roster persistence path yet, so it does
not write legacy lineup tables.

```bash
python cli.py --collect-match-player-stats CD_M20260142001 --print-json
```

This collects CFS match player statistics and persists the current canonical
snapshot to `cfs_player_stats`. It consults canonical match metadata for status
reconciliation. `--afl-match-id AFL_MATCH_ID` supplies an AFL **numeric match
ID** for that reconciliation when needed; `--source-status STATUS` is an
explicit diagnostic status fallback. Both options require
`--collect-match-player-stats` and do not change the required `CD_M...`
collector identifier.

## Explicit legacy HTML commands

These commands use rendered AFL web pages, persist legacy tables where noted,
and are not invoked as fallback by a JSON command:

```bash
python cli.py --scrape-injuries
python cli.py --scrape-lineups 9
python cli.py --scrape-round 1155
python cli.py --scrape-all-rounds
python cli.py --scrape-match 8216
```

| Flag | Identifier | Behavior |
| --- | --- | --- |
| `--scrape-injuries` | none | Collects HTML injury data, resolves players to canonical AFL IDs, and persists resolved current/history injury records; unresolved or ambiguous rows are reported rather than assigned guessed identities. |
| `--scrape-lineups ROUND` | AFL round number, such as `9` | Collects HTML lineups and persists legacy lineup tables. |
| `--scrape-round ROUND_ID` | Database/AFL numeric `round_id`, such as `1155` | Collects HTML match cards and persists legacy match data; this is not a round number. |
| `--scrape-all-rounds` | none | Reads rounds already in the database and runs the legacy HTML match collector for all of them. |
| `--scrape-match MATCH_ID` | AFL numeric match ID, such as `8216` | Collects HTML player stats and persists `player_stats`, separately from CFS `cfs_player_stats`. |

The direct scraper modules are implementation/diagnostic entry points rather
than the release-facing operator interface. Prefer the `cli.py` forms above.

Scheduler and Admin do not invoke `--scrape-match`: their operational
player-stat jobs use `MatchPlayerStatsCollector` over CFS JSON and write
`cfs_player_stats`, matching `--collect-match-player-stats`. Conversely, the
explicit manual `--scrape-match` HTML path writes `player_stats` only. The two
tables have different writers and are neither interchangeable nor dual-written.
See the [player-stat storage contract](architecture/player_stats_storage_contract.md)
before adding a reader, report, scoring path, or migration involving either table.
Scheduler/Admin injuries and operational lineups intentionally use the same HTML
source families as `--scrape-injuries` and `--scrape-lineups`; those selections
are policy choices, not fallback after a JSON failure.

## Club import, export, scrape, and enrichment

Club player scraping is a legacy HTML/file workflow:

```bash
python cli.py --scrape-club richmond
python cli.py --enrich-club richmond
python cli.py --scrape-clubs --skip-existing
python cli.py --enrich-clubs
python cli.py --scrape-enrich-all --skip-existing
```

`--scrape-club` and `--scrape-clubs` write raw player JSON files under `data/`.
Enrichment reads those files and adds local aliases/codes. The combined command
scrapes, enriches, and finally imports players into the database.

```bash
python cli.py --import-clubs
python cli.py --export-clubs
```

`--import-clubs` persists the repository's canonical club seed; it does not
import an arbitrary operator-supplied JSON file. `--export-clubs` reads the
database and writes its configured backup JSON. Both require an initialized
database.

## Output, raw capture, and diagnostics

`--print-json` prints the full collected/normalised payload for commands that
support it; without it JSON commands print a compact summary. It does not turn
a persistent command into a dry run. Shell redirection saves standard output:

```bash
python cli.py --collect-afl-metadata --afl-season 2026 --print-json > metadata-2026.json
```

`--afl-raw-directory PATH` retains original per-endpoint/page AFL or CFS JSON
below `PATH` for diagnostics. It is not the destination for normalised output,
does not disable database persistence, and never stores request credentials:

```bash
python cli.py --collect-match-rosters CD_R202601421 --afl-raw-directory data/raw-afl
```

`--skip-existing` applies only to club file scraping. There is no silent
JSON-to-HTML fallback, source auto-selection, or automatic write to both legacy
and canonical tables.

## Bounded JSON CLI gap review

The implemented public metadata hierarchy, season/player bootstrap, CFS match
rosters, and CFS match player statistics are all reachable through `cli.py`.
No new persistence switch was added: roster/selection persistence lacks a
canonical publication-safe persistence function, while metadata collection and
metadata persistence are already deliberately separated. Follow-up work should
first define canonical selection identity/replacement semantics; only then
should it expose a persistent CFS roster command. Grouped subcommands and
broader source-selection redesign are also intentionally outside this guide's
scope.
