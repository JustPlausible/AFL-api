# Engineering status and scheduler-readiness review

> **Document relationship:** This review remains the evidence-based assessment
> of the current implementation. The human-authored
> [scheduler workflow design](workflows/scheduler_workflow_design.md) defines the
> intended scheduler behaviour and implementation direction.

## Review identity and method

| Item | Current value |
| --- | --- |
| Repository/default branch | `JustPlausible/AFL-api`, `main` |
| Exact reviewed `main` revision | `db6912e95d540c4c6e3e502d5e5f1f34901cf694` (`Preserve verified team identity on null refresh (#128)`) |
| Review date | 3 August 2026 (UTC) |
| Repository version | `0.5.0`, from `version.py` and `python cli.py --version` |
| Historical comparison point | `a4ddcd1306cebf90b7a7d52766187740f47b4e8a` and `project_status_post_v0_5_0.md` |
| Automated validation | `python -m pytest`: **504 passed in 26.09s** on CPython 3.14.4 |
| GitHub state at review | **0 open issues; 0 open pull requests** (GitHub REST API) |
| Environmental validation | No live AFL/CFS/browser calls and no production database load test were performed. Conclusions about upstream behavior use checked-in contracts, fixtures, code and the existing live-validation record. |

The exact default-branch SHA and open-item state were resolved from the GitHub
API, not inferred from the working branch. Code, migrations, tests, current
operator documentation and the complete commit range were inspected. Issue and
PR descriptions explain intent but are not treated as implementation evidence.
This document supersedes the earlier review only as a current snapshot; it does
not modify that historical record.

## 1. Executive assessment

**The project is ready to begin a scheduling-focused engineering phase, but the
current scheduler is not ready for unattended lifecycle-driven, high-frequency
collection.** The collector, authority, audit, whole-season synchronisation and
completeness boundaries are now strong enough to compose rather than rewrite.
The scheduling phase should start with a small correctness/coordination tranche,
then implement a durable match-window controller and measured polling.

Three defects are prerequisites to enabling frequent production polling:

1. establish SQLite write coordination and connection policy (WAL/busy timeout,
   short transactions, and preferably one scheduler-process write lane);
2. make match-window plans and lifecycle progress durably reconstructable,
   including interrupted `running` jobs and final-snapshot confirmation; and
3. fix scheduling semantics around time parsing/day selection and separate a
   polling series from the present one-shot `stats_match_<id>` job identity.

A fourth, smaller prerequisite is to update operator documentation: the CLI
page still says completeness reporting is planned after documenting its working
command, and the scheduler guide describes the older one-shot recovery model.

A fixed 30-second loop should **not** be the initial default. Begin player-stat
polling at **60 seconds per live match**, with a global concurrency/write limit
and jitter. Move toward 30 seconds only after observing endpoint latency, 429s,
retry amplification, simultaneous-match load and SQLite lock time. Use slower
cadences before and after live play, and stop only after an authoritative
concluded snapshot is persisted and completeness checks pass.

The recommended primary next-phase theme is **operational assurance and data
quality, expressed through scheduler readiness**. The secondary follow-up is a
**canonical read API**. Canonical roster/lineup persistence remains valuable but
is not a prerequisite for stat scheduling; fantasy/scoring stays a downstream
consumer concern.

## 2. Change since the previous review

### 2.1 All merged work after `a4ddcd1`

The following is the complete first-parent-equivalent commit range present on
current `main` (14 merged PR commits):

| PR | Commit | Delivered change and assessment effect |
| --- | --- | --- |
| #104 | `6006605` | Added the prior post-v0.5.0 status baseline. Historical only. |
| #105 | `cb6bec6` | Clarified consumer-neutral scope; strengthens the boundary that fantasy rules do not belong in ingestion. |
| #113 | `2c3fcae` | Designed whole-season sync and established explicit lifecycle, audit and transaction requirements. |
| #114 | `3157755` | Rejects conflicting CLI operations before runtime loading; resolves the previous first-flag ambiguity. |
| #116 | `7eec367` | Preserves successful zero-match scraper behavior during CLI extraction. |
| #117 | `7dddeff` | Standardised CLI source/persistence diagnostics and extracted lightweight runtime handlers. |
| #118 | `fef58f6` | Implemented idempotent whole-season metadata/player/stat sync with bounded filters and authoritative-snapshot protection. |
| #121 | `a59d3d6` | Documented supported first-run and operator command workflows. |
| #122 | `4889f34` | Added a concise data-authority and identifier map, reducing canonical/legacy ambiguity. |
| #123 | `a631a8c` | Hardened season-sync audit finalisation and made exclusive connection/transaction ownership explicit. |
| #124 | `ac1db2a` | Persisted correlated audit decisions for skips and unsafe cases, improving explanation and recovery evidence. |
| #125 | `3458cf3` | Added deterministic, read-only season completeness/reconciliation reporting and exit semantics. |
| #127 | `e750ddd` | Distinguished development Compose from a production-like deployment and documented deployment operation. |
| #128 | `db6912e` | Prevented null stat refreshes from erasing verified CFS team-provider identity. |

The range contains the baseline review itself (#104) plus 13 subsequent feature
or documentation merges. GitHub reports no current open issue or PR. There is
therefore no active tracker backlog to substitute for the gaps identified here.

### 2.2 Quantified change

The suite grew from **397 to 504 tests** (+107). The current migration head is
`0012_season_sync_decisions`, adding correlated season-sync decision evidence.
The release version remains `0.5.0`; the new capability has not yet been cut as
a new declared release.

Whole-season sync, completeness reporting, audit resilience, CLI extraction and
operator documentation materially improve scheduler feasibility. The CFS
team-provider fix closes a concrete identity-erasure risk during repeated live
snapshots. None of those merges, however, installs lifecycle polling, a durable
polling plan, a writer coordinator, or scheduler-aware freshness readiness.

## 3. Disposition of the prior section 11 concerns

| Prior concern | Current disposition | Current evidence / remaining boundary |
| --- | --- | --- |
| Operational source ambiguity | **Resolved** | `SOURCE_POLICY` still makes Scheduler/Admin sources explicit; there is no automatic HTML fallback or dual write. |
| Hard-coded database paths | **Resolved** | Active connections use configured `DB_PATH`; read-only reporting has a query-only connection. |
| Canonical club/player/season persistence | **Substantially resolved, narrowed** | Season sync now refreshes the foundation and stats idempotently. Canonical roster/lineup persistence and canonical reads remain separate. |
| CLI documentation mismatch | **Narrowed, not fully resolved** | Conflicting flags are rejected and runtime/help are tested, but `docs/cli.md` still calls the now-implemented completeness report “planned.” |
| Dual stat models | **Resolved as an accepted authority decision** | CFS writes `cfs_player_stats`; HTML remains compatibility-only; scheduling must not fallback or dual-write. |
| JSON fixture corpus | **Resolved for offline regression** | Coverage now includes sync and completeness semantics; live drift remains unavoidable. |
| Dry-run/orchestration | **Substantially resolved** | Database-free collection remains useful for live validation, but it is not the production scheduler path. |
| Fetch/parse/persist coupling | **Substantially resolved for existing pilots** | Stat and season services are composable. Legacy HTML lineup coupling remains accepted debt. |
| Injury identity safety | **Resolved for the current contract** | Only canonical-resolved injuries persist. |
| Version/release/runbook/live gates | **Resolved for v0.5.0; active again for next milestone** | New sync/report/scheduler work needs its own live and rollback gate; version is still 0.5.0. |
| CFS roster persistence | **Accepted limitation** | Read-only collection remains intentional. It does not block stats scheduling. |
| HTML lineup persistence | **Accepted limitation** | Existing scheduled/Admin lineup paths remain HTML-backed. No silent replacement is allowed. |
| Undocumented upstream contracts | **Accepted and mitigated** | Contracts, reduced fixtures, raw capture, retry policy and live checks mitigate but cannot eliminate drift. |
| Canonical API coverage | **Still active as a product gap** | It is recommended as the secondary theme, after scheduler assurance. |

## 4. Reassessment of prior section 12 risks

### Correctness and integrity

* Legacy/CFS authority confusion is **narrowed** by the authority map and season
  report, but API consumers can still read only legacy stats.
* Nullable player-team membership is **narrowed** by completeness findings and
  fixed by #128 against destructive null refresh. Genuine unresolved identities
  remain visible rather than guessed.
* HTML lineups remain an **accepted limitation**, not canonical roster history.
* Whole-season sync now has explicit per-match transactions, skip decisions,
  audit correlation and final-snapshot authority. This substantially resolves
  the earlier absence of a safe bulk reconciliation boundary.

### Operational reliability

* Failure/freshness visibility is **narrowed, still active**. `scrape_runs`, the
  scheduler registry and season report provide evidence, but `/readyz` checks
  only `SELECT 1`; there are no staleness thresholds, alerts or match-window
  readiness summaries.
* SQLite contention is **more important than before**. Five APScheduler worker
  threads, separate API/Admin/Scheduler processes and independent default SQLite
  connections have no repository-wide busy timeout, WAL setup or write queue.
  Existing transactions are well bounded, but frequent concurrent snapshots
  would materially change contention probability.
* Production Compose ambiguity is **resolved/narrowed** by a separate
  production-like example. Horizontal/multi-writer scaling remains unsupported.
* Upstream/token drift remains **active and accepted**. `AflJsonClient` retries
  transient failures at most three times and refreshes authentication once;
  one client reuses one token only within its process/client lifetime.

### Maintainability and usability

CLI extraction, diagnostics, operator workflows and the authority map materially
reduce contributor/operator friction. Scheduler registration remains spread
across static decorators and several registration modules, with lifecycle logic
encoded in SQL/status strings. This is manageable for current jobs but should be
centralised into a match-window planner before adding more high-frequency
families. Do not create a universal collector framework.

## 5. Current architecture and operational readiness

### 5.1 Scheduler startup, registry and jobs

`scheduler.start` runs migrations, imports static cron jobs, registers dynamic
jobs once under a process lock, reconciles the durable registry, then starts one
`BlockingScheduler` either directly or in a FastAPI-managed daemon thread. Its
memory job store and five-thread executor mean APScheduler trigger state is not
durable. The SQLite `scheduler_job_registry` is an application registry, not an
APScheduler job store.

The registry stores stable IDs, job type/target, planned time, status, attempts,
success/error summaries, callable references and arguments. Its common wrapper
marks running/succeeded/failed and exports the job ID so `scrape_runs` can
correlate scheduler execution. Startup safely reconstructs only future pending
one-shot date jobs. Past pending jobs are skipped; failed/succeeded jobs are not
blindly replayed.

Current registration is:

| Domain | Present scheduling behavior | Readiness judgement |
| --- | --- | --- |
| Metadata/fixtures | Daily public JSON refreshes; a match-day status interval every 5 minutes; live-match status interval every 5 minutes | Useful foundation, but match-day interval is registered only when the 09:00 check sees a same-day match and is not explicitly removed after the day. SQLite `localtime` need not equal scheduler AWST. |
| Match status | Public detail, per live/today match, policy-routed and audited | Lifecycle resolver is reusable; cadence/controller semantics need work. |
| Player stats | One job per UPCOMING/LIVE match at start +10 seconds; one immediate startup job for LIVE matches if not recently logged | Not polling. The stable ID represents one attempt, so it cannot express a durable series or final confirmation. Recentness checks legacy `scrape_log`, not authoritative `scrape_runs`/`cfs_player_stats`. |
| Lineups | Round T-1 17:00, Thursday 17:00 where applicable, and match T-1 hour; HTML policy | Adequate for accepted legacy scope. Past jobs are safely skipped. |
| Injuries | Daily at 11:00 AWST, HTML policy | Adequate for current scope. |
| Player refresh | Legacy leaderboard every five days | Not the canonical season-player refresh. Its name can mislead operators. |
| Whole-season sync/report | CLI services only; no Scheduler/Admin registration | Correct current safety boundary, but scheduler phase should reuse their service/evidence rather than shelling out. |

Date parsing in stat registration uses
`datetime.fromisoformat(value).replace(tzinfo=UTC)`, which can overwrite a real
offset. Other modules use `astimezone` directly. This must be normalised before
window scheduling. Registration catches per-match errors and only logs them,
without a durable planning-failure record.

### 5.2 Persistence, transactions and processes

Collectors generally obtain a connection per operational call and close it.
Season sync is stronger: it rejects a caller-owned active transaction and owns
bootstrap, per-match and audit transaction boundaries, with defensive audit
finalisation. The completeness reporter uses a query-only connection. Stat
upserts preserve final authority and verified team identity.

What is missing is **coordination above those correct local boundaries**.
SQLite connections use library defaults: no explicit busy timeout, WAL policy or
application write mutex/queue. APScheduler permits five simultaneous jobs, and
production-like Compose separates Scheduler, API and Admin processes over one
DB volume. A Python lock would not coordinate those processes. Initial
high-frequency writes must therefore run through a single scheduler-owned
write lane (or an explicit cross-process strategy), with short transactions and
measured lock/error telemetry. API reads can remain concurrent.

### 5.3 Source, audit and failure behavior

Source policy is scheduler-ready: metadata/status use public JSON, stats use
CFS, lineups/injuries use their explicit HTML paths. It prohibits fallback and
dual write. Keep that invariant.

Transport retries are bounded (three transient attempts with backoff), CFS
“not published” is a typed non-success, authentication gets one token refresh,
and errors are body-free/redacted. A client caches its token, but creating a
client for every tick may reacquire tokens; the match-window worker should own a
long-lived client per scheduler process, not share a `requests.Session` across
uncoordinated threads.

`scrape_runs` provides trigger/correlation, lifecycle, counts and safe failures;
season sync adds parent/child and decision audits. Registry status describes
execution, not collection completeness. Abrupt death can leave registry/audit
rows `running`; current reconciliation ignores them. A scheduler phase needs a
lease/heartbeat or startup cutoff rule that marks interrupted attempts and
replans from persisted match/final-snapshot state.

### 5.4 Admin, health and documentation

Admin can safely queue one injury, fixture-round, lineup-round, lineup-match or
player-stat-match job with validation, CSRF/auth boundaries, duplicate checks
and correlation. It cannot request a season sync, report, or lifecycle polling
window. That is a safe limitation. A future manual control should enqueue a
bounded match window or a single reconciliation attempt, never accept arbitrary
callables or execute collection in the Admin process.

`/healthz` is liveness. `/readyz` verifies only that the configured DB accepts
`SELECT 1`; it says nothing about scheduler running state, migration head,
registry recovery errors, upstream authentication, latest successful metadata,
live-match poll freshness, final authoritative snapshots or SQLite lock
pressure. Keep liveness cheap, but add a scheduler-specific readiness/status
view with reason codes rather than making upstream availability a hard process
readiness dependency.

Current docs are strong on registry inspection, Admin triggers, source policy,
deployment, audit and CLI workflows. They do not yet operate a lifecycle polling
controller. The contradictory “report planned” sentence in `docs/cli.md` and the
registry guide's obsolete Issue #26 wording should be corrected in the first
scheduler tranche.

## 6. Scheduler-specific gap analysis

| Gap | Severity before frequent polling | Required response |
| --- | --- | --- |
| No durable match-window plan/state machine | **Blocker** | Persist per-match desired lifecycle, next due time, cadence, terminal confirmation and lease/attempt state. |
| No repeated stat polling/final confirmation | **Blocker** | Add lifecycle-driven ticks; do not overload one-shot job semantics. |
| SQLite write coordination/default connection settings | **Blocker** | Single write lane initially; explicit busy timeout/WAL decision; concurrency/load tests. |
| Timezone/day-query inconsistency | **Blocker** | Parse aware UTC safely and compute AFL match day in one declared timezone. |
| Interrupted `running` recovery | **High** | Lease/expiry plus audit repair/replan based on authoritative DB state. |
| Planning failures only logged | **High** | Persist planner audit/failure reason and expose it. |
| Five worker threads with no domain limits | **High** | Per-domain/global limits; coalesce/max-instances/misfire policies. |
| Token reuse tied to short-lived clients | **High at volume** | Scheduler-process client lifecycle, refresh-on-401, never persist token. |
| No 429/latency/lock metrics or Retry-After policy | **High** | Record attempts/outcomes/duration; respect supported retry signals and apply global backoff. |
| Readiness is DB-only | **Medium** | Add non-secret freshness/failure/scheduler status endpoint and Admin display. |
| Legacy `scrape_log` drives stat recovery | **Medium** | Use `scrape_runs` plus authoritative snapshot timestamps/status. |
| Static/dynamic registry docs are stale | **Medium** | Update operator runbook and recovery table. |
| Commentary/interchange collectors absent | **Not a blocker** | Explicitly defer until supported structured contracts exist. |

## 7. Recommended scheduling model

### 7.1 Architecture

Compose existing services around a narrow **match-window planner/controller**:

1. A low-frequency metadata planner refreshes the public hierarchy and evaluates
   upcoming/recent matches.
2. It persists one match-window record per match with lifecycle, next due time,
   cadence class, last authoritative outcome, consecutive unavailable/failure
   counts, lease and terminal confirmation.
3. A single scheduler tick (for example every 15 seconds) claims only due work,
   with deterministic per-match jitter and global/per-domain concurrency caps.
   The tick frequency is not the upstream request frequency.
4. Network collection occurs outside a SQLite write transaction. Results enter
   a scheduler-owned serialized write lane; existing policy collectors/writers
   and audit correlation remain authoritative.
5. The controller reconciles public lifecycle and CFS snapshot classification,
   calculates the next due time, and terminates only on a concluded authoritative
   snapshot that passes defined completeness gates.
6. Startup expires abandoned leases, finalises/annotates stale audit attempts,
   and rebuilds work from matches plus persisted window state. It never replays
   arbitrary failed jobs blindly.

Use APScheduler for clock/wakeup and manual one-shots, the registry/audit for
execution evidence, and a small migration for durable window state. Do not
replace APScheduler, source policy, season sync, completeness reporting or stat
persistence.

### 7.2 Practical initial cadence

Cadence must be configuration with safe floors, jitter and an emergency disable,
not scattered constants.

| Lifecycle | Initial player-stat/status cadence | Rationale |
| --- | --- | --- |
| More than 24h before start | Metadata every 6h; no stats | Fixture corrections are useful; unpublished CFS polling is waste. |
| 24h to 2h before start | Metadata every 60m; no stats | Detect schedule/status movement cheaply. Lineups remain on existing jobs. |
| 2h to 15m before start | Status every 15m; optional one CFS availability probe near T-15m | Avoid repeated expected 404/unpublished responses. |
| T-15m through scheduled start | Status every 5m; stats every 5m only after publication | Handles delayed start/publication without burst retries. |
| LIVE | **Stats every 60s initially**; public status every 2m | At nine concurrent matches this is about 9 CFS requests/min plus 4.5 status requests/min before retries—material but measurable. Thirty seconds doubles it. |
| POSTGAME/provisional | Stats every 2m for 20m, then every 5m | Allows delayed corrections/publication without live-rate pressure. |
| CONCLUDED without authoritative complete snapshot | Every 10m for 1h, then 30m with a bounded horizon (suggest 24h) | Slow reconciliation; surface incompleteness rather than retry forever. |
| CONCLUDED + authoritative complete snapshot | Stop; recheck only via explicit reconciliation/daily completeness policy | Makes terminal state durable and restart-safe. |

A normal 2.5-hour match at 60 seconds is roughly 150 stat requests; at 30
seconds it is roughly 300. Nine simultaneous matches would be approximately
1,350 versus 2,700 CFS requests, excluding retries, status and token calls. The
repository contains no measured upstream quota or 429 baseline, so 30 seconds
cannot yet be justified. After a live trial, allow 30 seconds only if p95 latency,
429 rate, retry volume, database lock time and snapshot change rate meet an
agreed budget. Conversely, automatically back off globally on 429s or sustained
latency/failure.

Higher-frequency commentary/interchange collection is only a future capability.
Those families are currently explicit unsupported skips. If contracts later
exist, isolate them from stat cadence and storage, measure payload/write volume,
and use append/batch semantics; do not assume the stats lane can absorb them.

### 7.3 Retry and failure policy

Keep transport retries bounded. Do not schedule three client retries every 30
seconds indefinitely. Classify:

* unpublished/unavailable before or just after start: successful observation,
  bounded lifecycle backoff, not a corruption failure;
* 429/transient server/network: client-bounded retry, then global/domain backoff
  with jitter and visible failure;
* authentication failure after one refresh: open a domain circuit and alert;
* validation/identity/partial result: persist only what current authority rules
  permit, audit partiality, retain prior final data and continue at slower cadence;
* permanent planning/schema/database error: do not retry hot; expose readiness
  degradation and require operator action.

Every attempt carries match-window, scheduler-job and scrape-run correlation.
Freshness should report last attempted, last successful collection, last write,
last authoritative concluded snapshot, next due and consecutive failures.

## 8. Staged implementation sequence

### Stage 0 — prerequisite correctness and measurement

* Correct timezone parsing and match-day selection; add offset/DST/boundary tests.
* Decide/configure SQLite WAL and busy timeout; serialize scheduler writes and
  test lock behavior across threads and processes.
* Add timing, response-class, retry/429 and DB-lock metrics to safe audits/logs.
* Correct CLI/scheduler/operator documentation.
* Define cadence, concurrency and disable/rollback configuration.

### Stage 1 — durable planner and recovery

* Add a migration for match-window state/leases without replacing the registry.
* Implement idempotent planning from canonical matches and metadata refresh.
* Expire interrupted leases and annotate stale running registry/audit attempts.
* Add planning/freshness status to Scheduler/Admin inspection.

### Stage 2 — conservative stat lifecycle pilot

* Enable one competition/season and a small match allowlist at 60-second live
  cadence, one or two concurrent network calls and one serialized writer.
* Reuse a scheduler-process CFS client/token; retain bounded client retry.
* Continue post-match until authoritative concluded + completeness confirmation.
* Validate restart during fetch, before write, after write and after audit.

### Stage 3 — operational assurance and controlled expansion

* Run all matches with caps/jitter/backpressure; add stale/failure readiness
  reasons and optional alert integration.
* Run completeness after match/round/season boundaries and link findings to
  audits without making the reporter mutate data.
* Tune cadence from evidence; only then trial 30 seconds if justified.

### Stage 4 — optional domains

Consider canonical roster/lineup polling only after its schema/authority design.
Consider commentary/interchange only after supported contracts, persistence and
volume tests. Neither belongs in the first scheduler milestone.

## 9. Candidate GitHub issues (do not create automatically)

1. **Correct scheduler timezone parsing and AFL match-day selection** — aware UTC
   parser, declared AFL timezone, date-boundary tests, durable planning errors.
2. **Define SQLite connection and scheduler write-coordination policy** — WAL/
   busy timeout decision, serialized write lane, multiprocess contention tests.
3. **Persist lifecycle-driven match-window plans and leases** — migration,
   state model, idempotent planner, expiry and terminal final confirmation.
4. **Implement conservative CFS live/post-match polling** — 60-second default,
   jitter, caps, coalescing, bounded backoff and feature flag.
5. **Recover interrupted scheduler and scrape-run attempts safely** — stale
   running cutoff, reconciliation reasons and no blind replay.
6. **Expose scheduler freshness/failure readiness and Admin status** — last
   success/authority, next due, consecutive failures, lock/backoff state.
7. **Unify scheduler audits with authoritative snapshot/completeness evidence** —
   remove `scrape_log` recentness dependency and correlate report findings.
8. **Update scheduler, CLI and deployment operator runbooks** — cadence, disable,
   drain, restart, manual reconciliation, and correct report documentation.
9. **Run a controlled live load/rate validation** — 1/3/9-match scenarios,
   request/429/latency/token/write-lock evidence and cadence decision record.
10. **Design versioned canonical stat read endpoints** — secondary milestone,
    after scheduler evidence is trustworthy.

## 10. Next-phase direction reassessment

### Primary: operational assurance and data quality

**Completed:** durable registry basics; correlated/redacted scrape audits;
source/persistence diagnostics; audit-finalisation hardening; explicit sync skip
decisions; deterministic completeness findings/status/exit code; safer
production Compose guidance; Admin inspection/manual triggers.

**Remaining:** durable match-window state, stale-running recovery, freshness SLOs,
writer coordination, rate/load evidence, planner failures, scheduler-specific
readiness, alert targets and an operator-controlled polling rollback. This theme
is the natural home for scheduler readiness and should be the next milestone.

### Secondary: canonical read API

**Completed:** canonical competition/season/team/player/provider models,
authoritative CFS stats, lifecycle semantics, whole-season population,
completeness evidence and authority documentation.

**Remaining:** versioned response models, stable identifier vocabulary,
competition/season/match/player resources, authoritative match-stat endpoints,
filters/pagination/freshness fields, compatibility policy and contract tests.
Existing legacy routes must not silently change tables. Scheduling first makes
freshness and finality meaningful for these consumers.

### Canonical roster/lineup persistence

**Completed:** CFS roster collection/normalisation/change states and file-only
inspection; canonical player/team/match identity foundations; existing HTML
lineup operational jobs.

**Remaining:** publication/finality semantics, canonical selection/history
schema, player/team resolution rules, reconciliation/backfill, writer, API,
Scheduler/Admin parity and HTML retirement policy. Valuable, but defer until
after scheduler assurance unless a concrete lineup consumer takes priority.

### Fantasy/scoring consumers

**Completed:** authoritative stat ingestion and consumer-neutral data foundations.

**Remaining:** essentially all product rules, scoring versions, recomputation,
league/roster entities and presentation. Build this outside AFL-api over a
canonical versioned API. It is neither the scheduling theme nor the immediate
secondary milestone.

## 11. Explicitly out of scope

* A broad scheduler/collector/database rewrite or replacement of APScheduler.
* PostgreSQL, distributed scheduling or horizontal multi-writer support in the
  initial milestone.
* Automatic HTML fallback, source blending or CFS/HTML dual write.
* Silent replacement or retirement of legacy API/stat/lineup tables.
* Canonical roster/lineup persistence in the first scheduler milestone.
* Commentary/interchange scraping before verified structured contracts.
* Fantasy/scoring/business rules inside AFL-api.
* Unbounded retries, guessed identities, or treating unpublished data as empty.
* Public exposure of Scheduler mutation endpoints or upstream credentials.

## 12. Tests, live validation and rollback expectations

### Automated acceptance

* Preserve the full offline suite and add deterministic clock/timezone tests,
  lifecycle transition tables, planner idempotency, lease expiry, retry/backoff,
  coalescing/max-instance and final-confirmation tests.
* Test crash points before/after network, write, registry and audit commits.
* Test SQLite contention with multiple threads **and separate processes**, while
  confirming readers remain usable and final snapshots cannot regress.
* Test simultaneous-match request budgets, token reuse/401 refresh, 429/global
  backoff and no HTML fallback/dual write.
* Test `/healthz`, DB readiness and scheduler freshness responses separately.
* Keep migration upgrade/checksum/rollback-backup tests and Markdown links.

### Live gate

Use a dedicated backed-up database and an allowlisted match set. Record exact
SHA/config, upstream request count by endpoint, token acquisitions, retries,
429s, latency percentiles, concurrent matches, lock waits/errors, audit links,
snapshot transitions and final completeness. Validate at 60 seconds first; a
30-second trial is a separate evidence-based decision. Include delayed,
unpublished and concluded cases and restart the scheduler during a live window.
No secrets or raw authenticated headers belong in the record.

### Rollback

Polling must have a feature flag/kill switch and drain behavior. Roll back by
disabling new claims, allowing or expiring leases, checkpointing/backing up
SQLite safely, stopping the scheduler, restoring the previous image/SHA and—if
needed—the pre-migration database backup. New migrations should be additive so
the old application can ignore the window table where practical. Never roll
back by copying only a live WAL-mode main database file. Verify integrity,
registry/audit state, authoritative snapshot counts and API health after restore.

## 13. Recommended next milestone boundary

**Milestone: “Scheduler assurance and match-window collection.”** It ends when:

1. timezone and SQLite coordination prerequisites are fixed and tested;
2. metadata-driven match windows are durably planned and restart-safe;
3. CFS stats poll at a measured, configurable 60-second live default with
   concurrency/rate backpressure and process-local token reuse;
4. post-match polling stops only after an authoritative concluded, complete
   snapshot or a visible bounded-horizon failure;
5. every attempt is correlated through registry/audit and freshness is visible
   in Scheduler/Admin status;
6. no fallback or dual write occurs;
7. multiprocess SQLite and controlled live validation pass with a documented
   request/lock budget; and
8. operators can enable, disable, drain, recover and roll back the feature from
   current documentation.

Do not include canonical API endpoints, canonical lineup persistence,
commentary/interchange or fantasy rules in this milestone. After it closes,
start the secondary canonical read API milestone using the now-reliable
freshness/finality evidence.

## 14. Issue #134 interrupted-attempt investigation and implementation plan

The implementation baseline for recovery is revisions #130–#133. Investigation
confirmed the following state and transaction map before recovery code was
changed:

* `match_stat_windows` is one durable CFS polling series per match and policy.
  Its controlled states are `planned`, `due`, `leased`, `backoff`,
  `awaiting_final`, `planning_error`, `complete`, `failed_terminal`, `disabled`,
  `cancelled`, and `not_applicable`. Claims use a monotonically increasing
  `lease_generation` and an owner/token/claimed/expiry tuple. Attempt and dynamic
  scheduler-job IDs are deterministically derived from window ID, generation,
  and attempt count, but were not stored on the window.
* `scheduler_job_registry` allowed `pending`, `running`, `succeeded`, `failed`,
  and `skipped`; its original schema had match correlation but no window,
  attempt, scrape-run, lease-generation, or lease-token columns.
  `scrape_runs` allowed `running`, `completed`, `partial`, and `failed`; polling
  used the attempt ID as `correlation_id`, but did not store the window/job/lease
  correlations explicitly.
* A polling claim committed first through the Scheduler write lane. Scrape-run
  creation then committed before the request. Collection ran without a database
  transaction. Successful domain upsert, scrape-run finalisation, and window
  update shared one short write-lane transaction. Registry context updates were
  separate and, for dynamic polling IDs, no registry row had first been
  inserted. Consequently a process could stop after any earlier commit, and
  match-wide CFS rows—not attempt-specific evidence—were the only persistence
  evidence.
* Finality is derived only from `cfs_player_stats`: authority level 2, a uniform
  authoritative snapshot, both `home` and `away`, and at least 20 authoritative
  rows. The window additionally requires concluded lifecycle before completion.
  Legacy `player_stats` is not consulted. Existing writer results expose total
  changed/written rows, while inserted/updated/unchanged values were not
  persisted per polling attempt.
* Startup ran migrations, reconciled match windows, registered dynamic jobs,
  reconciled future pending one-shots, and only then started APScheduler. It had
  no runtime heartbeat/history. Graceful shutdown drained the polling executor
  and write lane, but did not persist a graceful marker. The process-local write
  lane and token/generation predicates are the required mutation boundary.

The focused implementation plan is to add migration `0014` with additive
correlation/recovery evidence and explicit interrupted terminal states; add one
write-lane reconciliation service with conservative settings, optimistic lease
checks, authoritative-CFS decisions, structured/redacted reports, and no
collector dependency; create polling audit/control rows with complete
correlations and attempt-specific persistence evidence; establish runtime
identity and run recovery before planner/job registration; expose the same
service through a bounded `python -m scheduler.recovery` command; add controlled
clock, state-matrix, idempotency, concurrency, startup-order, dry-run, scope,
redaction, and integrity tests; and update the workflow/operator documentation.
A replacement lease is never stolen, historical IDs are retained, and any retry
is represented by a future claim with a new generation/attempt/job identity.

### Issue #134 refinement validation

Review of the draft implementation confirmed one concern was narrower than it
first appeared: `claim_due_windows()` did return the same derived job ID assigned
to the local `job_id` in `run_claim()`. Nevertheless, the claimed dictionary is
not refreshed after registry insertion and was too implicit a boundary. The
worker now passes one immutable execution identity (job, attempt, scrape run,
lease token, and generation) through success, non-final, skip, failure,
unexpected-error, and lost-lease finalisation. Durable tests assert that the
created registry identity never remains `running` after any normal terminal
path.

The persistence concern resolved to the stronger atomic design. CFS rows,
window consequence, audit completion/commit marker, and registry completion/
commit marker share one write-lane transaction. Injected rollback coverage
proves none survive together. Recovery tests now separately cover historical
unknown evidence, final match evidence, heartbeat ownership, strict later-only
supersession, multiple attempt rows, compatibility records, dry-run database
identity, planner-owned horizon/feature decisions, per-attempt savepoint
isolation, and migration fidelity from a populated migration-0013 database.
The migration rebuild remains necessary only because SQLite cannot add values to
existing CHECK constraints; it copies every prior column and row, retains
original defaults/constraints, captures and recreates the repository-known
indexes, triggers, and dependent views exercised by migration tests, and adds
focused correlation indexes. This is a repository compatibility guarantee, not
a claim to preserve every dependency an arbitrary external SQLite consumer may
have created.

The final refinement bounds active-heartbeat protection by maximum attempt
duration, blocks a window action for the run when its attempt repair savepoint
fails, and requires exactly one correlated running registry row, scrape row,
and owned window row in normal atomic finalisation. Startup examines at most the
configured candidate limit (default 500) from each durable candidate source.

#### Issue #134 file-to-requirement map

| Files | Recovery requirement |
|---|---|
| `.env.example`, `config.py` | Conservative heartbeat, stale-state, grace, and bounded-startup configuration. |
| `db/migrations/0014_interrupted_attempt_recovery.py`, `db/scrape_runs.py` | Backward-compatible attempt correlation, recovery evidence, runtime ownership, and model fields. |
| `scheduler/player_stat_polling.py`, `scheduler/registry.py` | Immutable execution identity and atomic domain/control-plane terminalisation. |
| `scheduler/recovery.py`, `scheduler/match_windows.py` | Shared startup/manual reconciler, optimistic lease repair, structured report, and planner-owned consequences. |
| `scheduler/runtime.py`, `scheduler/scheduled_tasks.py`, `scheduler/start.py` | Process identity, heartbeat/graceful-stop evidence, and recovery-before-polling startup order. |
| `tests/test_interrupted_attempt_recovery.py`, `tests/test_player_stat_polling.py`, `tests/test_migration_runner.py`, `tests/test_scheduler_startup.py`, `tests/test_scrape_runs.py` | Offline crash, evidence, concurrency, compatibility, migration, ordering, and durable-state verification. |
| This workflow/readiness document, `docs/scheduler_registry.md`, `docs/scrape_run_audit.md` | Transaction, evidence, recovery-policy, and operator guidance. |

No changed production path serves functionality outside interrupted-attempt
recovery, its required correlation/atomicity and ownership evidence, startup or
manual invocation, and compatibility. In particular, no collector, scheduling
backend, or unrelated domain workflow is introduced.
