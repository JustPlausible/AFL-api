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
| Parsing | `scraper/injuries/parser.py` | Caller-supplied raw HTML | `InjuryParseResult` containing raw `ParsedInjuryRecord` values, team count and parser diagnostics. It performs no I/O or identity lookup. |
| Resolution | `scraper/injuries/resolution.py` | Parsed records plus the canonical repository connection | `InjuryResolutionResult` with explicit resolved, unresolved or ambiguous records and diagnostics. Club artwork/alt markers use shared canonical club data; players use the existing club-scoped `CanonicalInjuryPlayerResolver`. |
| Persistence | `scraper/injuries/persistence.py` | Resolved records and source metadata | `InjuryPersistenceResult` counts and status. It owns commit/rollback, skips unsafe identities, and retains existing injury history/current-row safety. |
| Orchestration | `scraper/injuries/orchestration.py` | Injected/default stage adapters and an application connection | `InjuryCollectionOutcome`. It alone owns scrape-run start, complete/partial/failure lifecycle and operational composition. |

`collection.source_policy.collect_operational()` remains the policy-selected
application wrapper. CLI, daily Scheduler and Admin/manual operations all call
that wrapper; it delegates injuries to `collect_injuries()` and does not create
a competing audit record. The old `scraper.scrape_afl_injuries` functions remain
compatibility surfaces, not the production composition path.

## Why acquisition remains Playwright

The maintained contract is the rendered `article__body` and its editorial
tables. Repository investigation has not identified a maintained public AFL or
CFS JSON injury resource with equivalent data, so changing to plain HTTP would
risk incomplete markup. Playwright remains isolated in a replaceable adapter.

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
