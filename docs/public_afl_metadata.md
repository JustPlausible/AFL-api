# Public AFL metadata collection

## First-run season bootstrap

After creating a clean database, migrate it and persist the prerequisite AFL
competition hierarchy with:

```bash
python -m db.migrate
python cli.py --bootstrap-afl-season 2026
```

The command resolves the Premiership competition and requested year through
the public metadata API, then inserts or updates the competition, season,
team-season links, rounds and matches. It next uses the existing normalized CFS
season-player and public player-ID-map collectors to transactionally persist
canonical players, separate provider mappings, and player-season membership.
It prints metadata and player counts, diagnostics, missing team-link counts and
the player collection state. Repeating it is safe.

`afl_teams` stores stable club identity (`afl_id` and provider crosswalk plus
display metadata); it has no season ownership. `afl_team_seasons` is the sole
source of truth for participation in a competition season. Consequently,
bootstrapping 2025, 2024, and 2023 in any order retains all three sets of
memberships, while repeatedly bootstrapping one season remains idempotent.
Player membership uses the same `(competition_season_id, team_id)` relationship.

Recommended new-installation settings are `AFL_COMPETITION_CODE=AFL`,
`AFL_COMPETITION_PROVIDER_ID=CD_C014`, and `AFL_SEASON_YEAR=2026`. An explicit
`--bootstrap-afl-season YEAR`, `--afl-competition-code`, or
`--afl-competition-provider-id` takes precedence over its environment default.
Legacy numeric settings remain available to legacy scrapers.

For `--bootstrap-afl-season`/`--sync-afl-season` specifically, `AFL_SEASON_YEAR`
plays a second, independent role: it declares which season is canonically
current (`afl_seasons.is_current`), separately from which season a given run
persists. Bootstrapping or syncing an explicit, e.g. historical, season (say
`--bootstrap-afl-season 2025` with `AFL_SEASON_YEAR=2026`) persists that
season's data without disturbing the already-established 2026 current-season
marker. When `AFL_SEASON_YEAR` is unset, or does not resolve to exactly one
season in the fetched competition-season list, current-season determination
falls back to an unambiguous upstream `current` flag, then to the date range
spanned by the collected season's own rounds; if none of those resolves
unambiguously, no season is marked current for that run rather than guessing
one.

Add `--afl-raw-directory PATH` to retain deterministic raw API responses for
diagnostics. The bootstrap does not start the scheduler or persist match
selections, lineups, injuries, or player stats. CFS authentication is required
for the season-player population. `unavailable` and a valid `empty` population
are reported distinctly and neither deletes existing membership;
authentication errors remain errors and never trigger a legacy HTML fallback.

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

The ID map is authoritative for the AFL numeric ID to Champion Data player-ID
crosswalk. The season-player response is authoritative for membership in the
requested Champion Data competition season and supplies a Champion Data team
ID when available. That team ID is linked only when it matches a team in the
metadata-bootstrap team-season association. No provider ID is transformed into
another provider's ID, and unresolved team links remain null with diagnostics.

## v0.5.0 persistence boundary

v0.5.0 persists the AFL metadata hierarchy, canonical player identities and
provider mappings, competition-season player membership, reliable season team
links, and current CFS match player statistics. It does not promise a complete
model of every discovered AFL domain, complete historical roster
reconstruction, publication-safe persistence of every match selection,
automatic deletion after partial/unavailable responses, or removal of all
legacy player and enrichment tables. Match roster persistence is deliberately
deferred to v0.5.1.
