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
there is no command-specific help screen. Only one top-level operation flag may
be selected per invocation; conflicting operations are rejected before runtime
database, browser, or network components are loaded.

Importing `cli` for `create_parser`, operation metadata, identifier validators,
or pure argument validation is intentionally lightweight. Runtime dependencies
are imported only after validation selects one operation and its handler is
dispatched. Consequently, help, version, parsing, validation, and unrelated
operations do not require database, legacy scraper/browser, or AFL/CFS optional
dependencies.

Network collectors need outbound AFL access. CFS commands additionally require
the configured CFS/WMC credentials. Persistent operations use `DB_PATH` (by
default `data/afl_players.db`) and require an initialized, migrated database.

## Preferred AFL public JSON commands

### Database-free pipeline collection

`--collect-afl-data` composes the maintained public AFL and authenticated CFS
collectors into a file-only run. It **never opens or writes the application
database**; `--no-database` may be supplied as an explicit operator assertion,
but is not a switch that changes the command's inherently database-free mode.

```bash
python cli.py --collect-afl-data --afl-season 2026 --collection-round 1 --collection-match 8001 --collection-endpoints fixtures,rosters,lineups,player-stats --collection-output collection-runs/2026-round-1-match-8001 --no-database
```

This small match-scoped form is suitable for manual endpoint validation.
`--afl-season` accepts the same year, numeric AFL ID, provider ID, or exact name
as metadata collection. Repeat `--collection-round ROUND` and
`--collection-match MATCH` to select multiple resources; a match accepts either
its numeric AFL ID or opaque `CD_M...` provider ID. `--collection-endpoints`
accepts comma-separated `metadata`, `players`, `fixtures`, `rosters`, `lineups`,
and `player-stats`. Recognised but unimplemented `injuries`, `commentary`, and
`interchange` families are recorded as unsupported/skipped rather than reported
as successes.

The deterministic output set is:

```text
OUTPUT/
  request.json
  summary.json
  raw/ENDPOINT/ENDPOINT__sorted-scope__page-NNNN.json
  normalised/ENDPOINT_FAMILY/RESOURCE_ID.json
```

Raw files preserve provider JSON. Normalised files contain a safe request/source
metadata envelope and normalised data; `request.json` records filters and source
contracts, while `summary.json` groups successful, skipped, and failed resources
by type. Credentials, WMC tokens, cookies, response headers, and provider bodies
from exceptions are never included in these files.

By default a non-empty output directory is rejected. Use
`--collection-overwrite` to atomically replace deterministic files, or
`--collection-resume` to safely complete/refresh an incomplete set. Individual
files are written to a temporary sibling and atomically replaced, so interruption
cannot leave partial JSON. Unsupported families do not fail the batch; a
required hierarchy failure or any resource collection failure produces a failed
summary and process exit status `1`. A complete batch, including one with clear
unsupported skips, exits `0`.

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

### Common collection diagnostic envelope

Collection boundaries use `CollectionDiagnostic` as a small, reusable envelope.
Its stable core identifies `operation`, `domain`, `source_family`, `collector`,
`mode`, `database_opened`, `persistence_target`, `result_status`,
`fallback_allowed`, and `fallback_occurred`. Optional fields describe a safe
source endpoint, persistence action, received/normalised/rejected records,
inserted/updated/unchanged/written rows, diagnostic count, target identifiers,
and audit/correlation IDs. JSON and compact operator output are assembled from
that same object; full JSON may additionally contain non-conflicting
domain-specific fields and records.

Missing or inapplicable values are JSON `null`; omitted human-summary values
mean the same thing. A numeric zero is emitted only when an operation measured
zero. In particular, the CLI never guesses an inserted-versus-updated split
when a writer exposes only a total. Endpoints are descriptions safe for logs,
not authenticated URLs, and credentials, cookies, tokens, authorization
headers, and raw sensitive errors are not diagnostic fields.

Source families have stable meanings: `public_afl_json` is authoritative public
AFL metadata JSON, `cfs_json` is authenticated Champion Data/CFS JSON, and
`html` is an explicit HTML workflow. A composite can report
`public_afl_json+cfs_json`. Canonical current statistics write only
`cfs_player_stats`; explicit legacy HTML statistics write only `player_stats`.
Operational injuries and lineups deliberately remain HTML-backed. No command
uses these diagnostics to enable fallback or dual writes.

Modes are `read_only` (no write), `database_free` (no database is opened and
the target is `none`), `persistent`, `legacy_persistent`, and `composite` for a
multi-stage operation. Stable statuses are `success`, `unchanged`, `partial`,
`unavailable`, `empty`, `live_partial`, `concluded`, `unknown`, `skipped`, and
`failed`. Provider publication detail such as `published` remains available in
`result_detail` or a domain field rather than being flattened away.

Representative compact JSON includes the same fields as `--print-json`:

```json
{"operation":"collect_match_player_stats","source_family":"cfs_json","collector":"MatchPlayerStatsCollector","mode":"persistent","database_opened":true,"persistence_target":"cfs_player_stats","rows_written":44,"result_status":"concluded","fallback_allowed":false,"fallback_occurred":false}
```

The equivalent database-free core is
`mode=database_free`, `database_opened=false`, and
`persistence_target=none`. An explicit legacy match-stat run instead reports
`source_family=html`, `mode=legacy_persistent`, and
`persistence_target=player_stats`.

### Active collection-operation inventory

This is a boundary inventory, not a transcript of every command's output.

| Operations | Classification | Source | Collector/service | Database and target | Structured output |
|---|---|---|---|---|---|
| `--collect-afl-metadata` | `read_only` | `public_afl_json` | `PublicAflCollector` | unopened; `none` | compact JSON; full `--print-json` |
| `--bootstrap-afl-season` | `composite` persistent | `public_afl_json+cfs_json` | `PublicAflCollector`, metadata/player persistence | opened; AFL metadata, canonical players and season links | compact/full JSON envelope |
| `--collect-afl-data` | `database_free` orchestrated | public AFL + CFS JSON | `CollectionOrchestrator` | unopened; deterministic files (`none` database target) | always JSON |
| `--collect-match-rosters` | `read_only` | `cfs_json` | `MatchRosterCollector` | unopened; `none` | compact JSON; full `--print-json` |
| `--collect-match-player-stats` | `persistent` | `cfs_json` | `MatchPlayerStatsCollector` | opened; `cfs_player_stats` | compact JSON; records with `--print-json` |
| `--scrape-match` | `legacy_persistent` | `html` | `scrape_afl_player_stats` | opened by scraper; `player_stats` | operator diagnostic plus existing scraper output |
| `--scrape-round`, `--scrape-all-rounds` | `legacy_persistent` composite | `html` | `scrape_afl_matches` | opened; legacy match/fixture tables | existing logs/audit only |
| `--scrape-lineups` | `legacy_persistent` | `html` | `scrape_afl_lineups` | opened; lineup tables | envelope; records with `--print-json` |
| `--scrape-injuries` | `legacy_persistent` | `html` | injury pipeline | opened; `injuries`, `injury_history` | compact/full JSON envelope |
| club scrape/enrich operations | file or composite legacy workflow | `html` or local files | club scraper / merge helpers | raw/enriched files; all-club flow imports players | existing summaries/logs |
| `--import-clubs`, `--export-clubs` | persistent / read-export | local canonical seed / database | club seed/import helpers | opened; clubs or backup JSON | existing logs |

Legacy round/all-round and club/import/export handlers deliberately retain their
specialized tables and summaries rather than receiving speculative counts or a
broad CLI rewrite. The envelope is intended for reuse by future orchestration,
including Issue #106, but this work does not implement that synchronization
workflow.

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
