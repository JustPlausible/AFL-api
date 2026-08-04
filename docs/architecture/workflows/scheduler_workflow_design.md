# Scheduler Workflow Design

**Status:** Draft for implementation review

**Related assessment:** [Scheduler-readiness review](../project_status_scheduler_readiness.md)

**Intended milestone:** Scheduler assurance and match-window collection

## 1. Background

AFL-api has an internal APScheduler service, deterministic registered jobs,
manual Admin triggers, scrape-run audits, operational source policy, and
focused collectors. The readiness review records what those components do now
and identifies the correctness and coordination gaps that prevent unattended,
high-frequency lifecycle collection.

This document is different: it is the authoritative human-designed target for
future scheduler Issues and implementation pull requests. It defines intended
behaviour, ownership, lifecycle decisions, persistence boundaries, recovery,
and operator experience. Statements about the target are not claims that the
current implementation already provides them.

The central principle is:

> The scheduler is a decision engine rather than merely a timer. Rather than
> asking *"What timer has expired?"* it asks *"What is the most valuable
> collection action to perform now?"* The answer is determined from authoritative
> AFL state, persisted collection evidence, and current scheduling priorities,
> not simply the passage of time.

## 2. Goals and engineering principles

Version 1 prioritises correctness, simplicity, predictability, recoverability,
operator visibility, and evidence-based optimisation. SQLite compatibility and
the single-file deployment model are explicit design goals, not temporary
constraints that must be designed away.

The following principles guide implementation:

- **Optimisation follows measurement, not assumption.** More concurrency or a
  faster cadence requires evidence from latency, source, and database metrics.
- **Cadence should reflect the probability of meaningful change.** Work becomes
  more frequent near publication and live transitions, then slows after them.
- **Missed polling occurrences are not durable work items.** Recovery asks what
  is useful now rather than replaying obsolete ticks.
- **The database is the recovery checkpoint.** Authoritative persisted data and
  scheduling consequences reconstruct the plan after interruption.
- **The scheduler orchestrates; collectors collect.** Planning never absorbs
  transport, parsing, source selection, validation, or persistence ownership.

Version 1 deliberately avoids adaptive polling, automatic source promotion,
distributed coordination, predictive scheduling, and other advanced
orchestration. Such features require demonstrated need and a separate design.

## 3. Architectural position and process ownership

Retain the internal APScheduler-based service as the scheduling foundation.
APScheduler supplies the clock, wakes a central planner at a short regular
interval, and supports safe bounded one-shot requests. It is not the durable
queue for every polling occurrence.

```text
Exactly one Scheduler process per configured database
                         |
              APScheduler planner wake-up
                         |
                         v
       State-aware planner and match controllers
                         |
                         v
          Operational source-policy boundary
                         |
                         v
             Existing collectors and writers
                         |
                         v
       SQLite plans, evidence, audits, and data
```

Run exactly one dedicated scheduler process or container for each configured
database. Web, Admin, CLI, and API processes must not independently start
scheduler instances against that database. Deployment must enforce one
scheduler service with one replica. Version 1 does not add distributed locks,
leader election, or multi-node scheduler coordination; those mechanisms would
hide a deployment error and add complexity without a demonstrated requirement.

## 4. SQLite-first execution boundary

Live scheduling does not require replacing SQLite. The initial implementation
must instead make its constraints explicit:

- use short, bounded write transactions;
- never hold a database transaction open during a network request;
- start with conservative worker concurrency;
- make persistence idempotent;
- decide and document WAL and busy-timeout policy before live polling;
- measure busy waits, lock contention, and lock failures;
- avoid a backlog of obsolete polling occurrences;
- never wrap multiple matches in a broad transaction; and
- favour simplicity and observability over premature scalability.

A match execution may read a plan, release the connection, perform a network
operation, and then open a short transaction to persist its collector result
and scheduling consequence. Existing per-match authority and monotonicity
protections remain in force. API reads may proceed concurrently, while write
coordination must follow the policy selected in the prerequisite SQLite stage.

## 5. Central planning model

Do not create a hierarchy of independent interval jobs for every match and
domain. APScheduler wakes one central planner frequently enough to make due-work
decisions; the configured planner interval is not an upstream polling cadence.

Each planning cycle should:

1. read current persisted fixture, lifecycle, and collection state;
2. determine which match-domain and competition-level work is due;
3. rank due work by priority while applying starvation protection;
4. atomically claim an eligible match;
5. submit one bounded match execution;
6. record decisions, skips, and outcomes; and
7. calculate each domain's next due time.

Individual polling occurrences are not durable APScheduler jobs. The durable
record says when a domain should next be considered and why. If a planner cycle
is delayed, the next cycle considers the current state once; it does not enqueue
one task for each elapsed interval.

### 5.1 Priority and fairness

The scheduler distinguishes between **controller domains** and **collection domains**.

The primary controller domain is **match status**, which determines the current
lifecycle of a match and therefore influences the scheduling behaviour of other
domains. Before live collection begins, match status determines when player
statistics become eligible. During live play it confirms lifecycle transitions,
and after the match it determines when finalisation should begin.

Player statistics remain the highest-priority operational collection domain
because they provide the most time-sensitive end-user data. However, when lifecycle
is uncertain or may have changed, match status should be evaluated first so that
subsequent scheduling decisions are based on authoritative state.

The initial operational priority is therefore:

1. lifecycle transition checks (match status where required);
2. live player statistics;
3. post-match finalisation;
4. imminent lineup monitoring;
5. injuries;
6. fixtures and season foundations;
7. reconciliation and housekeeping.

For each match, the planner evaluates controller domains before selecting the
highest-priority collection work. Across matches, stable ordering, an age
component, or another simple documented rule should prevent lower-priority work
from starving indefinitely. Priority influences scheduling order only; it does
not override domain concurrency, pause state, source policy or persistence
rules.

Future implementations may use additional lifecycle information (such as quarter
and half-time transitions) to refine polling cadence where operational metrics
demonstrate a measurable benefit. Version 1 intentionally maintains fixed,
predictable cadences during live play.

## 6. Match controller and claim model

Use one logical controller per relevant match, backed by durable database state.
The controller is not a long-running thread. A temporary execution should:

1. atomically claim the match;
2. identify domains currently due;
3. run those domains sequentially;
4. persist each domain result in a short transaction;
5. recheck high-priority due work before release where useful;
6. calculate future due times; and
7. release the claim in guaranteed cleanup.

Only one execution may own a match at a time. All domains for that match are
initially mutually exclusive, which prevents status, stats, lineup, and manual
operations from racing their persistence decisions. Different matches may run
concurrently. Never occupy a worker continuously from pre-match until
conclusion; every execution is bounded and returns its worker.

### 6.1 Workers, atomic claims, and skips

Version 1 should use two concurrent collection workers as the conservative default.
Under normal operation these workers will process different matches concurrently,
while all collection work for a single match remains mutually exclusive.

Match execution ownership is coordinated entirely within the single scheduler process. Before
starting a match execution, the scheduler atomically determines whether the match
is already being processed. If it is, the scheduler records a benign skip and
reconsiders that match during a future planning cycle.

When a match is already active:

- record the benign reason `same_match_already_running`;
- do not report a failure or increment failure counters;
- do not queue missed polling occurrences;
- allow the current execution to complete normally; and
- reconsider the match during the next planning cycle if further work remains.

The scheduler must always release match ownership when an execution completes or
fails. If the scheduler terminates unexpectedly, any interrupted work is identified
during startup recovery using persisted execution history and scheduler heartbeat
information. Recovery is based on the current authoritative match and domain state,
not on replaying missed scheduling intervals.

This design intentionally avoids distributed locking or durable ownership
coordination. Version 1 assumes a single scheduler process per database, making
simple in-process coordination the preferred approach while retaining sufficient
persistent information for safe restart recovery.

## 7. Durable scheduling state

### 7.1 Per-match, per-domain identity

Persist and evaluate planning separately for each `match_id + domain`. Overall
match status does not prove that every domain is complete. A concluded match
may have complete status, incomplete player statistics, complete lineups, and
unavailable or incomplete interchanges.

Version 1 match-controller domains are:

1. match status;
2. player statistics; and
3. lineups.

Interchanges are the likely next live domain after the initial implementation.
Injuries, fixtures, season refreshes, and player refreshes remain
competition-level scheduler tasks rather than match-controller domains.

### 7.2 Derived state, not a second authority

Do not make a separately persisted scheduler phase authoritative. Derive match
and domain phase from:

- authoritative match lifecycle;
- current scheduled fixture time;
- structured source result;
- stored collection completeness; and
- stored snapshot authority where applicable.

Persist the consequences needed for planning and diagnosis: `next_due_at`,
current cadence, last planned time, last attempt, last success, last meaningful
change, completion state, consecutive failures, paused or blocked state, and
decision reason. A cached derived phase may support display or audit, but the
planner must correct it whenever authoritative evidence disagrees.

The schema should also retain the claim owner/lease and safe correlations to
scheduler registry and scrape-run evidence. The existing registry describes job
execution and does not by itself represent domain completeness.

## 8. Source-policy and collector boundary

The scheduler consumes the existing
[operational source policy](../../operational_source_policy.md). It must not:

- calculate source authority dynamically;
- automatically promote fallback sources;
- silently switch between CFS JSON, AFL HTML, or FootyWire;
- dual-write multiple sources; or
- inspect raw exception text to make source-policy decisions.

Collectors and source policy own source selection, transport retries,
validation, normalisation, and persistence safety. The scheduler interprets
structured outcomes and decides whether future work remains useful. Version 1
does not orchestrate fallback, although the scheduling schema and result model
should not prevent a separately designed fallback feature later.

### 8.1 Scheduler-facing collector result

Collectors should converge on a small scheduler-facing result without forcing
all internal collectors into one universal abstraction. The result should
express:

- semantic outcome;
- whether data changed;
- records written;
- source lifecycle or status;
- snapshot authority where applicable;
- domain completion state;
- whether a later attempt can be useful;
- a safe, redacted error summary; and
- request and persistence duration.

Useful semantic outcomes are `success`, `success_no_change`,
`not_yet_available`, `partial_success`, `retryable_failure`,
`permanent_failure`, and `blocked`. A successful collection with zero changed
records is still a success and updates last-success evidence. Version 1 must not
adapt cadence merely because several successful observations were unchanged.

## 9. Lifecycle and cadence

Scheduled start time is an indicator, not the highest-level source of truth.
The controller must not begin live player-stat polling solely because bounce
time has arrived. An authoritative match-status response must indicate `LIVE`.

Expected transitions are:

- before start, perform only appropriate pre-match activity;
- as scheduled start approaches, increase match-status checks;
- after scheduled time, if status remains `SCHEDULED`, keep checking and raise a
  warning after a configured prolonged-delay threshold;
- on authoritative `LIVE`, begin live player-stat polling and normally complete
  lineup monitoring;
- on authoritative `POSTGAME` or `CONCLUDED`, begin finalisation; and
- once a domain's authoritative completion condition is met, stop that domain.

Fixture time changes cause replanning; they do not leave old one-shot work in a
queue.

### 9.1 Match-status cadence

Conservative configurable defaults are:

| Window | Initial status cadence |
| --- | ---: |
| More than two hours before | 15–30 minutes |
| Two hours to five minutes before | 5 minutes |
| Final five minutes | 1 minute |
| Scheduled start passed but not `LIVE` | 30 seconds |
| Confirmed `LIVE` | 30–60 seconds |
| `POSTGAME` awaiting conclusion/finalisation | 1–5 minutes |

These are initial profiles, not permanent timing guarantees. Planner delay,
capacity, backpressure, manual pauses, and source results may affect actual
execution.

### 9.2 Player-stat cadence and finalisation

Player statistics are the highest-priority live fantasy-related domain. Start at
60 seconds per authoritatively `LIVE` match. Thirty seconds is a possible later
target only after measuring endpoint latency, rate limiting, simultaneous-match
load, request volume, and SQLite contention.

Do not collect live stats before authoritative `LIVE`. Continue after play into
post-match finalisation until the configured authoritative completion condition
is satisfied. An overall `CONCLUDED` status does not complete statistics by
itself: persist an authoritative concluded snapshot, or continue progressively
slower checks until the expiry/reconciliation threshold.

Incomplete work beyond the normal finalisation window becomes a visible
reconciliation finding rather than an infinite retry loop.

### 9.3 Lineup scheduling

Calculate lineup activity from current fixture times and the earliest scheduled
match of the round, never from a fixed Thursday assumption. Initial lineup
monitoring should begin during the expected publication window for the earliest
scheduled match, which historically occurs a little over 24 hours before the
match (commonly around 5:00 pm local time on the preceding day). This timing is
an operational expectation rather than a fixed rule and should remain
configurable.

A practical initial profile should include:

- begin monitoring during the expected initial publication window for the
  earliest scheduled match;
- periodic checks after likely publication to detect initial team announcements;
- increased polling frequency as each individual match approaches;
- checks every few minutes during the final 30 minutes before bounce to detect
  late changes;
- automatic replanning whenever fixture dates or times change;
- normal completion when the match becomes authoritatively `LIVE`.

The scheduler should increase lineup polling because the probability of
meaningful change increases as the match approaches, not simply because time has
elapsed. Actual publication timing should always be determined from
authoritative source data rather than assumed from the scheduled fixture.

The current operational source policy's explicit HTML lineup path remains in
force until canonical lineup authority is separately redesigned.

## 10. Competition-level scheduling

Competition-level work shares the central planner's priority, audit, outcome,
and configuration principles, but it does not take a match claim unless it
invokes match-scoped work.

### 10.1 Pre-season and active-season foundations

When no season is active, perform conservative weekly refreshes for:

- upcoming-season players and player details;
- teams and season membership;
- rounds; and
- fixtures and scheduled match details.

During an active season, use daily or lifecycle-aware metadata refreshes that
can detect fixture and membership corrections without unnecessary polling.
These tasks should reuse existing bootstrap and source-policy services rather
than shelling out or duplicating collection logic.

### 10.2 Injuries

Treat the AFL Tuesday injury publication as the expected weekly update window,
rather than as a fixed timestamp. Increase polling frequency leading into and
during the expected publication period to detect the weekly update as soon as it
becomes available.

Once the updated injury list has been successfully collected, reduce monitoring
to an infrequent cadence (for example, approximately every six hours) to detect
corrections or late amendments until the next expected publication window.

The expected publication window and all polling cadences should remain
configurable. The scheduler should respond to observed publication behaviour
rather than assuming a permanently fixed Tuesday schedule.

## 11. Failure ownership and retry

Collectors own immediate bounded retries for transient transport and
source-specific errors. The scheduler owns whether another scheduled attempt is
useful, longer-delay retry policy, continuation of unrelated work, pausing
blocked domains, escalation of repeated failures, and operator reporting.

Operational evidence must distinguish:

- **benign skip:** expected contention or no currently useful action;
- **collector failure:** a bounded collection attempt failed;
- **blocked task:** progress requires configuration, source-policy, data, or
  operator intervention;
- **interrupted execution:** ownership ended without normal completion;
- **expired incomplete domain:** its normal retry horizon elapsed;
- **workflow or database-fatal failure:** safe planning or persistence cannot
  continue.

These classifications have different counters and readiness effects. A benign
skip does not increase failures. Permanent or repeated failures must not hot
loop. One faulty domain may be paused while unrelated domains and matches
continue.

## 12. Restart and downtime recovery

Persist scheduler heartbeat and runtime history sufficient to determine the
previous startup, last heartbeat, graceful shutdown where possible, unexpected
disappearance, and approximate downtime.

On startup:

1. inspect the previous runtime and heartbeat;
2. identify abandoned `running` work;
3. mark stale attempts interrupted using a safe cutoff or lease rule;
4. refresh current match lifecycle where required;
5. inspect matches relevant to the downtime window;
6. compare per-domain completion evidence;
7. schedule only useful recovery work;
8. resume normal planning; and
9. record a recovery summary.

Do not replay every missed polling interval. If the scheduler stops during a
match and restarts an hour later when the match is concluded but final player
stats are absent, schedule one immediate finalisation attempt—not 120 missed
30-second polls. Existing registry and scrape-run stale-row recovery can provide
correlated evidence, but durable match-domain plans and heartbeat history are
needed to make the recovery decision.

## 13. Reconciliation

Slower reconciliation should detect:

- concluded matches without final player stats;
- stale local `LIVE` or `POSTGAME` lifecycle;
- incomplete required domains;
- interrupted or stalled executions;
- prolonged blocked domains; and
- incomplete rounds.

A daily in-season run and an after-round run are reasonable initial candidates,
subject to implementation and load review. Reconciliation reports and schedules
bounded useful repair; it does not weaken authority rules or retry forever.

## 14. Admin and operator experience

The Scheduler page should be clean and self-explanatory. At a glance it should
show:

- scheduler health and next planner run;
- upcoming competition-level tasks and matches;
- overlapping matches and current worker use;
- each match's derived phase;
- active or next-due domain;
- last attempt, last success, and whether data changed;
- prominent but non-noisy warnings and failures; and
- startup and restart-recovery activity.

Use plain-language labels such as **Refresh upcoming season**, **Check AFL
injuries**, **Refresh round line-ups**, **Track live player statistics**,
**Track match status**, **Finalise match data**, and **Reconcile completed
round**. Technical identifiers and correlations belong in expandable diagnostic
detail, not as the primary label.

### 14.1 Manual controls

Authenticated, CSRF-protected Admin controls should support:

- run one domain now;
- retry one failed domain;
- pause or resume an individual domain;
- recalculate one match plan;
- recalculate all plans; and
- inspect diagnostic history.

Per-domain pause and retry are required so a faulty source or domain under test
does not stop other match collection. Manual execution still obeys match claims,
timeouts, source policy, concurrency limits, and persistence safety. Admin must
enqueue the request for the scheduler; it must not execute collection itself,
expose arbitrary callables, or force uncontrolled concurrency. Existing manual
triggers are a safe foundation but do not yet provide this complete controller
experience.

## 15. Configuration

Store live operational scheduler configuration in the database so Admin changes
do not require rebuilding the container. Candidate values include planner
interval, worker count, per-domain cadence profiles, task timeout, finalisation
windows, failure thresholds, and domain pause state. Ship conservative defaults
and simple user-facing profiles rather than exposing every tuning constant to a
new operator.

Configuration ownership should be explicit:

- **boot-time/environment settings** identify infrastructure that cannot safely
  change in-process: database path, service bind/listen settings, process role,
  deployment-level feature enablement, and secret references;
- **database settings** control live operational behaviour: enabled domains,
  cadence profiles, two-worker default, thresholds, timeouts, and pauses.

Boot should validate database settings, apply documented defaults for absent
values, and record the effective configuration without secrets. Environment
variables may provide safe initial defaults or emergency infrastructure-level
disablement, but must not silently override an operator's live settings on every
restart. Precedence, validation, and restart requirements must be documented in
the implementation Issue.

## 16. Metrics, audit, and retention

Capture enough evidence to evaluate efficiency and justify change:

- planner cycles, tasks due, and tasks run;
- successful collections and successful unchanged collections;
- failures by domain and source, benign skips, and retries;
- total task, network, and persistence duration;
- records inserted or updated;
- SQLite busy waits, wait duration, and lock failures;
- concurrent active matches and worker utilisation;
- stale or overdue work and restart recovery actions;
- finalisation latency;
- time from scheduled start to authoritative `LIVE`; and
- source request counts plus 429 and 5xx responses where available.

Retain detailed attempt history for a bounded operational period, then compact
or aggregate summaries for longer-term comparison. Scrape-run audits remain the
safe collection evidence and should be extended only as necessary; never store
tokens, response bodies, or unsafe exception details merely for scheduler
diagnosis.

## 17. Health and readiness

Expose distinct signals for:

- process liveness;
- scheduler heartbeat health;
- planner freshness;
- worker health;
- database connectivity and write readiness;
- migration readiness;
- overdue high-priority work;
- repeated source failures;
- stale live-match collection; and
- blocked or incomplete finalisation.

Basic process liveness must remain cheap and must not fail merely because an
upstream source is temporarily unavailable. Scheduler status and readiness may
be degraded with reason codes while the process remains live. Operators should
be able to distinguish infrastructure failure from a domain-specific upstream
problem.

## 18. Version 1 non-goals

Version 1 explicitly excludes:

- fantasy scoring and consumer-defined league management;
- automatic source fallback or source-authority scoring;
- adaptive polling based on observed unchanged results;
- distributed scheduling, multi-node leadership, or coordination;
- replay of missed polling occurrences;
- arbitrary high concurrency;
- permanent replacement of existing collectors;
- a universal collector abstraction;
- commentary and interchange orchestration in the initial implementation; and
- complex predictive scheduling.

The initial design also does not authorize source blending, lower-authority
overwrites, or broad application-code rewrites.

## 19. Staged implementation plan

No open implementation Issues were present when the readiness review was
written. The following is therefore a proposed Issue breakdown, not a claim
that numbered Issues already exist. Each stage should include focused offline
tests, operator documentation, and additive migrations where relevant.

### Stage 1 — Timezone and match-day correctness

Correct aware timestamp parsing, declare the AFL match-day timezone, and cover
UTC-offset, daylight-saving, and date-boundary cases. Persist planner errors
rather than logging them only.

### Stage 2 — SQLite policy and write coordination

Decide WAL, busy timeout, write coordination, backup, and shutdown behaviour.
Test contention across threads and separate processes, measure waits/failures,
and demonstrate that no transaction spans a network request.

### Stage 3 — Match-domain plan, claim, and heartbeat schema

Add durable per-match/per-domain consequences, claim leases, scheduler runtime
heartbeat/history, completion evidence, effective configuration, and bounded
attempt history without replacing the existing registry or scrape-run audit.

### Stage 4 — Central planner and recovery

Implement one APScheduler planner wake-up, atomic match claims, two-worker
limits, priority/fairness, stale-claim recovery, useful-work-only restart
planning, and guaranteed cleanup.

### Stage 5 — Match-status controller

Implement lifecycle derivation and conservative status cadences, delayed-start
warnings, fixture-time replanning, and structured collector outcomes. Do not
activate stats based only on time.

### Stage 6 — Player-stat live polling and finalisation

Enable a controlled 60-second live default, bounded post-match finalisation,
concluded-snapshot completion, expiry/reconciliation, request metrics, and a
kill switch. Validate one, two, and overlapping-match scenarios before wider
rollout.

### Stage 7 — Lineup integration

Move existing operational lineup calls behind match-domain planning, using
round-earliest and per-match fixture windows rather than weekday assumptions.
Preserve current source and persistence policy.

### Stage 8 — Admin visibility and manual controls

Deliver the scheduler overview, plain-language labels, per-domain pause/retry,
plan recalculation, history, and restart-recovery display while preserving the
private mutation boundary.

### Stage 9 — Metrics, readiness, and reconciliation

Add separated health signals, bounded detail retention, long-term summaries,
daily/after-round reconciliation, alertable reason codes, and evidence-based
cadence review.

### Stage 10 — Interchanges and later extensions

Only after initial operational evidence, design and implement interchange
authority, persistence, outcome, and volume boundaries as a separate domain.
Evaluate other extensions independently; do not assume the stats controller is
a universal execution model.

## 20. Acceptance boundary

Version 1 is ready when one scheduler process can reconstruct and execute useful
match-domain work from SQLite; authoritative lifecycle gates live stats;
per-domain completion survives restart; claims and two-worker concurrency are
safe; post-match finalisation is bounded and visible; source policy is
unchanged; Admin controls are safe; and metrics demonstrate request, latency,
failure, recovery, and lock behaviour.

The [readiness review](../project_status_scheduler_readiness.md) remains the
evidence-based baseline for current gaps. This workflow is the implementation
target. Future Issues should cite both and state which stage and acceptance
criteria they deliver.

## Design history

This workflow was developed through iterative architectural design
between the project maintainer and AI-assisted design review.

The resulting document intentionally captures agreed engineering
principles before scheduler implementation begins. It should be
treated as the architectural reference for future scheduler Issues,
pull requests and design discussions.