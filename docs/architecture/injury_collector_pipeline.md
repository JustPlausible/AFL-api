# Injury collector pipeline and reference boundaries

The injury collector is the reference implementation for a rendered-HTML
collector. Its production path is deliberately linear:

```text
InjuryAcquirer -> parse_injuries_html -> InjuryResolver
               -> InjuryPersistenceAdapter -> collect_injuries audit outcome
```

## Module and data boundaries

| Stage | Module | Input | Output and responsibility |
| --- | --- | --- | --- |
| Acquisition | `scraper/injuries/acquisition.py` | URL/selectors and timeout configuration | `InjurySourceDocument`: raw HTML, source URL, UTC acquisition time and elapsed milliseconds. This adapter alone imports Playwright and owns browser startup, navigation, waiting and cleanup. |
| Parsing | `scraper/injuries/parser.py` | Caller-supplied raw HTML | `InjuryParseResult` containing raw `ParsedInjuryRecord` values, team count, parser diagnostics, and `teams`: one `ParsedTeamBlock` per recognised team block, **including blocks with zero player rows**. This is what makes a team's observed coverage explicit and independent of whether it happened to have any rows. It performs no I/O or identity lookup. |
| Resolution | `scraper/injuries/resolution.py` | Parsed records plus the canonical repository connection | `InjuryResolutionResult` with explicit resolved, unresolved or ambiguous records and diagnostics, plus `observed_teams`: one `ResolvedTeamCoverage` per parsed team block, each carrying the resolved canonical `team_id` (from the same canonical club seed used elsewhere) when its club marker itself resolves, or `status="unresolved"` when it does not. Club artwork/alt markers use shared canonical club data; players use the existing club-scoped `CanonicalInjuryPlayerResolver`, which also supplies `canonical_player_id` on each resolved record. |
| Persistence | `scraper/injuries/persistence.py` | Resolved records, observed-team coverage, and source metadata | `InjuryPersistenceResult` counts, status, and `teams_observed`. It owns commit/rollback, skips unsafe identities, persists `canonical_player_id`/`canonical_team_id` on every resolved row, and scopes expiry to observed-team authority (see below) rather than whole-page authority. |
| Orchestration | `scraper/injuries/orchestration.py` | Injected/default stage adapters and an application connection | `InjuryCollectionOutcome`, including `teams_observed`. It alone owns scrape-run start, complete/partial/failure lifecycle, source-coverage provenance (`diagnostic_summary` on the scrape run), and operational composition. |

`collection.source_policy.collect_operational()` remains the policy-selected
application wrapper. CLI, daily Scheduler and Admin/manual operations all call
that wrapper; it delegates injuries to `collect_injuries()` and does not create
a competing audit record. The old `scraper.scrape_afl_injuries` functions remain
compatibility surfaces, not the production composition path.

## Observed-team-authority persistence (Issue #213)

The AFL injury page is not always authoritative for the whole competition --
during finals it has been observed showing only the teams still competing,
not all 18 AFL teams. **A team omitted from the page is not evidence that
team has zero injuries.** Persistence therefore never treats "not mentioned
on this page" the same as "confirmed no injuries":

```text
Team block present on the latest page (club marker itself resolves):
    listed injury row      -> current
    previously-current row for that same team, now absent from the page -> expired
    (this holds even when the team's block has zero rows: that is an
    authoritative empty list for that team, not missing information)

Team block absent from the page entirely,
or present but its club marker does not resolve to a canonical team:
    previously-current rows for that team are left untouched
    (untouched, because it is not safely known whether -- or which -- team
    an unresolved block's rows belong to)
```

Concretely, `InjuryPersistenceAdapter.persist()` scopes its expiry `UPDATE` to
`canonical_team_id IN (<resolved observed team ids>)`, rather than expiring
every previously-current row not present in the latest scrape. A team whose
`canonical_team_id` is not in that set -- because it never appeared on the
page, or its club marker did not resolve -- keeps its existing state exactly
as before this scrape ran.

The pre-existing safety rule is retained unchanged and still applies
page-wide: **any** unresolved or ambiguous player-identity row anywhere on
the page blocks expiry entirely for this scrape, not just for its own team.
This is deliberately coarser than the per-team expiry scoping above -- it is
the existing, already-tested safety net, not narrowed by this change.

## Canonical identity persistence (Issue #213)

`injuries.canonical_player_id` and `injuries.canonical_team_id` (migration
`0022_injury_canonical_identity.py`) are populated **at collection time** by
the resolver, not derived later at read time:

* `canonical_player_id` comes directly from `CanonicalInjuryPlayerResolver`
  (via `merge.helpers`), the same resolver already used by the legacy
  compatibility path.
* `canonical_team_id` comes from the same canonical club seed
  (`bootstrap/clubs.json`'s `teamId`, the same identifier space as
  `afl_teams.afl_id`) already used to resolve the source club marker for
  player-identity scoping -- it is not a second, independently-derived team
  identity.

Existing rows were backfilled only where deterministic: `canonical_player_id`
only when a row's `afl_id` maps to exactly one canonical player via
`player_provider_ids` (itself `UNIQUE(provider, provider_player_id)`, so this
mapping can never be genuinely ambiguous -- only present or absent);
`canonical_team_id` only when the persisted club code matches a known
canonical club. An unresolved legacy mapping is left explicitly `null`, never
guessed.

## `/api/v1/injuries` (Issue #213)

`GET /api/v1/injuries` (`api/routes_v1.py`) exposes current injuries using
this canonical identity directly, filterable by `team_id` and
`canonical_player_id`. See `docs/api_v1_injuries.md` for the full consumer
contract. It reads only rows with a resolved `canonical_player_id` --
unresolved/legacy rows are omitted rather than exposed under an invented or
provider-only identity, consistent with the rest of `/api/v1`.

## Source-coverage provenance

`collect_injuries()` reuses the existing scrape-run audit record
(`db/scrape_runs.py`) rather than a parallel logging mechanism: each run's
`diagnostic_summary` records `source_url`, `observed_team_count`, the list of
`observed_resolved_teams` (canonical club codes whose block resolved on this
page), and the parsed/persisted row counts and status. This is what lets an
operator later distinguish "this team had zero injuries this run" from "this
team was not on the page at all" without re-deriving it from the `injuries`
table's `current` flags alone.

## Acquisition decision: Playwright retained (re-affirmed 2026-08-24)

The maintained contract is the rendered `article__body` and its editorial
tables. Repository investigation has not identified a maintained public AFL or
CFS JSON injury resource with equivalent data, so changing to plain HTTP would
risk incomplete markup. Playwright remains isolated in a replaceable adapter.

Issue #213 asked that this decision be re-tested by fetching the live page
directly (plain HTTP vs. Playwright vs. any structured source) before
concluding. That comparison was attempted on 2026-08-24 and was blocked
identically to the prior 2026-07-28 investigation: this execution
environment's own network egress policy refuses `www.afl.com.au` outright
(`403`/`EGRESS_BLOCKED`) before any origin response -- HTTP or
browser-rendered -- can be observed. See
`docs/investigation/afl_injury_finals_evidence_capture_2026-08-24.md` for the
full record of that attempt. Because no live response was obtainable by
either method, this decision is **unchanged, not re-confirmed**: Playwright
is retained because parity could not be tested, not because it was proven
necessary. Re-running the comparison from an unrestricted network remains
required follow-up work before this conclusion can be revisited.

## Reference pattern for future collectors

The reusable idea is the boundary, not a universal base class. Acquisition may
use Playwright, plain HTTP, a JSON client, or another source-specific mechanism;
parsers and normalisers should accept supplied content and stay independent of
persistence; identity resolution should be a separate domain stage where it is
needed; persistence should consume structured records; and orchestration should
own audit state and operational composition.

Injury-specific details remain the editorial comment/image club marker, the
three-column table, canonical injury player resolver, and injury current/history
semantics. A lineup follow-up should separate its interactive round selection
and expand controls, HTML parsing, player IDs and writer without forcing either
collector into inheritance or pretending their browser interactions are equal.

## Pilot lessons and limitations

* Raw upstream markers must survive parsing so changed club artwork becomes an
  explicit resolution diagnostic rather than a guessed identity.
* Partial identity failure must not prevent safe writes or expire previous rows.
* Process termination cannot run exception cleanup; documented stale-run
  recovery remains the protection for that exceptional case.
* Editorial markup remains upstream fragility. Offline golden fixtures protect
  the observed contract; a live smoke test remains an operator release check.
