# Season Synchronisation Workflow Design

**Status:** Draft for implementation review  
**Related issue:** #106 — Implement an idempotent whole-season data synchronisation workflow  
**Intended milestone:** Post-v0.5.0 season data product work

## 1. Background

AFL-api currently provides focused collectors and bootstrap operations for individual data domains. These commands are useful for development and diagnostics, but an operator must understand which commands to run, in what order, and which identifiers each operation requires.

The next practical step is a supported workflow that can prepare a useful local AFL dataset for an entire competition season.

The immediate use cases are:

- testing a fresh installation and database;
- creating a populated database for development or proof-of-concept work;
- loading the current AFL season on a new installation;
- loading a completed historical season for analysis;
- rerunning an active season to collect newly available or finalised data;
- providing a predictable foundation for later scheduling and consumer-facing APIs.

The workflow must remain consumer-neutral. It supplies authoritative AFL data and does not implement downstream scoring, presentation, league-management, or other application-specific behaviour.

## 2. Goals

The season synchronisation workflow should:

1. Provide one supported operation for discovering and synchronising a published AFL season.
2. Work for both active and completed historical seasons.
3. Persist the available season structure, rounds, fixtures, matches, canonical players, provider mappings, and season membership using existing authoritative collectors and persistence services.
4. Collect authoritative CFS player statistics for matches that have started.
5. Be safe and efficient to rerun.
6. Continue after bounded per-match failures rather than abandoning the whole season.
7. Make progress, request volume, duration, persistence outcomes, skips, failures, and remaining gaps visible to the operator.
8. Support both an interactive first-run experience and a direct non-interactive CLI command through one shared workflow service.
9. Use the database itself as the resume checkpoint.
10. Establish a documented workflow pattern that can later be reused by other orchestration features.

## 3. Non-goals

The first implementation will not:

- calculate fantasy or consumer-defined scores;
- manage downstream teams, leagues, rosters, ladders, or reports;
- replace existing focused collector commands;
- persist canonical CFS lineup or roster history unless already required by existing bootstrap services;
- perform broad legacy-table retirement or reconciliation;
- parallelise match-stat requests;
- automatically schedule future updates;
- expose the workflow through FastAPI or the Admin UI;
- treat a dry run as a full network fetch without persistence;
- bypass source-authority or snapshot-monotonicity protections;
- guarantee that unavailable source data can be completed in one run.

## 4. Architectural position

The workflow belongs between the user-facing entry points and the existing collector/bootstrap services.

```text
CLI / future Admin / future Scheduler / future API trigger
                         |
                         v
               Season sync workflow
                         |
                         v
       Existing collectors and bootstrap services
                         |
                         v
             Existing persistence layer
```

The workflow is responsible for:

- deciding which existing services to call;
- ordering those calls;
- discovering the work to perform;
- constructing an execution plan;
- deciding when work can be skipped;
- applying retry and continuation policy;
- reporting progress and results.

The workflow must not duplicate data-source parsing or persistence logic already owned by collectors and repositories.

A useful general pattern is:

> Discover → Plan → Persist foundations → Collect bounded units → Reconcile outcomes → Report

## 5. Entry points
Note: Command names shown in this document describe the proposed interface for Issue #106. They are architectural design targets and are not yet implemented.

### 5.1 Direct season synchronisation

The direct command is intended for experienced operators, development, testing, scripts, and later automation.

```text
Proposed command:
  --sync-afl-season YEAR
```

The exact parser structure may follow existing CLI conventions, but the command must be non-interactive when the required year is supplied.

Initial options should include:

```text
--sync-afl-season YEAR
--through-round NUMBER
--force-refresh
--dry-run
--report-file [PATH]
--no-progress
```

The full discovered season is the default scope. `--through-round` is the only reduced season scope required initially.

### 5.2 Guided first-run workflow

The guided command is intended for a new installation or an operator who does not yet know the available season identifiers.

```text
Proposed command:
  --aflapi-first-run
```

The guided flow must:

1. resolve and display the configured database path;
2. create the database and parent directory when required;
3. apply all required migrations;
4. verify that the schema is current;
5. discover available AFL competition seasons;
6. identify and recommend the current active season where possible;
7. allow another season to be selected;
8. allow the operator to finish after database preparation without loading data;
9. gather sync options;
10. display the proposed execution plan;
11. request confirmation before bulk collection;
12. invoke the same underlying season-sync service as direct mode.

Example first-run menu:

```text
Database successfully prepared.

Database:
  /app/data/afl_players.db

What would you like to do?

  1. Load the current AFL season (recommended)
  2. Load another AFL season
  3. Finish now
```

The interactive wrapper must not implement a separate collection path.

## 6. Database readiness

### 6.1 First-run mode

`--aflapi-first-run` owns infrastructure preparation. It may create the database and apply migrations before offering season selection.

If database creation or migration fails, the workflow must stop before AFL collection and provide a clear recovery message.

### 6.2 Direct sync mode

`--sync-afl-season` is an operational data command. It must validate rather than silently prepare infrastructure.

Before network discovery it should:

- verify that the configured database exists;
- verify that required migrations have been applied;
- verify that the schema required by the season workflow is available;
- fail clearly if the database is missing or outdated;
- direct the operator to the migration command or `--aflapi-first-run`.

A mistaken `DB_PATH` must not silently create an empty database during direct season sync.

Design principle:

> First-run mode may prepare infrastructure; operational sync mode validates infrastructure.

## 7. Discovery and planning

The workflow must discover season structure from AFL JSON sources. It must not assume a fixed number of rounds or matches.

Discovery should determine, as available:

- competition;
- competition season;
- season year and source identifiers;
- active or completed season state;
- published rounds;
- published fixtures and matches;
- match provider identifiers;
- scheduled start times;
- current canonical match lifecycle;
- local persistence state;
- whether each eligible match already has a concluded-authority player-stat snapshot.

The planner then classifies every discovered match into a planned action or skip reason.

### 7.1 Player-stat eligibility

Normal season sync should request player statistics for matches whose resolved lifecycle is:

- `LIVE`;
- `POSTGAME`;
- `CONCLUDED` when no concluded-authority snapshot is already stored.

It should not request player statistics for clearly `SCHEDULED` future matches.

If status is missing or unrecognised, a match may be considered eligible when its scheduled start time is in the past. This fallback must be reported explicitly.

All discovered match metadata should still be persisted according to existing source-authority rules, including future and live matches.

### 7.2 Default rerun policy

A normal rerun should:

- collect newly eligible matches;
- refresh previously collected `LIVE` or `POSTGAME` matches;
- collect concluded matches that do not yet have concluded-authority statistics;
- skip matches whose concluded-authority snapshot is already stored;
- skip scheduled future matches.

### 7.3 Force refresh

`--force-refresh` bypasses the planner's normal final-snapshot skip and re-requests every eligible match.

It does **not** bypass persistence authority, validation, or monotonic lifecycle rules.

Force refresh therefore means:

> Ask the source again.

It does not mean:

> Overwrite useful data regardless of the returned observation.

An unavailable, malformed, partial, live, or otherwise lower-authority response must not replace a useful concluded snapshot.

### 7.4 Plan presentation

Discovery should make only the requests necessary to construct the season plan. Before bulk collection, guided mode should display the planned scope and request confirmation.

Example:

```text
Season: 2026 AFL Premiership Season
Rounds discovered: 24
Matches discovered: 207

Planned player-stat actions:
  138 new eligible matches
  7 live/postgame matches to refresh
  42 concluded matches already final — skipped
  20 scheduled matches — metadata only

Estimated CFS player-stat requests: 145
Proceed? [Y/n]
```

Direct mode proceeds without confirmation when the required options are supplied.

## 8. Dry-run behaviour

`--dry-run` should:

- perform season discovery;
- inspect local database state;
- construct the execution plan;
- show or save the plan;
- avoid player-stat requests;
- avoid persistence changes;
- clearly report that no data was written.

Dry run is intended as a low-impact planning operation. A future fetch-without-persist diagnostic mode, if needed, should be a separate feature.

## 9. Metadata and player foundations

Before match-stat collection, the workflow should call the existing authoritative services needed to establish season foundations.

The exact implementation should reuse current bootstrap capabilities and inspect their present contracts, but the intended resulting local state includes:

- competition and season metadata;
- teams;
- rounds;
- fixtures and matches;
- canonical players;
- provider-player mappings;
- season-player membership.

The workflow must not introduce a second representation when an established canonical table and persistence service already exists.

Foundation persistence should complete before the player-stat phase begins so match and player crosswalks are available to downstream collectors.

## 10. Execution model

### 10.1 Sequential processing

The initial implementation processes one match at a time.

For each planned match:

1. collect the CFS player-stat observation;
2. validate and normalise through the existing collector;
3. persist accepted records within a bounded match transaction;
4. commit that match as one unit;
5. record metrics and outcome;
6. continue to the next match.

Sequential processing is an explicit conservative choice because it:

- limits pressure on AFL/CFS services;
- keeps SQLite writes simple;
- makes progress and request accounting predictable;
- simplifies interruption and failure handling;
- provides baseline timing evidence before considering bounded concurrency.

No concurrency option is required in the first version.

### 10.2 Transaction boundaries

Player-stat persistence should be atomic per match.

A failure during one match must roll back that match's uncommitted changes without affecting previously committed matches.

The whole season must not be wrapped in one database transaction.

### 10.3 Idempotency

The workflow must rely on existing natural keys and upsert protections.

A repeated run should:

- avoid requesting already final matches by default;
- produce zero meaningful writes for identical observations;
- update same-authority observations only where the established persistence contract allows;
- allow concluded data to replace live or unknown snapshots;
- refuse lower-authority observations that would downgrade concluded data.

## 11. Retry policy

Each eligible match-stat request receives:

- one initial attempt;
- up to two additional attempts for likely transient failures;
- short increasing delays between attempts;
- server-provided retry guidance where available.

Likely retryable failures include:

- timeouts;
- connection resets;
- temporary DNS or network failures;
- HTTP rate limiting;
- temporary 5xx responses;
- recoverable short-lived authentication/token failures.

Do not automatically retry outcomes such as:

- explicitly unavailable or unpublished data;
- valid empty publication;
- malformed source shape;
- validation failures caused by repeatable bad source data;
- missing required source identifiers.

The report must record attempt counts, retry delays, and final outcomes. Exhausting retries for one match must not stop the remaining season.

## 12. Progress reporting

The workflow should use named stages:

```text
[1/5] Discover season structure       complete
[2/5] Build execution plan            complete
[3/5] Persist season metadata         complete
[4/5] Collect player statistics       64% (93/145)
[5/5] Finalise report                 pending
```

Discovery and finalisation use stage status rather than guessed percentages.

Once the execution plan is known, the player-stat phase uses the number of planned match actions as its denominator.

Interactive terminals may display a dynamic progress bar. Redirected output, container logs, CI, or other non-interactive output must use stable periodic status lines rather than terminal control sequences.

`--no-progress` suppresses dynamic progress reporting but must not suppress warnings or the final summary.

## 13. Interruption and resume behaviour

On `Ctrl+C`:

- stop scheduling new matches;
- allow the current network operation to terminate;
- roll back an in-progress match transaction;
- retain all previously committed matches;
- show the best available partial summary;
- write a requested partial report with an interrupted outcome;
- exit with interruption status;
- allow the next normal run to derive remaining work from the database.

No separate checkpoint file is required.

Design principle:

> The database is the resume checkpoint.

## 14. Failure containment

A season sync contains two failure classes.

### 14.1 Match-scoped failures

Examples:

- a request fails after bounded retries;
- one response is malformed;
- one match cannot be normalised or persisted.

These should be recorded and the workflow should continue with the next match.

### 14.2 Workflow-fatal failures

Examples:

- the requested season cannot be discovered;
- required authentication or configuration is unavailable;
- the database is missing or outdated in direct mode;
- a migration fails during first-run mode;
- foundational persistence cannot complete safely;
- the database becomes unusable.

These prevent meaningful continuation and should stop the workflow with a clear diagnostic.

## 15. Outcomes and exit codes

The command should distinguish at least:

- `0` — completed successfully; legitimate unavailable or unpublished matches may remain;
- `2` — partial success; one or more planned eligible matches failed after retries;
- `130` — interrupted by the operator;
- another documented non-zero code — workflow-fatal error.

The final output and report must include a semantic outcome such as:

```text
Outcome: partial_success
Exit code: 2
Failed matches: 3
```

The implementation should align any additional fatal exit code with existing CLI conventions where possible.

## 16. Reporting

A concise human-readable summary should always be printed at completion or interruption.

Example:

```text
Season sync complete

Season: 2026 AFL Premiership Season
Matches discovered: 207
Player-stat requests: 145
Successful collections: 142
Unavailable: 2
Failed: 1
Skipped as already final: 42
Scheduled matches skipped: 20
Rows written: 6,214
Elapsed time: 8m 17s
```

### 16.1 Detailed report files

Guided mode should offer to save a detailed report.

Direct mode should support:

```text
--report-file
--report-file PATH
```

If no path is supplied, the workflow should generate a predictable timestamped JSON path under an appropriate data/report directory.

The final CLI output must identify every report or diagnostic file written.

A structured JSON report is the preferred persistent format because the terminal summary already provides the human-readable view.

### 16.2 Report contents

The detailed report should include:

- workflow version or report schema version;
- run identifier;
- start and end timestamps;
- run mode: normal, force refresh, or dry run;
- interactive/direct entry point;
- database path;
- requested and resolved season;
- requested `through_round`, if any;
- discovered rounds and matches;
- planned actions by category;
- executed requests;
- request attempts and retries;
- skip reasons;
- successful, unavailable, empty, partial, failed, and interrupted outcomes;
- rows inserted or updated where available;
- diagnostics by match;
- output file locations;
- semantic outcome and exit code.

### 16.3 Timing and request metrics

To support future performance work, capture at least:

- total elapsed time;
- discovery duration;
- planning duration;
- foundation persistence duration;
- cumulative player-stat request time;
- average and slowest player-stat request;
- cumulative normalisation/persistence time where practical;
- average per-match processing time;
- total source requests by endpoint or collector;
- retry count and cumulative retry delay.

These measurements will provide evidence for any later bounded-parallel design.

## 17. Source authority and data safety

Season sync orchestrates existing source contracts; it does not redefine them.

In particular:

- CFS JSON remains the authoritative source for canonical match player statistics;
- `cfs_player_stats` remains the persistence target for those observations;
- live and postgame observations remain lower authority than concluded observations;
- endpoint and canonical match statuses retain separate provenance where already supported;
- future or lower-authority observations cannot downgrade a concluded snapshot;
- explicit legacy HTML collection remains outside normal season sync;
- force refresh does not weaken any of these rules.

Network retry policy and persistence authority are separate concerns.

## 18. Audit integration

The workflow should integrate with existing audit/run-tracking infrastructure rather than inventing a separate incompatible system.

At minimum, the workflow should make visible:

- the overall season-sync run;
- child or per-match outcomes where the existing audit model supports them;
- entry point and run options;
- start/end state;
- request and persistence counts;
- partial, failed, or interrupted outcome;
- report file location.

Implementation should inspect current audit contracts and choose the smallest extension necessary.

## 19. Testing strategy

The workflow requires deterministic offline tests.

Coverage should include:

- active season discovery;
- completed historical season discovery;
- `--through-round` planning;
- scheduled, live, postgame, concluded, and unknown match classifications;
- concluded snapshot skip behaviour;
- force refresh fetching without destructive downgrade;
- dry run performing no writes and no player-stat bulk requests;
- idempotent rerun;
- live snapshot later replaced by concluded data;
- one match failing while later matches continue;
- bounded retry success and exhaustion;
- unavailable and empty publication;
- malformed match payload containment;
- per-match transaction rollback;
- interruption after prior successful commits;
- partial-success and fatal exit codes;
- report generation and schema;
- interactive confirmation and cancellation;
- non-interactive/no-progress output behaviour;
- database readiness differences between first-run and direct sync.

Live AFL/CFS checks may supplement but must not replace offline tests.

## 20. Future extensions

Potential follow-up work includes:

- season completeness and reconciliation reporting (#107);
- scheduled incremental season refresh;
- Admin and Scheduler entry points using the same workflow service;
- canonical read APIs over the resulting season data;
- bounded request concurrency after timing data is available;
- configurable historical backfill workflows;
- additional workflow documents under `docs/architecture/workflows/`;
- richer alerting for stale or repeatedly failed season data.

## 21. Implementation questions to resolve against current code

The implementation PR should explicitly resolve these repository-specific details without changing the agreed behaviour above:

1. Which existing bootstrap functions can be composed directly, and which require a thin service wrapper?
2. What is the current migration-readiness API and canonical method for creating a fresh database?
3. Which match table fields provide the safest existing indication of concluded snapshot authority?
4. How should request counts be captured from the current JSON client without duplicating instrumentation?
5. How should optional `--report-file` arguments be represented within the existing CLI parser?
6. Which existing audit tables and run categories should represent the parent workflow and match-level outcomes?
7. Which current CLI exit-code conventions should be reused for fatal configuration or database errors?
8. What report schema version should be introduced for the first implementation?

These are implementation investigations, not reasons to weaken or broaden the workflow scope.

## 22. Acceptance summary

The first implementation is complete when a new user can prepare a database through guided first-run, select a current or historical AFL season, review the discovered plan, and populate all currently available supported season data without needing to understand the individual collectors.

An experienced user must be able to execute the same workflow directly and non-interactively.

Rerunning the workflow must efficiently collect only new or non-final work by default, preserve useful final data, continue past isolated match failures, and clearly report what happened, how long it took, how many source requests were made, and what remains incomplete.
