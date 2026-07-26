# Comprehensive Engineering & Architecture Review for v0.5.0

Comprehensive Engineering & Architecture Review for v0.5.0
Review date: 26 July 2026
Repository state: 4273e61 on branch work
Scope: Current implementation, documentation, tests, operational interfaces, and all six open GitHub Issues.
Repository changes: None. In accordance with the request, this review was not written into the repository, committed, or submitted as a pull request.

1. Executive summary
AFL-api has made a substantial architectural advance since the v0.4.0 review. The new afl_json subsystem establishes a credible long-term collection foundation:

Endpoint definitions, authentication requirements, identifier conventions, and pagination policies are centralised.

Public metadata and protected CFS traffic share a bounded, testable HTTP transport.

Provider IDs are correctly treated as opaque identifiers rather than inferred from AFL numeric IDs.

The public hierarchy—competition, season, teams, rounds, and matches—can be collected and persisted idempotently.

Player identity collection explicitly reconciles Champion Data IDs with AFL numeric IDs.

Match rosters and player statistics have source-specific collectors with raw preservation and validation.

Player-stat persistence protects concluded data from being overwritten by weaker live observations.

The JSON subsystem has materially better offline coverage than the remaining HTML scraper subsystem.

The architectural direction is therefore sound and appropriate for v0.5.0. It should become the default design pattern for future collectors.

The repository is not yet uniformly operating on that architecture, however. It currently contains two generations of collection code:

A modular JSON-oriented layer under afl_json/.

Older HTML/Playwright collectors that often combine acquisition, parsing, persistence, audit handling, and CLI behavior.

That coexistence is reasonable during migration, but it produces operational and model inconsistencies:

Scheduled and Admin-triggered collection still primarily invokes legacy HTML collectors.

The CLI exposes new JSON functionality, but not as a complete orchestrated pipeline.

Player identities and season associations are normalised in memory but are not yet persisted as a canonical seasonal model.

Match rosters have no canonical persistence layer.

Old and new player-stat tables coexist.

Several active HTML scrapers still bypass the configured database path.

The README’s earliest CLI examples are stale and do not match the implemented arguments.

Diagnostics, retries, validation, raw capture, and source metadata differ materially between JSON and HTML collectors.

Readiness verdict
Conditional release candidate. The project is approaching a logical v0.5.0 milestone and its new core architecture is mature enough to release. The strongest argument for v0.5.0 is that it can mark the introduction of the canonical AFL JSON collection foundation—not the completion of migration away from HTML.

The most important pre-release activities are verification and release clarification rather than another broad refactor:

Explicitly document which JSON collectors are production paths, diagnostic paths, or not yet scheduled.

Resolve or prominently document the hard-coded database-path risk in the remaining active HTML collectors.

Correct the user-facing CLI documentation.

Define the supported v0.5.0 operational workflow, especially whether daily jobs intentionally remain HTML-based.

Run one clean-database, current-season, end-to-end smoke exercise before tagging.

Confirm that the absence of player-season and roster persistence is an intentional v0.5.0 boundary.

The larger legacy scraper refactor, broad schema unification, CLI redesign, and Admin parity work can appropriately remain post-v0.5.0 architectural debt.

2. Overall architecture
2.1 Current application shape
The repository remains a flat Python application composed of several recognizable subsystems:

Public FastAPI service.

Separate authenticated Admin service.

Separate internal scheduler service.

SQLite database and additive migration runner.

JSON collectors under afl_json/.

HTML/Playwright collectors under scraper/.

CLI orchestration in one root-level module.

Scheduler registry and scrape-run audit infrastructure.

Docker Compose service/network boundaries.

This remains suitable for the project’s present scale. A full framework migration, ORM introduction, message broker, or microservice decomposition would add more complexity than value at this stage.

The strongest service-level boundary is the operational separation between the public API, operator-facing Admin interface, and internal scheduler. The documented deployment keeps scheduler mutation routes off host ports and gives the scheduler a separate egress path for collection traffic. 

2.2 Separation of concerns
The quality of separation varies by generation.

Strong boundaries
afl_json/contracts.py is a data-only endpoint catalogue.

afl_json/client.py isolates HTTP, retries, JSON decoding, and CFS token handling.

Public collection and normalisation do not directly write to the database.

afl_json/bootstrap.py separately adapts collected metadata into persistence.

Player-stat collection separates collection/normalisation from its explicit upsert function.

Scheduler state and scrape-run history are separate concerns.

The contract module explicitly says transports and collectors consume immutable definitions rather than maintaining separate URLs and authentication rules. It also states that provider identifiers must not be derived from AFL numeric identifiers. 

Weak boundaries
Several HTML collectors still mix:

Playwright acquisition.

DOM parsing.

normalization.

SQLite access.

logging.

scrape auditing.

command-line execution.

The CLI itself also imports numerous concrete scraper and persistence functions and selects exactly one operation through a long if/elif chain. 

2.3 Maintainability and extensibility
The JSON layer is maintainable because likely upstream changes have localized homes:

AFL change	Primary isolated update location
URL, method, authentication, or response collection path	afl_json/contracts.py
Retry or token policy	afl_json/client.py
Public hierarchy field shape	Public normalizers in afl_json/collectors.py
Player-stat source field name	Central stat mapping in afl_json/player_stats.py
Roster wrapper/position shape	afl_json/rosters.py
Database projection	afl_json/bootstrap.py or stat persistence adapter
This is not absolute isolation. A newly required canonical field can still require changes to its normalizer, result model, migration, persistence adapter, documentation, and tests. That is normal. The important improvement is that an endpoint or wrapper change should not require modifying unrelated collectors, scheduler modules, and API code.

2.4 Testability
The repository’s 217 passing tests represent a meaningful strength. The JSON client is injectable, retry sleeping is injectable, transport tests are mocked, and JSON collectors accept stored payload fixtures.

The remaining imbalance is that JSON collection has extensive contract-level coverage while several active HTML sources have little or no realistic fixture coverage. Existing HTML fixtures primarily protect fixture and match parsing rather than every active HTML collector.

2.5 Dependency boundaries
The dependency direction is mostly reasonable:

contracts
   ↓
HTTP client
   ↓
collectors / normalizers
   ↓
CLI, bootstrap and persistence adapters
   ↓
database
The largest exception is operational orchestration. The CLI, scheduler, and Admin interface frequently depend directly on concrete scraper modules rather than a shared collector application service. This makes swapping an HTML collector for its JSON equivalent an entry-point-by-entry-point exercise.

2.6 Architectural debt that can wait until after v0.5.0
The following should be treated as intentional post-release debt rather than reasons for a broad pre-release rewrite:

Convert the flat flag-based CLI into explicit subcommands.

Establish a collector application/service layer shared by CLI, scheduler, and Admin.

Split the large public collector module into endpoint-family or domain modules if it continues growing.

Replace plain dictionary canonical models with typed domain records where contracts have stabilised.

Reconcile or retire parallel legacy and CFS player-stat storage.

Model team membership and player membership as historical season associations.

Abstract common diagnostic and raw-capture behavior across every collector.

Refactor remaining HTML collectors behind fetch/parse/normalise/persist boundaries.

Package the flat root-level source tree more conventionally.

3. AFL JSON collection architecture
3.1 Endpoint contracts and discovery
The endpoint catalogue is one of the strongest additions. It captures:

Public versus CFS source.

HTTP method.

Path template.

Authentication requirement.

Entity type.

Collection paths.

Identifier type.

Required and optional parameters.

Pagination policy.

Verification state and unresolved fields.

The catalogue currently describes public competitions, seasons, rounds, teams, matches, match detail, player ID mapping, CFS season players, rosters, and player statistics. 

This is a good long-term arrangement. Endpoint discovery findings are not merely prose; they become executable contracts. The companion investigation catalogue remains valuable because it records uncertainty and provenance that should not be encoded as runtime assumptions.

One limitation is that the contract models request and broad response structure, not a complete versioned schema. That is appropriate while upstream APIs are undocumented, but contract regression fixtures remain essential.

3.2 Public versus authenticated endpoints
The separation is correct:

Public AFL metadata calls have no CFS token requirement.

Protected CFS calls lazily acquire a WMCTok.

The token is cached only in process memory.

Protected requests inject x-media-mis-token.

A 401 or CFSAPI001 result causes exactly one token invalidation and refresh.

CFSSDS001 is classified as unpublished/unavailable rather than authentication failure.

The client returns structured error types and deliberately avoids logging payload bodies or tokens. Its default network policy has explicit connect/read timeouts, bounded attempts, and exponential backoff. 

This is a solid security and resilience boundary.

Potential future considerations—not v0.5.0 blockers—include honoring Retry-After, adding randomized jitter for concurrent workers, reporting response duration/request identifiers, and defining whether a shared session may be used concurrently.

3.3 Pagination
The public collector supports:

No pagination.

Response-driven pagination.

A special “verify total, then page” strategy for season players.

Duplicate suppression.

Maximum-page guards.

Detection of non-progressing pagination.

Detection of advertised additional pages that add no new records.

Season player collection deserves particular credit: it treats the initial unpaged response as a completeness probe, validates represented counts and totals, and explicitly pages from page one when incomplete. Diagnostics capture changing totals and unreconciled final counts. 

The primary residual risk is upstream ambiguity: page-number bases and pagination metadata names are provider behavior, not a public contract. Issue #60 remains important because pagination should be defended by realistic captured states, not only synthetic cases.

3.4 Identifier handling
The design correctly distinguishes:

AFL numeric IDs.

Champion Data provider IDs such as CD_C, CD_S, CD_R, CD_M, CD_T, and CD_I.

Legacy internal or textual identifiers.

The central identifier rules make prefixes documentary and validate shapes, but do not attempt to derive one ID namespace from another. 

Player reconciliation is particularly sound:

The public ID-map endpoint supplies the crosswalk.

Duplicate rows are diagnosed.

Contradictory Champion Data-to-AFL and AFL-to-Champion Data mappings are diagnosed.

Unmapped players are retained rather than discarded.

Season identity and season association records are returned separately.

This is the right conceptual model for long-term identity integrity.

3.5 Canonical models
The public hierarchy normalizes stable fields while preserving each source record. Match statistics use a frozen typed record and central source-to-canonical stat mapping. Rosters use structured collection results but retain more dictionary-based internal records.

That mixed typing is acceptable during discovery. It does mean the term “canonical model” currently describes conventions rather than one consistent type system.

The strongest canonical properties are:

Stable IDs remain explicit.

Unknown source fields are not discarded.

Missing values remain missing rather than being fabricated.

Status provenance is separated from publication classification.

Match-status reconciliation is monotonic.

Provider-side ordering is treated as diagnostic rather than identity.

3.6 Raw payload preservation
There are two preservation levels:

Normalized records retain source fragments in source, raw_player, or similar fields.

An opt-in raw response writer stores deterministic endpoint/scope/page JSON captures.

The public metadata documentation accurately distinguishes raw source capture from normalized stdout output and states that headers and credentials are excluded. 

This is a good balance between storage cost and investigatory value. A future retention policy may be needed because deterministic files can still accumulate across scopes, seasons, or repeated operational directories.

3.7 Diagnostics and logging
JSON collectors provide substantially better diagnostics than the legacy scraper generation:

Diagnostic codes.

Human-readable messages.

Structured context.

Distinction between malformed rows and malformed responses.

Per-record rejection without necessarily discarding usable peers.

Explicit unpublished/empty/live-partial/concluded classifications.

Token-safe request logs.

The inconsistency is that CollectionDiagnostic, PlayerStatDiagnostic, and match-status diagnostics are separate types, while roster failures are more exception-driven and expose fewer structured warnings. A common diagnostic protocol would eventually help orchestration, but the current source-specific types do not undermine the foundation.

3.8 Long-term foundation verdict
Yes—the AFL JSON design is a solid long-term foundation.

Future URL or response-wrapper changes are likely to be isolated. Canonical-field changes will naturally span the appropriate domain normalizer and persistence projection, but should not require widespread changes.

The main risk is not the design of afl_json; it is that the rest of the application has not yet fully adopted it.

4. Remaining HTML scraper architecture
4.1 Current positioning
Documentation explicitly describes the new CFS player-stat collector as preferred and the rendered HTML player-stat scraper as a temporary fallback. 

The endpoint source-priority map similarly places JSON/CFS ahead of HTML for metadata, players, rosters, and statistics. 

Architecturally, then, the HTML collectors are correctly described as:

Fallback collectors.

Compatibility collectors.

Legacy operational support.

Operationally, however, they remain primary in several places:

Daily scheduled fixture and match work invokes HTML scraper modules.

Daily injury collection is still Playwright-based.

Scheduled stats jobs invoke the legacy player-stat scraper.

Admin manual triggers invoke legacy injury, fixture, lineup, and player-stat functions.

Player and club refresh paths remain HTML/browser based.

Therefore, v0.5.0 should avoid implying that JSON is already the universal production source.

4.2 Remaining coupling and brittleness
The active HTML collectors retain the risks identified in the v0.4.0 review:

Browser acquisition and parsing live in the same modules.

Selectors and parser behavior remain coupled to rendered AFL pages.

Persistence is sometimes embedded directly in scraper code.

Some modules use configured connection helpers while others use literal database paths.

Logging and exception handling differ per scraper.

Browser start-up cost and dynamic-page timing add operational variability.

Centralised selectors and the shared HTTP utility are improvements, but most active browser collectors have not yet been reorganised around those abstractions.

4.3 Maintenance burden
The burden should gradually decline as JSON coverage becomes operational, but it will remain significant for sources without known JSON replacements—particularly injuries and any lineup behavior not fully represented by CFS rosters.

The historical scrape_afl_lineups-early2025.py file also indicates seasonal compatibility code is being retained alongside the active module. That may be useful as evidence, but historical and production modules should remain clearly distinguishable.

4.4 Appropriate future abstraction
Without prescribing implementation work, the most useful future boundary remains:

source adapter → raw document → pure parser → normalizer/validator
               → persistence adapter → audit/orchestration
The JSON subsystem already demonstrates much of this pattern. Future HTML work should adopt it selectively rather than forcing every source into a premature universal framework.

5. Collector framework consistency
Concern	JSON collectors	HTML collectors	Assessment
Request handling	Shared requests.Session client	Mixture of Playwright and shared HTTP utility	Inconsistent
Authentication	Central CFS token policy	Generally source-specific/browser state	JSON is strong
Retry behavior	Bounded transient retries	Varies by browser/helper/module	Inconsistent
Timeouts	Explicit connect/read policy	Varies	Inconsistent
Pagination	Contract-driven and guarded	Mostly source-specific iteration	Inconsistent
Raw capture	Deterministic, opt-in JSON	Ad hoc or absent	Inconsistent
Diagnostics	Structured coded records	Logs, prints, exceptions, broad row handling	Inconsistent
Logging	Standard logging in JSON layer	Multiple logger utilities and emoji-rich messages	Inconsistent
Error handling	Typed transport/auth/unavailable/invalid errors	Module-specific	Inconsistent
Validation	Explicit identities, numeric validation, count reconciliation	Mostly parser-specific	Inconsistent
Audit integration	CLI persistence paths use scrape_runs; read-only paths do not	Uneven but improving	Partially consistent
Source metadata	Explicit endpoint/source/raw fields	Often implicit URL/module context	Inconsistent
Persistence	Explicit adapters for metadata/stats	Frequently embedded	Inconsistent
The inconsistency is understandable during architectural transition. It becomes problematic only if new collectors continue to follow legacy patterns or if operators cannot tell which behavior to expect.

6. Database and canonical model
6.1 Competition and season hierarchy
Migration 0007 adds dedicated competition, season, and team tables, and extends legacy rounds and matches with provider IDs, seasonal references, structured JSON fragments, and source preservation.

The bootstrap persists an entire collected hierarchy within one transaction and reports inserted, updated, and unchanged records.

This is a strong foundation for a canonical AFL metadata model.

6.2 Teams
The main concern is historical cardinality. afl_teams uses the AFL team ID as its primary key while also storing a single season_id. That represents “team as observed in the most recently written season,” not a durable many-season relationship.

If multiple seasons are bootstrapped, an existing team row can be updated to point to the latest season. Consequently, the current table cannot answer historical “which teams belonged to this season?” questions reliably without relying on preserved source data.

This is post-v0.5.0 schema debt unless multi-season historical querying is already a release requirement.

6.3 Rounds and matches
Rounds and matches now contain the correct core links:

competition → season → round → match
                      ↘ team references
Provider IDs are uniquely indexed when present, and Opening Round’s valid round number zero is explicitly protected in collection logic.

Residual model issues include:

The baseline matches.round_id is NOT NULL, while later metadata fields were added incrementally.

Relationships added through ALTER TABLE are not backed by newly declared foreign keys.

Team relationships coexist as both textual names and numeric IDs.

Venue remains embedded JSON/text rather than a canonical venue entity.

Competition on rounds and season on matches are partly denormalized for convenience.

None is a release blocker, but they should be recognized as an evolutionary schema rather than a completed relational domain model.

6.4 Players
The legacy players table models one current club, guernsey, and position directly on the identity row. 

The new collector improves the in-memory design by splitting:

Stable player identity.

Player-season association.

However, there is no corresponding canonical player-season persistence migration. As a result, the best conceptual player model currently exists only in collector output.

This is one of the clearest remaining canonical-model gaps.

6.5 Match rosters
The roster collector has a thoughtful normalized representation and stable comparison behavior, but there is no canonical roster/selection persistence table. It is currently a diagnostic and compatibility-capable collector rather than a complete database integration.

That is acceptable if explicitly stated as the v0.5.0 scope boundary.

6.6 Match player statistics
Migration 0006 introduces a dedicated current-observation CFS table keyed by match provider ID and Champion Data player ID. It preserves:

Canonical mapped statistics.

Extra unmapped statistics.

Entire raw player records.

Collection timestamp.

Endpoint status.

Resolved match status.

Snapshot authority.

The overwrite policy is a notable strength: concluded observations cannot be downgraded by later partial/live data. The player-stat documentation clearly records this lifecycle and authority behavior. 

Remaining debt:

Legacy player_stats and new cfs_player_stats coexist.

afl_match_id is declared as text in the CFS table despite the canonical match key being integer-oriented.

The CFS table does not declare foreign keys to matches or player identities.

The natural provider identity is strong, but canonical AFL player-ID enrichment is not stored directly.

6.7 Raw versus normalized data
The separation is generally appropriate:

Canonical columns support stable application queries.

Source/raw JSON fields prevent lossy normalization.

Optional raw response files preserve entire payloads for investigation.

Audit tables track execution rather than duplicating provider data.

Raw data is physically co-located with normalized database rows in some tables, but logically separated into explicit JSON columns. For SQLite and the project’s current scale, that is a reasonable compromise.

7. CLI review
7.1 Interface shape
The CLI is currently a single flat argparse parser with grouped mutually-by-convention flags rather than subcommands. Only the first matching operation runs because dispatch is one long if/elif sequence. 

This is usable but increasingly difficult to extend.

Implemented command families
Commands	Purpose	Assessment
--scrape-club, --scrape-clubs	Club/player-list HTML collection	Legacy but coherent
--enrich-club, --enrich-clubs	Local player enrichment	Clear, though “enrich” is domain-specific
--scrape-enrich-all	Club scrape, enrichment, DB import	Useful batch shortcut
--skip-existing	Club-file behavior	Scope is documented in help
--scrape-injuries	Injury collection and persistence	Appropriate daily/manual operation
--scrape-lineups ROUND	Legacy HTML lineup collection	Parameter is round number
--scrape-round ROUND_ID	Match collection for a stored round ID	Name is ambiguous beside round number
--scrape-all-rounds	Refresh all match rounds	Useful seasonal/repair operation
--scrape-match MATCH_ID	Legacy HTML player stats	“scrape match” understates that it means player statistics
--collect-afl-metadata	Read-only public hierarchy	Good diagnostic operation
--bootstrap-afl-season YEAR	Collect and persist hierarchy	Good first-run/seasonal operation
--afl-season	Metadata season selector	Flexible but valid only for related operations
--afl-competition-code	Metadata selector	Appropriate
--afl-competition-provider-id	Metadata selector	Appropriate but specialized
--afl-raw-directory	Raw JSON capture	Good diagnostic capability
--collect-match-rosters	Read-only CFS roster collection	Good troubleshooting operation
--collect-match-player-stats	CFS stats collection and persistence	Good but semantically unlike the read-only roster command
--source-status	Manual status fallback	Useful but should remain diagnostic
--afl-match-id	Canonical match reconciliation	Necessary but advanced
--import-clubs, --export-clubs	Club backup/restore	Coherent utility operations
--print-json	Full output for supported paths	Useful, but not universal
The runtime help accurately lists the current options, but the README still advertises --all, --scrape richmond, and --enrich richmond, none of which exist in the parser. 

That documentation drift is a notable v0.5.0 usability risk.

7.2 Naming and parameter consistency
Key inconsistencies:

“Round” can mean a round number or database round ID.

“Match ID” can mean AFL numeric ID, database match ID, or Champion Data provider ID.

--scrape-match means legacy player-stat scraping.

--collect-match-player-stats collects and persists, whereas --collect-match-rosters only collects.

--bootstrap-afl-season YEAR overlaps with the general --afl-season selector but has different precedence.

Advanced option flags remain globally visible even when irrelevant to the chosen operation.

Unsupported combinations are silently ignored rather than rejected.

The help text does improve identifier discoverability by explicitly naming Champion Data IDs for the new CFS operations. 

7.3 Dry-run and diagnostics
There are useful dry-run-like capabilities:

--collect-afl-metadata avoids database writes.

--collect-match-rosters avoids database writes.

--print-json provides normalized output.

--afl-raw-directory captures source JSON.

But there is no universal --dry-run, no full-season collector pipeline without database writes, no resumable batch output, and no unified collection summary. Thus Issue #61 is only partially satisfied.

7.4 Operational coverage
Operational need	CLI support
Daily injury refresh	Yes
Daily fixture/match refresh	Yes
Match-day lineup refresh	Yes
Individual match stats	Yes, legacy and JSON paths
Seasonal metadata bootstrap	Yes
Player/club refresh	Yes, legacy
Roster investigation	Yes
Raw JSON investigation	Yes
Full non-persistent JSON pipeline	No
Explicit player-identity collection	No CLI entry
Roster persistence	No
Scheduler state inspection/control	No direct CLI workflow
Scrape-run audit inspection	No direct CLI workflow
Unified health/freshness summary	No
The CLI covers the major collection tasks, but troubleshooting is split between CLI output, log files, database inspection, and Admin pages.

8. Admin interface and Scheduler page
8.1 Existing shortcuts
The Scheduler page provides manual forms for:

Injury refresh.

Fixtures for one round.

Lineups for one round.

Lineups for one match.

Player statistics for one match.

It also allows scheduler refresh and displays queued scheduler jobs. The page correctly warns that triggers enqueue work rather than waiting for completion, and flags player-stat refresh as potentially expensive. 

Admin POST handlers validate identifiers, apply CSRF protection, and forward only predefined requests to the internal scheduler instead of executing arbitrary shell commands. 

This is a sensible and secure operator workflow.

8.2 CLI comparison
Duplicated functionality
Injury refresh.

Round fixture/match refresh.

Round lineup refresh.

Match player-stat refresh.

CLI-only functionality
Club scraping and enrichment.

Club import/export.

All-round match refresh.

Public metadata collection.

Season bootstrap.

Raw JSON capture.

CFS roster diagnostics.

CFS player-stat collection.

AFL/provider ID reconciliation options.

Full JSON output.

Admin-only convenience
Scheduler listing.

Queue visibility.

Duplicate-job feedback.

One-click forms with stored-identifier validation.

Authenticated web access without shell availability.

8.3 Architectural mismatch
The Admin manual player-stat trigger invokes the legacy HTML scraper, not MatchPlayerStatsCollector. The scheduler’s regular player-stat jobs likewise invoke the legacy path. 

This is the most significant Admin/CLI divergence: an operator using Admin and an operator using the new CFS CLI command are not necessarily exercising the same source, validation, or persistence semantics.

There is also terminology risk: the lineup round trigger submits round_id, but the target function passes it as round_number. 

8.4 Admin verdict
The Admin interface now offers sensible entry points for administrators who prefer not to use the CLI. It is particularly good for narrow, queued operational recovery.

It is not yet an interface to the complete modular JSON collector suite, and it should not be described as CLI-equivalent. Its present role is best described as safe manual control over scheduled legacy operational jobs.

9. Open GitHub Issues review
The live repository currently has six open Issues.

Issue	Current assessment	Recommendation
#61 – Add collector orchestration and dry-run CLI	Relevant; partially satisfied. Metadata can be collected without writes; roster collection is read-only; raw and normalized data can be emitted separately; selected match stats can be collected. Missing pieces include player collection exposure, endpoint-family selection, multi-stage orchestration, deterministic normalized output directories, resumability, aggregate failure summaries, and batch exit semantics. Player-stat CLI currently writes to DB.	Retain after v0.5.0. Reframe around one application-level orchestration layer rather than adding more top-level flags.
#60 – Add JSON payload fixtures and contract regression tests	Relevant; substantially partially satisfied. Fixtures already exist for competitions, seasons, teams, rounds, matches, player ID mapping, season players, rosters, and multiple player-stat states. Client tests cover token failures with mocks. Coverage is strong, but fixture provenance metadata, comprehensive pagination captures, every empty/optional-field state, and a systematic endpoint-family matrix appear incomplete.	Retain, but reassess acceptance criteria against the current 217-test suite so completed portions are recognized rather than duplicated. High value before or immediately after v0.5.0.
#51 – Remove hard-coded database paths from active scrapers	Still relevant and directly evidenced. Active fixture, match, and injury modules still contain literal data/afl_players.db connections, while newer paths use get_db_connection().	This remains the clearest small reliability gap. It is especially important for custom DB_PATH, Docker, and isolated tests. Consider before release if those deployment modes are claimed; otherwise document prominently and address immediately afterward.
#50 – Separate fetching, parsing and persistence for one pilot scraper	Architecturally aligned, but its premise has changed. The JSON collectors already prove much of the desired architecture. Refactoring the legacy HTML player-stat scraper would now duplicate a domain already served by CFS. A fixture/match or injury HTML collector would be a more representative pilot if it remains operationally necessary.	Retain only after reconsidering the pilot target and dependency on Issues #47/#48. Post-v0.5.0 work. It is not obsolete, but the JSON implementation partially satisfies its architectural validation goal.
#48 – Add a golden fixture corpus for all active AFL scrapers	Still relevant for HTML; partly superseded for JSON. Fixture/match HTML parsing has samples, and the new JSON subsystem has a growing fixture corpus. Clubs, injuries, players, and lineups still lack the comprehensive state coverage described by the Issue. Issue #60 explicitly replaces its JSON portion.	Narrow conceptually to remaining active HTML/Playwright collectors. Avoid duplicating #60. Reassess whether every legacy collector merits a large corpus or whether some will be retired.
#47 – Document AFL scraper sources and page contracts	Relevant; partially satisfied. The JSON endpoint catalogue is detailed and current, but it does not replace an inventory of all active HTML pages, rendering requirements, selectors, parser entry points, and operational callers.	Retain, but scope the remaining work to HTML and operational source selection. This is useful before deciding which legacy collectors to refactor or retire.
Obsolescence assessment
No open Issue is wholly obsolete.

The closest candidates for reconsideration are:

#50, because afl_json has already demonstrated the desired architectural pattern and the originally suggested player-stat pilot would now target a fallback implementation.

#48, because its JSON scope has been explicitly superseded by #60 and much JSON fixture work already exists.

These should be narrowed or reevaluated after v0.5.0 rather than implemented literally from their original broad wording.

10. Documentation assessment
Strengths
Documentation for the new subsystem is unusually strong for an undocumented upstream integration:

Endpoint catalogue.

Public metadata/bootstrap guide.

Match roster behavior and unresolved observations.

Match player-stat mapping, publication semantics, and live verification.

Scheduler registry behavior.

Scrape-run auditing.

Admin manual-trigger workflow.

Database migration process.

The documentation index gives these guides a discoverable home.

Gaps and drift
The primary README CLI examples use nonexistent options.

The root TODO.md describes the older scraper-centric architecture and risks being mistaken for the current roadmap.

There is no single document defining which collector is authoritative for each operational data family.

The JSON endpoint catalogue and runtime contracts are detailed, but the remaining HTML source inventory is absent.

There is no concise v0.5.0 operator runbook spanning migration, season bootstrap, scheduler start, verification, backups, and rollback.

It is not always obvious which collector writes canonical data, which is diagnostic-only, and which remains scheduled.

Documentation quality is therefore high locally but inconsistent at the top level.

11. Testing and quality assessment
Strengths
All 217 tests pass.

JSON transport tests are offline and mock network behavior.

Contract and selector tests protect central assumptions.

Metadata, player identity, roster, match status, and player-stat collectors have focused coverage.

Database migration behavior is well tested.

Scheduler startup, registry, manual triggers, Admin CSRF, API-key hashing, Compose security, and health endpoints have dedicated tests.

The suite is fast enough to remain a practical CI gate.

CI runs a test matrix and includes dependency-aware setup.

Remaining gaps
No full clean-database, full-season offline integration test crosses collection, bootstrap, scheduler registration, and API results.

HTML coverage remains uneven.

No linting, formatting, static typing, or dependency vulnerability gate is evident.

No Docker image/runtime smoke check is part of the reviewed local validation.

Live verification is documented in commits and domain docs but is necessarily non-deterministic.

The tests protect behavior but do not yet provide a formal compatibility matrix for every documented endpoint state in Issue #60.

The test suite is already a release strength; the principal concern is source-state coverage, not raw test count.

12. v0.5.0 readiness and remaining risks
12.1 Maturity assessment
Area	Assessment
Overall architecture	Good and improving
JSON transport/contracts	Strong
Public hierarchy collection	Strong
Player identity collection	Strong in memory; incomplete persistence
Roster collection	Strong collector; no persistence/orchestration
Player-stat collection	Strong new path; legacy operational path remains
HTML collectors	Functional but high-maintenance
Database migrations	Mature for project scale
Canonical schema	Good hierarchy, incomplete historical/player/roster model
CLI	Capable but increasingly inconsistent
Admin	Practical and secure, but legacy-source oriented
Scheduler/audit	Good operational foundation
Documentation	Strong domain docs, stale top-level commands
Tests	Strong, especially for new JSON subsystem
12.2 Major risks before release
Risk 1: Operational source ambiguity
The repository declares JSON/CFS as preferred but schedules and Admin-triggers legacy HTML collectors. This can produce different behavior, tables, diagnostics, and failure modes depending on entry point.

Release treatment: Document the intended v0.5.0 source matrix explicitly.

Risk 2: Configured database-path bypass
Several active HTML modules still connect directly to data/afl_players.db. A successful CLI or scheduler run can write to a different database from the configured API/Admin service.

Release treatment: Either resolve Issue #51 or clearly limit the supported deployment configuration.

Risk 3: Incomplete canonical persistence
Player-season associations and rosters are not persisted, while team-season history is modeled as a single mutable season reference.

Release treatment: State that v0.5.0’s canonical persistence scope is metadata hierarchy plus current CFS match statistics, not the complete discovered AFL domain.

Risk 4: CLI documentation mismatch
New users following the README’s first CLI examples will encounter invalid arguments.

Release treatment: Correct release-facing usage documentation before tagging.

Risk 5: Dual player-stat models
Legacy player_stats and new cfs_player_stats coexist, and Admin/scheduler versus new CLI paths populate different systems.

Release treatment: Document which API/operation reads each table and which is authoritative.

Risk 6: Upstream API instability
The AFL JSON services remain undocumented. Strong local fixtures reduce risk, but not all endpoint states and provenance requirements from Issue #60 are complete.

Release treatment: Preserve raw capture guidance and conduct a clean live smoke check close to release.

13. Recommended milestone boundary
A coherent v0.5.0 definition would be:

v0.5.0 introduces the modular AFL JSON transport, endpoint contracts, public season hierarchy bootstrap, player identity collection, roster collection, canonical CFS player-stat collection, and the supporting diagnostic/test foundation. Legacy HTML collectors remain supported for compatibility and scheduled operations while migration continues.

That boundary is honest, technically meaningful, and avoids delaying the milestone until every legacy component has been redesigned.

The following need not be folded into the release:

Complete HTML scraper refactoring.

Universal collector orchestration.

Full Admin/CLI feature parity.

Historical team-season normalization.

Player-season persistence.

Roster persistence.

Retirement of all legacy tables.

Complete subcommand CLI redesign.

Universal typed models.

14. Overall assessment
Architectural health: Good, with clearly bounded transition debt
The project is healthier than the coexistence of old and new collectors might initially suggest. The new design is not merely another scraper implementation; it establishes the correct architectural direction:

Contracts separate upstream details.

Transport centralises policy.

Collectors preserve raw evidence.

Normalizers avoid invented semantics.

IDs retain namespace integrity.

Persistence is explicit and idempotent.

Diagnostics are structured.

Tests are offline and source-state aware.

Operational audit and scheduler state are durable.

The major architectural concern is now adoption consistency, not the quality of the new foundation.

v0.5.0 readiness: Approaching ready, with release-documentation and operational-source caveats
A v0.5.0 release is justified once maintainers are comfortable presenting the JSON subsystem as the new foundation while acknowledging that:

HTML remains operationally important.

The canonical database model is not yet complete for every collected domain.

Admin and scheduler do not yet mirror the new CLI collection paths.

Some deployment-path reliability debt remains open.

With those boundaries made explicit, v0.5.0 represents a logical and valuable milestone rather than an unfinished attempt at total migration.



---

## Historical Note

This document was produced as an independent engineering review prior to the planned **v0.5.0** release. It is intended to provide an architectural snapshot of the project at this point in its evolution, recording the state of the repository, the migration toward the AFL JSON collection architecture, and the remaining technical debt before the next major milestone.

The review is deliberately advisory. It does **not** propose code changes, create GitHub Issues, or alter the project's roadmap. Instead, it is intended to inform release planning and future architectural decisions.

