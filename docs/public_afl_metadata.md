# Public AFL metadata collection

## First-run season bootstrap

After creating a clean database, migrate it and persist the prerequisite AFL
competition hierarchy with:

```bash
python -m db.migrate
python cli.py --bootstrap-afl-season 2026
```

The command resolves the Premiership competition and requested year through
the public metadata API, then atomically inserts or updates the competition,
season, teams, rounds and matches. It prints counts for records read, inserted,
updated, unchanged and failed. Repeating it is safe: stable AFL identifiers are
used as keys, while fixture, venue, score and status changes are updated.

Recommended new-installation settings are `AFL_COMPETITION_CODE=AFL`,
`AFL_COMPETITION_PROVIDER_ID=CD_C014`, and `AFL_SEASON_YEAR=2026`. An explicit
`--bootstrap-afl-season YEAR`, `--afl-competition-code`, or
`--afl-competition-provider-id` takes precedence over its environment default.
Legacy numeric settings remain available to legacy scrapers.

Add `--afl-raw-directory PATH` to retain deterministic raw API responses for
diagnostics. The bootstrap only collects and persists public metadata: it does
not start the scheduler or collect rosters, lineups, injuries, or player stats.

## Read-only diagnostics

The public JSON collector discovers and normalises the AFL metadata hierarchy
without writing it to the application database:

```text
competition -> competition season -> rounds -> teams -> matches
```

Run it through the existing CLI and select a season explicitly when required:

```bash
python cli.py --collect-afl-metadata --afl-season 2026 --print-json
```

`--print-json` emits the full normalised hierarchy to standard output. Save it
with ordinary shell redirection when a file is wanted:

```bash
python cli.py --collect-afl-metadata --afl-season 2026 --print-json > metadata-2026.json
```

The Premiership defaults to the stable `AFL` competition code and `CD_C014`
provider ID. These can be changed with `--afl-competition-code` and
`--afl-competition-provider-id`. If no season is supplied, the collector uses
an unambiguous current flag or a season date range containing today's date; it
fails with guidance rather than choosing by numeric ordering.

Raw source responses are disabled by default. Opt in for investigation or a
dry run by providing a dedicated directory:

```bash
python cli.py --collect-afl-metadata --afl-season 2026 \
  --afl-raw-directory data/collection-dry-run
```

Captures are JSON files grouped by endpoint, with deterministic filenames that
include relevant scope IDs and the response page. They are separate from the
normalised result printed by `--print-json`; request headers and credentials are
never included. In other words, `--afl-raw-directory PATH` stores the original
per-endpoint/per-page API responses under `PATH`; it is not a destination for
the normalised output. There is currently no `--afl-json-output` option.

The public response shapes can gain new fields. Normalised records therefore
include a `source` copy of each record in addition to the currently understood
identity, name, reference, time, score, bye and metadata fields. No mapping is
invented for undocumented fields.

## Player identity collection

`PublicAflCollector.player_id_map()` independently collects the public mapping
from Champion Data player IDs to AFL numeric IDs. `season_players(provider_id)`
collects the practical CFS population for one provider season, first checking
the endpoint's default response against `players.Count` (when present) and
`totalResults`, then explicitly paging from page one only if it is incomplete.

`collect_players(provider_id)` joins those sources and returns separate
`players` identities and `player_seasons` associations. Unmapped identities are
retained with a null AFL ID; malformed, duplicate, contradictory, count and
unmapped cases are returned as structured diagnostics. When raw capture is
enabled, both endpoints use the same endpoint/scope/page filename convention as
the metadata hierarchy.
