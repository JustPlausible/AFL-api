# Post-v0.5.0 engineering status review

## Review baseline and method

| Item | Reviewed value |
| --- | --- |
| Repository/default branch | `JustPlausible/AFL-api`, `main` |
| Exact default-branch revision | `a4ddcd1306cebf90b7a7d52766187740f47b4e8a` (`Formalise injury collector pipeline boundaries (#103)`) |
| Review date | 1 August 2026 (UTC) |
| Repository version | `0.5.0`, confirmed from `version.py` and `python cli.py --version` |
| Review branch | `docs/post-v0.5.0-engineering-status` |
| Automated validation | `python -m pytest`: **397 passed in 17.59s** on CPython 3.14.4 |
| Environment limits | Docker was not installed, so the image/Compose stack was not rebuilt. No live AFL/CFS request was made. Upstream behavior was assessed from checked-in contracts, fixtures and tests plus the completed live-validation record. |

The GitHub API was used before editing to resolve `main` and its exact SHA.
Recent merged PRs and closed issues were reviewed through PR #103/Issue #50;
there were no open non-PR issues at that point. Implementation, migrations,
entry points and tests were then inspected directly rather than treating issue
descriptions as proof. This review therefore includes the completed injury
collector pipeline refactor.

This is a current-state baseline, **not** a release-readiness gate or a v0.6.0
plan. Historical conclusions remain in the [pre-release review](architectural_review_v0_5_0.md),
[final readiness review](release_readiness_v0_5_0.md), and
[live validation record](release_validation_v0_5_0_issue_78.md). Current
operator behavior is defined by the [source policy](../operational_source_policy.md),
[CLI guide](../cli.md), [migration guide](../database_migrations.md), and
[v0.5.0 runbook](../operations/release_runbook_v0_5_0.md).

## 1. Executive summary

**Overall assessment: a credible, well-tested single-instance collection
service with a modular canonical foundation, but not yet a complete canonical
data product.** Its strongest areas are endpoint/transport contracts, offline
regression protection, migrations, explicit source authority, auditability and
bounded SQLite operations. Public/CFS JSON collectors and the new injury stages
provide good seams for future work.

The main limitations are now product-facing: the API mostly exposes older
tables and has no authoritative CFS-stat reader; CFS rosters are collectable but
deliberately not persisted; operational lineups remain HTML-backed; player
bootstrap is CLI-only; and canonical and legacy identities coexist without a
general read/reconciliation layer. Monitoring is inspectable but not alerting,
and multi-writer horizontal deployment is not claimed.

The repository is suitable to begin a new planning phase. The recorded backlog
has been completed, narrowed or retained explicitly as a limitation. Confirm
the maintainer's product goal and select one coherent theme—most plausibly
canonical API value or canonical roster/lineup persistence—before creating a
milestone. Recommendations below are choices, not commitments.

## 2. Current supported capability

| Area | Classification | Current boundary |
| --- | --- | --- |
| Public AFL metadata | **Production-supported** | `PublicAflCollector` collects competition, season, teams, rounds and matches; bootstrap/operational refresh persist them. Separate CLI collection is read-only. |
| Authenticated CFS | **Production-supported** | Shared client/token path supports season players, rosters and match stats; credentials/upstream publication remain environmental. |
| Season bootstrap | **Production-supported** | CLI-only public hierarchy plus transactional canonical player/provider/season-membership persistence. |
| Canonical clubs, players, provider IDs | **Production-supported** | Versioned club seed; `canonical_players`, `player_provider_ids`, `afl_team_seasons`, `competition_season_players`. Team membership may be null when unresolved. |
| Fixtures, rounds, teams, matches | **Production-supported** | JSON refresh persists hierarchy, provider IDs, time/venue/status/scores and corrections. |
| Match status | **Production-supported** | Public detail reconciliation applies monotonic lifecycle updates; full metadata refresh owns broad fields. |
| Rosters | **Supported but diagnostic/read-only** | CFS normalisation and change states have CLI/database-free paths, but no writer or target table. |
| Lineups | **Intentionally legacy/manual** | Scheduler/Admin/CLI use rendered HTML and persist `lineups`; database-free CFS lineup output is file-only. |
| Player statistics | **Production-supported**, with legacy compatibility | Preferred CFS entry points persist authoritative `cfs_player_stats`; explicit HTML and compatibility API use `player_stats`. No fallback/dual write. |
| Injuries | **Production-supported** | HTML acquisition, pure parsing, canonical resolution and safe current/history writes are shared by CLI/Scheduler/Admin. Unsafe identities stay diagnostic. |
| Database persistence | **Production-supported, constrained** | Checksummed migrations through `0011`, configured SQLite path, transactions/upserts; intended for one shared SQLite instance. |
| CLI | **Production-supported**, with legacy commands | Documented flag surface covers preferred JSON, bootstrap, database-free, explicit HTML and club-file operations. Only the first operation flag dispatches. |
| Scheduler | **Production-supported** | Registry-backed status/recovery around in-memory APScheduler; operational jobs use shared source policy. Unsafe missed/failed jobs are not blindly replayed. |
| Admin triggers | **Production-supported, constrained** | Authenticated/CSRF forms queue bounded injury, fixture, lineup and stat jobs via private Scheduler endpoints. No bootstrap trigger. |
| API | **Partial or constrained** | Health/readiness and authenticated reads for legacy players, operational rounds/matches, canonical-resolved injuries, legacy lineups/stats. Canonical CFS stats/players are absent. |
| Scrape auditing | **Production-supported, partial by design** | Persistent operational paths record lifecycle, trigger/correlation, counts and redacted errors. Database-free/read-only runs do not write the DB; old audit tables coexist. |
| Diagnostic/database-free collection | **Supported but diagnostic/read-only** | Deterministic raw/normalised/request/summary files without opening the DB; resume/overwrite/failure semantics. Injury/commentary/interchange JSON families are explicit unsupported skips. |

“Production-supported” above asserts persistence only where the supported path
actually writes. Diagnostic/read-only and legacy/manual classifications are
deliberate capabilities, not implicit promises of canonical storage.

## 3. Collection architecture

`afl_json.contracts` centralizes public/authenticated endpoint, method,
authentication, identifier and pagination expectations. `AflJsonClient` owns
token acquisition, bounded retry/rate behavior, response classification and
body-free errors. Public/CFS collectors compose it rather than embedding HTTP.
Raw response writers preserve per-page JSON separately from normalised models;
the database-free orchestrator adds safe request/source envelopes and aggregate
results. Contract fixtures protect observed shapes and important empty,
unavailable and partial states.

Source choice is correctly outside collectors. `SOURCE_POLICY` is the single
Scheduler/Admin selection map and prohibits automatic fallback and dual writes.
CLI sources remain explicit rather than pretending HTML and JSON are
interchangeable. This keeps an unpublished CFS response from silently changing
storage authority.

HTML is justified for injuries because no equivalent maintained structured
source is proven, and temporarily for persisted lineups. The injury production
path is now clearly staged:

1. `InjuryAcquirer` alone owns Playwright and returns raw HTML/source metadata;
2. `parse_injuries_html` is I/O-free and retains upstream identity markers;
3. `InjuryResolver` returns explicit resolved, ambiguous and unresolved rows;
4. `InjuryPersistenceAdapter` owns transaction/current-history safety; and
5. `collect_injuries` owns composition and exactly one audit lifecycle.

Compatibility functions no longer define a competing production composition.
This is sufficiently modular and is a useful pattern, not a reason for a
universal collector base class. Remaining coupling is concentrated in lineups,
where policy imports the HTML workflow/writer and database IDs bridge entry
points. `collect_operational` also opens a connection and dispatches domain
writers; that is acceptable at its small application boundary but should not
absorb parsing. New sources fit the contract/client/collector/raw-capture model;
shared diagnostic envelopes are useful, while forced HTML/JSON inheritance is
not.

## 4. Data and identity architecture

| Concept | Current authority and identity rule |
| --- | --- |
| Competition/season | `afl_competitions`, `afl_seasons`; numeric AFL keys with provider IDs retained. |
| Editorial clubs | `clubs`, seeded from `bootstrap/clubs.json`, owns stable codes and aliases. |
| Provider team participation | `afl_teams`, `afl_team_seasons`; provider data does not replace editorial club identity. |
| Rounds/matches | Operational `rounds`/`matches`, extended with unique provider IDs and canonical hierarchy fields. Numeric and opaque provider IDs remain distinct. |
| Canonical players | `canonical_players`; unique `(provider, provider_player_id)` mappings in `player_provider_ids`. |
| Season membership | `competition_season_players`, linking player/season with optional team/source/squad metadata. |
| Injury identity | Club-scoped name resolution to canonical AFL provider identity; only resolved AFL IDs persist. |
| Player-stat authority | `cfs_player_stats`, keyed by match provider/player provider IDs with optional canonical crosswalk and snapshot authority. |

Metadata/player/stat upserts and unique indexes provide idempotency. Match
status is monotonic. A live CFS stat snapshot cannot replace a concluded one;
raw/extra fields retain evidence. Injury current/history writes are source-date
keyed, and partial resolution does not expire unrelated safe rows.

Legacy coexistence is explicit. `players` is the legacy profile/API table, not
`canonical_players`; `lineups` is the HTML operational table; `player_stats` is
the HTML compatibility table while `cfs_player_stats` is authoritative;
`scrape_log`/`scrape_summary` remain beside `scrape_runs`. Risks are consumer
confusion between ID dialects, null team membership, unsafe unions of stat
models, non-canonical lineup history, and name/provider drift. The code prefers
explicit gaps over guessed joins. Retirement/backfill needs designed identity,
provenance and compatibility rules, not simple table consolidation.

## 5. Persistence coverage matrix

“Audit” means `scrape_runs` on a supported persistent path, not a file summary
or Scheduler registry row.

| Domain | Preferred source; collector/model | Writer → target | CLI | Scheduler | Admin | API | Audit | Gap |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Competition | Public JSON; `PublicAflCollector` | `persist_afl_metadata` → `afl_competitions` | Read-only + bootstrap | Daily refresh | Via fixture refresh | No | Operational refresh | No endpoint |
| Seasons | Public JSON; same | Same → `afl_seasons` | Read-only + bootstrap | Daily | Indirect | No | Operational refresh | No endpoint |
| Clubs/teams | Public JSON + seed; team/club records | metadata → `afl_teams`,`afl_team_seasons`; seed → `clubs` | Bootstrap/import; legacy enrichment | Provider refresh | Indirect | No | Metadata only | Editorial/provider models distinct |
| Rounds | Public JSON; round dicts | metadata → `rounds` | Collect/bootstrap; legacy scrape | Daily | Fixture trigger | Yes | Operational refresh | Direct DB-shaped response |
| Matches | Public JSON; match dicts | metadata → `matches` | Collect/bootstrap; legacy round scrape | Daily | Fixture trigger | Yes | Operational refresh | No reconciliation report |
| Match status | Public detail; `MatchStatusResolution` | reconciler → `matches.status` | Via CFS stat; legacy tools | Match-day | Via stat/match work | In match | Yes | Narrow lifecycle writer |
| Players | Public ID map + CFS season players; `PlayerCollectionResult` | `persist_player_seasons` → canonical players/maps | Bootstrap; legacy import separately | No | No | Legacy `players` only | No dedicated refresh | Canonical players absent from API |
| Season membership | CFS player-season records | same → `competition_season_players` | Bootstrap | No | No | No | No dedicated refresh | Nullable team; CLI-only |
| Rosters | CFS; `RosterCollectionResult` | **None** | Read-only | Policy capability, no standard persisted job | No direct trigger | No | Auditable zero-write policy call | Collectable, not persisted |
| Lineups | CFS preferred conceptually; HTML operational | `save_lineups_to_db` → `lineups` | HTML persistent; CFS file/read-only | HTML | HTML round/match | Yes | Yes | Legacy source/schema |
| Player stats | CFS; `PlayerStatsCollectionResult` | `upsert_player_stats` → `cfs_player_stats` | CFS persistent; explicit HTML → `player_stats` | CFS | CFS | Legacy `player_stats` only | Yes | Authority has no API/scoring reader |
| Injuries | Rendered HTML; parsed/resolved models | adapter → `injuries` | Yes | Daily | Yes | Current/history | Yes, including partial | Editorial fragility; AFL-ID API |

## 6. CLI, Scheduler and Admin consistency

Scheduler and Admin are consistent where they overlap. Admin validates one
target and calls a private Scheduler mutation; queued functions use the same
wrappers and `collect_operational` as scheduled work. Sources/tables therefore
match: public JSON metadata, HTML lineups, CFS stats and HTML injuries. Registry
IDs correlate with `scrape_runs`; Admin records `admin_manual`, scheduled work
records `scheduler`, and diagnostics include selection, fallback and counts.
Tests cover routing, duplicates, CSRF, trigger source and registry transitions.

CLI is broader by design: it adds read-only rosters, bootstrap, database-free
orchestration and explicit legacy tools. Friction remains in global flag help,
first-operation dispatch, three round/match identifier dialects, and pre-
canonical club-file/enrichment commands. Direct scraper modules should remain
implementation/diagnostic surfaces. The [CLI guide](../cli.md) accurately
documents these limitations.

## 7. API assessment

FastAPI provides liveness/readiness, root/header diagnostics and authenticated
players, injuries, lineups, rounds, matches and player stats. Rounds/matches use
tables maintained by canonical metadata refresh; injuries contain safely
resolved AFL IDs. Nevertheless the public contract is legacy-shaped:

* player routes read `players`, not canonical players/maps/membership;
* lineup routes read HTML-backed `lineups`;
* `/api/player-stats` explicitly reads `player_stats`, not authoritative CFS;
* clubs/teams, competitions, seasons, season squads, canonical identities, CFS
  stats and scrape status lack public reads; and
* SQLite rows are serialized directly without stable declared response models,
  while numeric and opaque provider IDs lack one consistent resource contract.

Highest practical value for downstream consumers would be a deliberately
versioned canonical match-stat endpoint with explicit match/player IDs, then
canonical season player membership and fixture/status resources. Roster API work
should wait for persistence authority. Existing routes must not silently switch
tables.

## 8. Testing and source-contract protection

The 397-test offline suite covers golden HTML/selectors and isolated parsers;
public/CFS JSON fixtures, transport, pagination and lifecycle states;
metadata/player/injury/stat persistence, conflicts, partials and rollback; CLI
help/documentation/identifier/bootstrap/database-free contracts; source policy,
Scheduler registry/startup/manual jobs and Admin security; migrations, database
paths, Compose security, health, audit/redaction and failure paths.

This strongly protects observed architecture, but fixtures cannot prove future
upstream shape. Authenticated CFS/rendered pages need environmental smoke tests;
abrupt termination cannot execute cleanup; and no local end-to-end multiprocess
Compose test was possible. The completed live-validation record is the latest
explicit upstream/deployed evidence; it was not repeated.

| Validation | Result |
| --- | --- |
| `python -m pytest` | **397 passed in 17.59s**, Python 3.14.4 |
| `python -m compileall -q .` | Passed |
| local Markdown link validation | Passed |
| `git diff --check` | Passed after edits |
| Docker/Compose build | Not run: `docker` unavailable |
| Live AFL/CFS/browser smoke | Not run by design |

## 9. Operations and deployment

The Dockerfile pins matching Playwright image/package versions. Example Compose
separates API/Admin/Scheduler, shares data/log volumes, restricts management
networks, restarts services and health-checks API/Scheduler. Startup/dedicated
commands run checksummed migrations; `DB_PATH` is consistently resolved and
missing paths fail clearly. The runbook covers SHA verification, writer
quiescence, WAL checkpoint, backup/restore rehearsal, migration, bootstrap,
integrity checks, build/start, health, gates and rollback.

First run remains operator-driven: prepare DB path, migrate, bootstrap a season
with CFS credentials, verify, then start services. `/readyz` proves a DB query,
not upstream reachability or freshness. Registry recovery re-registers only safe
future work; failed/past jobs are not replayed. Stale audits need deliberate
cutoff recovery.

This is suitable for a carefully operated **single-instance** production
deployment. Risks are SQLite contention across processes, upstream/auth drift,
manual failure/freshness observation, and the example bind-mount/API `--reload`
development posture. It is not a hardened horizontal deployment or alerting
system.

## 10. Documentation assessment

Documentation now accurately distinguishes sources, persistence, fallback,
canonical/legacy stats, CLI behavior, migrations, audit and recovery. The index,
runbook and focused guides provide a coherent operator/developer entry point,
while historical reviews are retained as records rather than rewritten.

Residual friction is conceptual density: understanding “canonical” by domain
requires combining several guides, and issue-oriented language in active docs
or old checklists can look current when linked directly. This review provides
the missing baseline. Focused future changes should be a short data-authority
map, a production-vs-example Compose note, and implementation-driven
corrections—not duplicated source matrices or broad rewrites.

## 11. Disposition of previous concerns

| Concern | Disposition | Evidence |
| --- | --- | --- |
| Operational source ambiguity | **Resolved** | Central policy; Scheduler/Admin share it; no automatic fallback/dual write. Issue #73/PR #85. |
| Hard-coded DB paths | **Resolved** | Active paths use configured connection; path tests. Issues #51/#74, PR #79. |
| Canonical club/player/season persistence | **Substantially resolved** | Seed, hierarchy, canonical identities/maps/membership and transactional bootstrap. Roster/API scope remains separate. Issues #75/#90; PRs #83/#86/#91. |
| CLI documentation mismatch | **Resolved** | Guide/help/documentation contract tests agree. Issue #76/PR #87. |
| Dual stat models | **Resolved as authority decision** | CFS table authoritative; HTML table compatibility only; no dual writes. Issue #77/PR #101. |
| JSON fixture corpus | **Resolved** | Broad public/CFS fixtures and regression tests. Issue #60/PR #100. |
| Dry-run/orchestration | **Substantially resolved** | Deterministic DB-free output, resume/overwrite and batch results; some families explicit unsupported. Issue #61/PR #102. |
| Fetch/parse/persist coupling | **Substantially resolved for pilot** | Injury stages isolated/tested. Lineups/older scrapers remain debt, not an unfulfilled injury issue. Issue #50/PR #103. |
| Injury identity safety | **Resolved for current contract** | Current club markers plus canonical resolver; unsafe rows not guessed. Issues #88/#90. |
| Version/release/runbook/live gates | **Resolved for v0.5.0** | Version, notes, full runbook and completed validation. Issues #78/#94/#95. |
| CFS roster persistence | **Accepted limitation** | Explicit read-only collector; authority/replacement design required first. |
| HTML lineup persistence | **Accepted limitation** | Explicit policy retains working behavior until canonical storage exists. |
| Undocumented upstream contracts | **Accepted limitation** | Fixtures/raw capture/live checks mitigate but cannot remove drift. |
| Canonical API coverage | **Still open as a planning choice** | Routes omit canonical player/season/team/CFS-stat reads. Do not reopen an old issue automatically. |

## 12. Remaining risks and technical debt

Issue advice below creates no issues.

### Immediate correctness or data-integrity risks

| Item | Impact / likelihood | Timing | Issue advice |
| --- | --- | --- | --- |
| New consumers may mistake legacy stats for authority or guess ID joins | High / medium | Guard any API/scoring work | Now only if that theme is accepted |
| Player-season team links may be unresolved | Medium / medium | Report/reconcile before complete squads are assumed | Later with data-quality work |
| HTML lineup semantics are not a canonical roster history | Medium / medium | Before lineup-dependent features | Now only if roster theme selected |

No demonstrated unmitigated high-likelihood corruption defect was found;
guessed injury IDs and stat dual writes are explicitly prevented.

### Operational reliability risks

| Item | Impact / likelihood | Timing | Issue advice |
| --- | --- | --- | --- |
| No failure/freshness alerting; stale runs need inspection | Medium / medium | Before unattended use matters | Candidate under operations theme |
| Shared SQLite write contention | Medium / low–medium | Measure before workload growth | Document; issue when observed/scope grows |
| Example Compose uses bind mount and `--reload` | Medium / medium if copied | Before deployment revision | Small doc issue later |
| Upstream token/page/markup drift | High / medium over time | Continuous fixture/smoke maintenance | Operational practice, not one broad issue |

### Maintainability debt

| Item | Impact / likelihood | Timing | Issue advice |
| --- | --- | --- | --- |
| Retained HTML lineups mix responsibilities | Medium / medium | Refactor with behavior/storage change | Later, not abstraction-only |
| Policy dispatcher owns connection and domain branches | Low–medium / grows with domains | Reassess after next domain | Document only |
| Parallel identity models burden contributors | Medium / high | Authority map now; retirement with design | Docs candidate; retirement later |

### Usability/documentation gaps

| Item | Impact / likelihood | Timing | Issue advice |
| --- | --- | --- | --- |
| Flag CLI and identifier dialects are confusing | Low–medium / high | If operator usage grows | Later |
| No authoritative stats/canonical-player API | High product impact / certain | Next planning decision | Only if API theme accepted |
| Freshness/data quality requires DB/log inspection | Medium / medium | Operations theme | Candidate later |

### Optional cleanup/legacy retirement

Retiring `players`, `player_stats`, old audits or direct scraper surfaces has low
urgency and potentially high migration cost: keep documented until a consumer
inventory/versioned replacement exists. Replace stale issue-number language
opportunistically, not as a standalone issue. Do not schedule a universal
collector framework without repeated concrete need.

## 13. Candidate next-phase directions

These are options, not an approved v0.6.0 roadmap.

### A. Canonical read API for downstream consumers

* **Value:** exposes authoritative CFS stats and canonical identity without DB
  access or legacy ambiguity.
* **Prerequisites:** identifiers, response versioning, lifecycle/stat fields,
  filtering and compatibility policy.
* **Fit/scope:** high; versioned match stats, canonical season players, then
  competition/team/fixture reads using existing authority.
* **Risks:** compatibility breaks, provider leakage and ambiguous crosswalks.
* **Sequence:** first if users need dependable access; never silently repoint
  compatibility routes.

### B. Canonical roster and lineup persistence

* **Value:** makes CFS selections queryable and can retire an HTML dependency.
* **Prerequisites:** selection identity, match/team links, publication/finality,
  replacement/history, player resolution and backfill rules.
* **Fit/scope:** good, but deliberately design-first; migration, adapter, policy,
  entry-point parity, reconciliation and tests.
* **Risks:** premature schema and destructive treatment of unpublished data.
* **Sequence:** before lineup-dependent behavior in downstream applications.

### C. External scoring and fantasy-league applications

* **Value:** turns ingestion into league scoring/eligibility/roster outcomes.
* **Prerequisites:** product rules, canonical CFS read model, identity
  completeness, correction/recomputation semantics, and lineups only if needed.
* **Fit/scope:** normally a separate consumer application over the canonical API,
  not a domain layer in AFL-api; its rules, league entities, deterministic
  scoring, audit, presentation and workflows remain consumer concerns.
* **Risks:** larger scope and volatile business rules.
* **Sequence:** after a narrow canonical read slice unless concrete product
  acceptance criteria lead first.

### D. Operational assurance and data quality

* **Value:** freshness, failure, unresolved-identity and reconciliation status
  reduces manual inspection.
* **Prerequisites:** SLOs, notification target, and actionable partial/
  unavailable definitions.
* **Fit/scope:** builds on audits, registry and collector outcomes; status/Admin
  views, stale policy, checks and optional alerts.
* **Risks:** noise and treating legitimate unpublished data as failure.
* **Sequence:** first for an operations goal or bounded after the primary data
  feature.

Legacy retirement, packaging hardening and historical backfill should enable an
accepted theme rather than become disconnected milestones.

## 14. Recommended planning process

1. Confirm product goals, downstream users/operators and wishlist; rank canonical
   data access, consumer needs and unattended reliability.
2. Choose one primary theme and explicitly record exclusions.
3. Validate authority/identity prerequisites on representative fixtures/data.
4. Convert only accepted recommendations into issues; do not recreate closed
   v0.5.0 issues from historical wording.
5. Define milestone acceptance criteria, offline contract tests, migration/
   rollback expectations and required live checks.
6. Produce a narrow implementation sequence and name the next release only
   after scope and release intent are approved.

The repository is a sound baseline for that conversation. The next phase
should capitalize on its canonical collection foundation rather than resume
broad reliability cleanup without demonstrated need.
