# Operator CLI command reference

The root `cli.py` flag parser is the supported operator interface. JSON sources
are preferred. HTML collection remains explicit and is never an automatic
fallback or dual-write path.

## Which command should I run?

Run these from the repository root after activating the configured Python
environment. Placeholder identifiers show the required identifier family; replace
them with values from the target season. “CFS auth” means access to the protected
CFS JSON endpoints: the client obtains and refreshes its WMC token automatically,
so there is currently no operator-supplied CFS username, password, or token
environment variable.

| Operator goal | Exact recommended command | Database effect | Source and persistence notes | Credentials/access |
| --- | --- | --- | --- | --- |
| Show the application version | `python cli.py --version` | Does not open the database | Local, lightweight version probe | None |
| Apply migrations | `python -m db.migrate` | Creates/upgrades the configured `DB_PATH`; also refreshes the canonical club seed when a migration calls for it | Repository migration modules; run before bootstrap | Filesystem write access to `DB_PATH` |
| Bootstrap one AFL season | `python cli.py --bootstrap-afl-season <SEASON_YEAR>` | Opens the database; upserts canonical metadata, players, provider mappings, team links, and season membership | Public AFL JSON metadata followed by CFS season players; concise summary by default, full result with `--print-json`; no HTML fallback | Outbound AFL access and CFS auth |
| Inspect season metadata without persistence | `python cli.py --collect-afl-metadata --afl-season <SEASON_YEAR> --print-json` | Does not open or write the database | Public AFL competition, season, rounds, teams, and matches only | Outbound public AFL access; no CFS auth |
| Collect several resources to artifacts only | `python cli.py --collect-afl-data --afl-season <SEASON_YEAR> --collection-round 1 --collection-endpoints metadata,players,fixtures,rosters,lineups,player-stats --collection-output collection-runs/season-round-1 --no-database` | Never opens the database | Writes raw/normalised files under the output directory; selected families can combine public AFL and CFS JSON | Outbound AFL access; CFS auth for protected families |
| Persist authoritative stats for one completed match | `python cli.py --collect-match-player-stats CD_M20260142001 --afl-match-id 8001` | Opens the database; upserts `cfs_player_stats` | CFS JSON canonical snapshot; one `CD_M...` match; never HTML fallback or a `player_stats` dual-write | Outbound AFL access and CFS auth |
| Inspect a CFS round roster | `python cli.py --collect-match-rosters CD_R202601421 --print-json` | Does not open or write the database | CFS selections are returned for inspection and are **not** canonically persisted | Outbound AFL access and CFS auth |
| Collect current injuries | `python cli.py --scrape-injuries` | Opens the database; upserts canonically resolved `injuries` and `injury_history` | Deliberate rendered-HTML operational source; unresolved/ambiguous players are reported, not guessed | Outbound AFL website access |
| Collect operational lineups for a round | `python cli.py --scrape-lineups 9` | Opens the database; upserts operational lineup tables | Deliberate rendered-HTML source; argument is an AFL round number | Outbound AFL website access |
| Explicitly scrape legacy matches for one database round | `python cli.py --scrape-round 1155` | Opens the database; writes legacy match/fixture data | Explicit HTML compatibility path; argument is a database/AFL `round_id`, not round number | Outbound AFL website access |
| Explicitly scrape legacy statistics for one match | `python cli.py --scrape-match 8216` | Opens the database; upserts legacy `player_stats` | Explicit HTML compatibility path; not authoritative and never an automatic fallback | Outbound AFL website access |
| Import or refresh the supported club seed | `python cli.py --import-clubs` | Opens the database; upserts canonical `bootstrap/clubs.json` rows | Local repository seed, not an arbitrary input file; migrations already seed/refresh it where applicable | Filesystem write access to `DB_PATH` |
| Synchronise an implemented whole season | `python cli.py --sync-afl-season <SEASON_YEAR>` | Opens the database; bootstraps canonical season data and upserts concluded match snapshots in `cfs_player_stats` | Implemented idempotent AFL/CFS JSON workflow; use its documented round/match bounds for narrower work | Outbound AFL access and CFS auth |
| Report persisted season completeness | `python cli.py --report-afl-season <SEASON_YEAR>` | Opens the existing SQLite database in query-only mode; never writes | Canonical AFL season relationships and authoritative `cfs_player_stats`; `--print-json` emits the structured result | No network or credentials |
| Add an API key | `python cli.py --add-api-key <LABEL>` | Opens the database; inserts one hashed key | Full key is printed once and never stored; only its hash and prefix are persisted | Filesystem write access to `DB_PATH` |
| List API keys | `python cli.py --list-api-keys` | Opens the database; reads API-key metadata | Prints label, prefix, and active status only; full keys are never shown | Filesystem read access to `DB_PATH` |
| Remove an API key | `python cli.py --remove-api-key <KEY_OR_LABEL>` | Opens the database; deletes one row | Accepts either the presented full key or its label | Filesystem write access to `DB_PATH` |
| Grant an API-key capability | `python cli.py --grant-api-key-capability <LABEL> <CAPABILITY>` | Opens the database; adds a named capability | Supported values are validated; `advanced-read` enables future advanced metadata access | Filesystem write access to `DB_PATH` |
| Revoke an API-key capability | `python cli.py --revoke-api-key-capability <LABEL> <CAPABILITY>` | Opens the database; removes a named capability | Repeating an absent revocation is a safe no-op | Filesystem write access to `DB_PATH` |

The table is a selector, not a full option reference. The sections below explain
individual collection modes; the implemented bounds and exit behavior for season
sync are in the [whole-season synchronization reference](../README.md#whole-season-persistent-synchronisation).

## Supported first run on a clean installation

This is the smallest supported setup sequence. The production release procedure,
including writer shutdown, backups, restore, and rollback, remains in the
[release runbook](operations/release_runbook_v0_5_0.md).

1. Copy or otherwise configure the environment values described in
   [`.env.example`](../.env.example). Set `DB_PATH` to the intended SQLite file
   (relative paths resolve from the repository root) and confirm the effective
   value before writing:

   ```bash
   python -c 'import config; print(config.DB_PATH)'
   ```

   Configure production Admin credentials and API-key handling before exposing
   those services. Public metadata needs only outbound AFL access. Protected CFS
   calls use the automatically acquired WMC token described above; they do not
   read a repository-defined CFS credential environment variable.
2. Apply all migrations. This opens and creates or upgrades `DB_PATH`, including
   the supported club seed:

   ```bash
   python -m db.migrate
   ```

3. Bootstrap the target season. This first reads public AFL JSON, then requires
   CFS auth for season players, and writes the canonical hierarchy and membership:

   ```bash
   python cli.py --bootstrap-afl-season <SEASON_YEAR>
   ```

4. Populate authoritative statistics for concluded matches in the normal
   full-season first-run path:

   ```bash
   python cli.py --sync-afl-season <SEASON_YEAR>
   ```

   An operator who explicitly wants only canonical metadata/player membership
   without historical concluded-match statistics may omit this step.
5. Verify season completeness after synchronization, without changing the database:

   ```bash
   python cli.py --report-afl-season 2026
   ```

6. Verify the configured database at the SQLite level. The following portable
   Python/SQLite check confirms integrity, applied migrations, the club seed, and
   canonical season/player membership for the requested year:

   ```bash
   SEASON_YEAR=<SEASON_YEAR> python - <<'PY'
   import os, sqlite3
   import config
   conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
   assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
   assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] > 0
   assert conn.execute("SELECT COUNT(*) FROM clubs").fetchone() == (18,)
   year = int(os.environ["SEASON_YEAR"])
   season_id = conn.execute("SELECT afl_id FROM afl_seasons WHERE year=?", (year,)).fetchone()
   assert season_id, f"season {year} was not bootstrapped"
   assert conn.execute("SELECT COUNT(*) FROM rounds WHERE season_id=?", season_id).fetchone()[0] > 0
   assert conn.execute("SELECT COUNT(*) FROM competition_season_players WHERE competition_season_id=?", season_id).fetchone()[0] > 0
   print("bootstrap verification passed")
   PY
   ```

7. Only after those checks pass, start the services required by the deployment.
   API, Scheduler, and Admin are **not** required to migrate or bootstrap. See the
   [portable Compose service definitions](../compose.example.yaml), the
   [API/Admin deployment guidance](../README.md#-docker-and-deployment-layout),
   and [Scheduler startup guidance](../README.md#scheduler-startup-and-shutdown).
   In production, start API/readiness validation before resuming Scheduler writers,
   as specified by the release runbook.

## Common follow-up workflows

* **Refresh metadata and player membership:** rerun
  `python cli.py --bootstrap-afl-season <SEASON_YEAR>`. Its canonical upserts are
  the ordinary supported membership refresh; see
  [public AFL metadata collection](public_afl_metadata.md).
* **Collect one completed match’s authoritative statistics:** run
  `python cli.py --collect-match-player-stats CD_M20260142001 --afl-match-id 8001`;
  see the [match player-stat guide](match_player_stats.md) for lifecycle rules.
* **Investigate upstream data without database writes:** use public-only
  `python cli.py --collect-afl-metadata --afl-season <SEASON_YEAR> --print-json`,
  or the `--collect-afl-data ... --no-database` artifact form in the table when
  several endpoint families are needed.
* **Rerun current injuries:** run `python cli.py --scrape-injuries`; the operation
  replaces/updates supported current and history records through canonical player
  resolution. See the [source policy](operational_source_policy.md).
* **Validate a deployment:** check `/healthz`, then `/readyz`, and separately
  inspect `schema_migrations`; readiness proves database connectivity, not schema
  completeness. Use the exact gates in the
  [release runbook](operations/release_runbook_v0_5_0.md#13-health-and-readiness-verification).
* **Back up, restore, or roll back:** use the production-sensitive procedures in
  the [release runbook](operations/release_runbook_v0_5_0.md), not abbreviated
  commands copied into this guide.

## Command boundaries

* **Bootstrap versus collection:** bootstrap is the idempotent, persistent
  one-season metadata/player setup and refresh. Read-only metadata collection is
  diagnostic; database-free orchestration writes artifacts only. The implemented
  `--sync-afl-season` additionally processes eligible completed match statistics;
  follow the linked whole-season synchronization reference for its bounds and
  exit semantics.
* **Canonical versus legacy:** AFL/CFS JSON supplies canonical metadata, player
  membership, and authoritative `cfs_player_stats`. Explicit HTML match/stat
  commands are compatibility paths; legacy `player_stats` is not authoritative.
  HTML is never an automatic fallback or dual-write where source policy prohibits
  fallback. Injuries and lineups are deliberate supported HTML-backed operational
  sources, not fallbacks.
* **Match versus season:** `--collect-match-player-stats CD_M...` is exactly one
  match. Whole-season collection exists only as the verified
  `--sync-afl-season` operation; do not extrapolate unimplemented command names.
* **CLI versus services:** CLI operations run synchronously. Scheduler/Admin
  triggers use their documented service paths and may enqueue work; they are not
  aliases for bootstrap. `--collect-match-rosters` itself remains read-only and
  never populates any table directly. Canonical CFS roster persistence now
  exists as a separate production path (the scheduler and
  `collect_operational(OperationalDomain.MATCH_ROSTERS)`, Issue #219, see
  `docs/match_rosters.md`); this CLI command still never populates the
  operational HTML-backed `lineups` table either way, and remains distinct
  from that path.
* **One operation per invocation:** select only one top-level operation flag.
  Conflicts are rejected before runtime components load, as implemented for
  [Issue #110](https://github.com/JustPlausible/AFL-api/issues/110).

Season completeness/reconciliation reporting remains planned in
[Issue #107](https://github.com/JustPlausible/AFL-api/issues/107). No proposed
syntax is documented until it exists in the parser and dispatch implementation.

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

Successful default output summarises the resolved season and persisted entity
counts, then points to `--sync-afl-season` and the subsequent
`--report-afl-season` verification step. Add
`--print-json` to emit the complete structured diagnostic and bootstrap result;
this changes output only, never persistence.

## Preferred Champion Data/CFS JSON commands

Provider identifiers are opaque strings. A round provider ID must begin
`CD_R`; a match provider ID must begin `CD_M`. Numeric AFL IDs and the wrong
provider-ID family are rejected before network access.

```bash
python cli.py --collect-match-rosters CD_R202601421 --print-json
```

This collects CFS round selections and change records. The CLI command itself
is **read-only**: it never writes the database, so it never populates either
canonical roster persistence or the operational HTML-backed lineup tables.
Canonical roster persistence (`cfs_match_rosters`/`cfs_match_roster_selections`/
`cfs_match_roster_context`, migration `0024`) is now available separately
through the production scheduler and
`collect_operational(OperationalDomain.MATCH_ROSTERS)` — see
[match roster collection](match_rosters.md) — and backs
`GET /api/v1/matches/{match_id}/rosters`.

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

## Supported HTML-backed and explicit legacy commands

These commands use rendered AFL web pages and are not invoked as fallback by a
JSON command. Injury and operational lineup collection are supported persistent
workflows; only commands explicitly identified as compatibility paths use the
`legacy_persistent` mode:

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
| `--scrape-lineups ROUND` | AFL round number, such as `9` | Collects HTML lineups and persists the operational lineup tables. |
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

## API key management

`python cli.py --add-api-key/--list-api-keys/--remove-api-key` is the one
supported interface for managing API keys, both from the repository root in a
configured local environment and inside the built `afl-api` container. It
requires no `PYTHONPATH` configuration because `cli.py` lives at the repository root
and establishes the correct import context for the application's operator commands.
Earlier guidance recommended running `scripts/manage_api_keys.py` directly or
setting `PYTHONPATH=/app`; that form is no longer supported.
`scripts/manage_api_keys.py` is now an internal library module used by the operator
CLI and has no standalone `__main__` entry point.

```bash
python cli.py --add-api-key "2026-live"
python cli.py --list-api-keys
python cli.py --remove-api-key "2026-live"
python cli.py --grant-api-key-capability "2026-live" advanced-read
python cli.py --revoke-api-key-capability "2026-live" advanced-read
```

Inside the container, run the same command with `docker compose exec`:

```bash
docker compose exec afl-api python cli.py --add-api-key "2026-live"
docker compose exec afl-api python cli.py --list-api-keys
docker compose exec afl-api python cli.py --remove-api-key "2026-live"
```

All three operations open the configured `DB_PATH` (the same database used by
the running application) and never fall back to a different or default
location. `--add-api-key LABEL` generates a new high-entropy key, prints it
once, and stores only its SHA-256 hash plus an eight-character prefix;
the full key is not recoverable afterwards. `--list-api-keys` prints only the
label, prefix, and active status for each stored key — never the full key.
`--remove-api-key KEY_OR_LABEL` accepts either the previously presented full
key or the key's label. New and upgraded keys default to `standard-read` only.
Grant or revoke `advanced-read` by label with the commands above; listing shows
all capability names without exposing the full key. Endpoint enforcement is
deferred to Issue #156. See [API key storage migration](api_key_migration.md)
for the underlying storage/hashing contract.

## Output, raw capture, and diagnostics

`--print-json` prints the full collected/normalised payload for commands that
support it; without it JSON commands print a compact summary. It does not turn
a persistent command into a dry run. Shell redirection saves standard output:

```bash
python cli.py --bootstrap-afl-season 2026 --print-json > bootstrap-2026.json
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
`html` identifies HTML source technology; it does not by itself imply legacy
persistence. A composite can report
`public_afl_json+cfs_json`. Canonical current statistics write only
`cfs_player_stats`; explicit legacy HTML statistics write only `player_stats`.
Operational injuries and lineups deliberately remain HTML-backed. No command
uses these diagnostics to enable fallback or dual writes.

Season-sync JSON additionally separates each match's `collection_outcome`,
`persistence_outcome`, and `audit_outcome`. It includes exact
`rows_inserted`, `rows_updated`, `rows_unchanged`, and `rows_written` counts;
audit-only failures include the audit ID, shared correlation ID, redacted
`audit_error_class`/`audit_error_summary`, and `processing_continued`. The
season-level `audit_outcome` and `audit_failures` expose child or parent audit
finalisation problems without changing a committed persistence outcome.

Modes are `read_only` (no write), `database_free` (no database is opened and
the target is `none`), `persistent`, `legacy_persistent`, and `composite` for a
multi-stage operation. `legacy_persistent` is reserved for explicit compatibility
writers such as HTML player statistics targeting `player_stats`; supported HTML
injury and lineup operations are `persistent`. Stable statuses are `success`, `unchanged`, `partial`,
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
| `--scrape-lineups` | `persistent` | `html` | `scrape_afl_lineups` | opened; lineup tables | envelope; records with `--print-json` |
| `--scrape-injuries` | `persistent` | `html` | injury pipeline | opened; `injuries`, `injury_history` | compact/full JSON envelope |
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
No new persistence switch was added for rosters (Issue #219): canonical
selection identity/replacement semantics are now defined and roster
persistence exists (see `docs/match_rosters.md`), but it is reached through
the production scheduler and `collect_operational(OperationalDomain.MATCH_ROSTERS)`,
not a new CLI flag — `--collect-match-rosters` deliberately stays the existing
read-only diagnostic command rather than becoming a surprising persistent
write path, consistent with how metadata collection and metadata persistence
are already deliberately separated commands. Grouped subcommands and broader
source-selection redesign are also intentionally outside this guide's scope.

## Read-only season completeness report

Use the report after bootstrap, after whole-season sync, during an active season,
or when investigating a bounded match collection:

```bash
python cli.py --report-afl-season 2026
python cli.py --report-afl-season 2026 --print-json
```

The command resolves the season from the configured stable AFL competition code
and provider identity plus the requested year. It opens the configured `DB_PATH`
in SQLite `mode=ro`, enables `PRAGMA query_only`, makes no HTTP requests, invokes
no collector or migration, and creates no `scrape_runs` record. The database name
(rather than an absolute path) is included in metadata. The initial filter is
always `{"scope":"full_season"}`.

Human and JSON modes contain the same status and findings. JSON fields include
metadata, aggregates, status, severity counts, finding count, and the complete
finding list. Finding automation must use `code`, not `message`. For example:

```json
{
  "metadata": {"requested_season_year": 2026, "competition_code": "AFL", "filters": {"scope": "full_season"}},
  "aggregates": {"matches": 207, "concluded_matches": 24},
  "status": "incomplete",
  "severity_counts": {"error": 0, "warning": 1, "info": 3},
  "finding_count": 4,
  "findings": [{"code": "match.final_without_authoritative_stats", "severity": "warning", "domain": "matches", "match_id": 8001}]
}
```

### Severity, status, and exit policy

| Severity | Meaning |
|---|---|
| `error` | Unsafe contradiction, broken seasonal relationship, or authority/identity conflict. |
| `warning` | Actionable missing, unresolved, partial, or failed state. |
| `info` | Legitimate optional, unassigned, future, unpublished, or limited-audit state. |

| Overall status | Decision | Exit |
|---|---|---:|
| `invalid` | At least one `error`. | 1 |
| `incomplete` | No error, but a warning with an explicitly required-data code (missing season foundations/provider identity, concluded authoritative statistics, a complete two-sided snapshot, a conservative player-count floor, or authoritative-player season membership). | 1 |
| `usable_with_warnings` | Other warnings remain, but no unsafe or explicitly incomplete condition exists. | 0 |
| `complete` | No errors or warnings; informational findings are allowed. | 0 |

This order is the precise decision table. Status is not computed only from the
highest generic severity. Human and JSON modes use this same table and exit
policy; there is intentionally no `--strict` option. CLI syntax errors remain
argparse exit `2`.

### Interpretation and authority

A match belongs to the report through `matches.season_id = afl_seasons.afl_id`;
its round is validated through `matches.round_id = rounds.round_id` and
`rounds.season_id`, while its teams are validated through
`matches.home_team_id`/`away_team_id = afl_team_seasons.team_id` for that same
season. A player membership belongs through
`competition_season_players.competition_season_id = afl_seasons.afl_id`; an
optional team is validated by the composite membership relationship to
`afl_team_seasons`. A CFS row belongs through
`cfs_player_stats.match_provider_id = matches.match_provider_id`, followed by
`matches.season_id`. Player reconciliation additionally uses
`cfs_player_stats.canonical_player_id`, and Champion Data identity uses
`player_provider_ids(provider='champion_data', provider_player_id)`.

The schema enforces unique competition/season/team provider IDs, unique round
and match provider IDs (partial indexes), one mapping per provider identity and
one provider mapping per canonical player, one team-season pair, one
player-season membership, and one CFS `(match_provider_id,
champion_data_player_id)` row. Player/membership foreign keys and the composite
membership-team foreign key exist, although SQLite enforcement depends on the
connection's foreign-key setting. The older `matches` and `rounds` base tables
do not have declared foreign keys for all canonical columns, so the report
checks those application-convention relationships explicitly.

Canonical match lifecycle normalization recognizes `SCHEDULED`, `LIVE`,
`POSTGAME`, and `CONCLUDED`; `COMPLETED` and `FINAL` normalize to `CONCLUDED`.
Cancelled, postponed, bye, and other upstream values are retained but normalize
to unknown rather than being invented as concluded. Scheduled/future and all
non-concluded matches are excluded from final-stat failures. Opening Round and
finals are ordinary persisted rounds; no fixed season-match count is assumed.
Missing venue/time is informational because upstream publication is nullable.

Future finals may reference public-API placeholder teams that are not members of
the season's `/teams` response. Bootstrap persists their numeric reference in
`matches.home_team_id`/`away_team_id` and retains the complete embedded match
team objects in `home_json`/`away_json`; it does not create participating
`afl_teams` rows for them. The source exposes no explicit placeholder flag and
placeholder numeric/provider IDs are not treated as stable. The current reliable
sentinel is the embedded team's simultaneous `abbreviation="TBD"` and
`nickname="TBD"` values (names describe ladder positions or prior-final
winners and therefore vary). A future, non-concluded fixture whose otherwise
invalid participants all carry that sentinel produces one informational
`match.participants_unpublished` finding. This applies to both `PLACEHOLDER` and
`SCHEDULED` source statuses, so a scheduled Grand Final is not misclassified.
A concluded match that still has TBD participants, or any fixture with an
unrecognised/non-placeholder team outside season participation, retains the
unsafe `match.missing_team` error. The report does not hard-code team IDs, match
IDs, round names, dates, or a finals format.

Only `cfs_player_stats` is authoritative. Authority `1` is live/partial and `2`
is concluded; the writer protects higher authority and only permits equal-
authority observations with a non-older collection timestamp. Legacy
`player_stats` is counted only as compatibility evidence and never closes a CFS
gap. Zero authoritative rows use `match.final_without_authoritative_stats`;
one-sided or mixed-authority observations use
`match.partial_authoritative_stats`; and a two-sided concluded snapshot below
the named `MIN_CONCLUDED_AUTHORITATIVE_PLAYER_ROWS = 20` floor uses
`stats.suspicious_player_count`. The floor is deliberately much lower than an
expected AFL match-day total: it identifies obviously incomplete snapshots
without assuming an exact team-sheet or interchange rule. All three conditions
make the report `incomplete` and exit `1`.

Canonical player crosswalks use `canonical_players`, `player_provider_ids`, and
`cfs_player_stats.canonical_player_id`; the report never guesses or merges an
identity. A null `competition_season_players.team_id` is legitimate when the CFS
season-player source did not provide or could not resolve a team and is therefore
informational. A contradictory non-null relationship is unsafe.

For authority-2 rows, `side=home` must carry the stored home participant's team
provider ID and `side=away` must carry the away participant's provider ID.
`stats.team_participant_mismatch` is an unsafe contradiction (`invalid`);
`stats.team_provider_unavailable` is informational when either identity is null,
because the report does not infer a missing identity. The CFS normaliser reads
`teamId` from each player context and the writer persists it unchanged, but the
field is optional in the current source/persistence contract and real concluded
responses may omit it for every row. The report therefore retains aggregate
counts of unavailable authority-2 rows and emits at most one structured
informational finding per match, with home and away counts, rather than one per
side or player. When supplied, CFS `CD_T...` values and canonical
`afl_teams.provider_id` share the Champion Data team namespace and can be
compared safely. A known canonical player in requested-season authoritative
statistics without a matching
`competition_season_players` row produces
`stats.player_missing_season_membership` and makes the report `incomplete`.
Statistic season scope is derived only through the persisted statistic-to-match
provider join and the match's `season_id`; player membership is never used to
infer the intended season of a statistic. Consequently, legitimate historical
statistics for a continuing player are not findings in a later-season report.

`match.duplicate_provider_id` is an `error` and makes the report `invalid`.
Healthy migrated databases normally prevent it through the partial unique
SQLite index `idx_matches_provider_id` for non-null values; the report retains a
defensive check for damaged, legacy, or manually altered schemas. Multiple null
provider IDs remain missing/unpublished identity, not duplicates.

Audit correlation is deliberately limited to `scrape_runs` records whose target
is the exact Champion Data match ID and whose scrape type is
`season_match_player_stats` or `match_player_stats`. Status, row counts, and
`started_at` are reliable at that boundary. Older `scrape_log` and
`scrape_summary`, database-free collection, read-only commands, metadata audit
records without a reliable season target, and heuristic correlation are not
promoted to authoritative evidence. Consequently `audit.no_successful_stat_run`
is informational rather than proof that collection never occurred.

Common remediation paths are:

```bash
python cli.py --bootstrap-afl-season 2026
python cli.py --sync-afl-season 2026
python cli.py --collect-match-player-stats CD_M20260142001 --afl-match-id 8001
```

Bootstrap supplies foundations; Issue #106 season sync performs bounded,
idempotent collection; the single-match command targets one concluded gap.
Reporting itself never repairs, backfills, collects, updates snapshot authority,
uses HTML fallback, or writes an audit. It does not validate injuries, CFS roster
persistence, consumer scoring, or require every season member to record a stat.
