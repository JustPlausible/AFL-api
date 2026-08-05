# Durable match-window planner

Issue #132 adds `match_stat_windows` as the durable control-plane table for lifecycle-driven CFS match-stat collection. APScheduler remains memory-backed, `scheduler_job_registry` remains a per-attempt execution registry, and `scrape_runs` remains the audit record for one collection execution.

## Identity

* Polling series / match window: `mw_cfs_stats_<canonical_match_id>_<policy_version>`.
* Attempt identity: `<window_id>_attempt_<lease_generation>_<attempt_number>`.
* Scheduler registry job identity for future attempts: `mw_attempt_<window_id>_<lease_generation>_<attempt_number>`.
* Legacy one-shot jobs keep the compatibility format `stats_match_<id>` and are not blindly migrated, replayed, or reused as polling-series identifiers.

## State vocabulary

Statuses are `planned`, `due`, `leased`, `backoff`, `awaiting_final`, `planning_error`, `complete`, `failed_terminal`, `disabled`, `cancelled`, and `not_applicable`. Collection phases are `not_started`, `pre_match`, `live`, `post_game`, `final_confirmation`, `complete`, and `none`. Stable reason codes include `future_outside_window`, `approaching_start`, `live`, `awaiting_final`, `final_stats_unavailable_or_partial`, `authoritative_final_confirmed`, `postponed`, `cancelled`, `unknown_lifecycle`, `contradictory_lifecycle`, `missing_start_time`, `missing_provider_identity`, `polling_horizon_exceeded`, `feature_disabled`, `unsupported_competition_or_season`, `lease_expired_reclaimed`, `attempt_failed_backoff`, `attempt_succeeded_non_final`, and `released`.

## Transition rules and finality

The planner reads persisted canonical `matches` and `rounds`, applies the existing AFL timestamp/timezone policy, and uses `afl_json.match_status.normalise_match_status` rather than duplicating lifecycle parsing. It never invokes collectors, network clients, HTML fallback, or player-stat writes.

A window is complete only when CFS has an authoritative concluded snapshot: `cfs_player_stats.snapshot_authority=2`, both sides are present, and the persisted authoritative row count reaches the repository's conservative completed-match floor. Elapsed time or estimated match end alone is never finality proof. Concluded matches with unavailable or partial statistics remain `awaiting_final` until the bounded post-match horizon, after which they become terminal with `polling_horizon_exceeded`. The horizon is anchored to persisted lifecycle observation time for post-game/concluded evidence when available; otherwise it falls back to scheduled start plus the documented conservative expected match duration.

## Leases

Due claiming runs through the Scheduler SQLite write lane and opens a short `BEGIN IMMEDIATE` transaction. The claim query checks due status/time and lease expiry, then mutates the same row to `leased`, assigning `lease_owner`, `lease_token`, `lease_generation`, `lease_claimed_at`, and `lease_expires_at`. Unexpired leases cannot be stolen. Expired leases can be reclaimed and are annotated with `lease_expired_reclaimed`. Attempt result updates compare against the current `lease_token`, preventing stale owners from completing or mutating a replaced lease.

## Startup and inspection

Scheduler startup runs bounded reconciliation before dynamic job registration. Reconciliation creates missing windows idempotently, updates lifecycle/start-time decisions, preserves attempt/failure history, preserves valid unexpired leases as fully leased, clears expired leases with a recovery reason, and leaves completed/cancelled/terminal rows inspectable. Recoverable metadata problems such as missing provider identity, missing time, or unknown lifecycle are stored as `planning_error` and can reopen when metadata is repaired. It does not schedule repeated production CFS polling; cadence names are placeholders for Issue #133.

Operators can inspect `/scheduler/match-windows` for match IDs, provider IDs, competition/season context, lifecycle, phase, status, next due time, cadence, lease owner/expiry, last attempts/successes, finality, counts, reason code, and series ID. Public/admin mutation controls are intentionally out of scope.

## Configuration

Settings use the existing environment/config mechanism:

* `AFL_MATCH_WINDOW_PLANNER_ENABLED` disables planning without deleting plans or authoritative data.
* `AFL_MATCH_WINDOW_PRE_MATCH_SECONDS` controls the pre-match eligibility window.
* `AFL_MATCH_WINDOW_POST_HORIZON_SECONDS` bounds post-match final confirmation.
* `AFL_MATCH_WINDOW_LEASE_SECONDS` sets bounded attempt ownership.
* `AFL_MATCH_WINDOW_RECONCILE_SECONDS` documents reconciliation cadence only; it does not enable polling.
* `AFL_MATCH_WINDOW_EXPECTED_MATCH_SECONDS` is the conservative fallback expected-duration anchor used only when no persisted post-game/concluded observation time exists.
* `AFL_MATCH_WINDOW_SUPPORTED_COMPETITIONS` and `AFL_MATCH_WINDOW_SUPPORTED_SEASONS` are optional comma-separated allowlists.
* `AFL_MATCH_WINDOW_POLICY_VERSION` participates in the one-active-window-per-match-policy uniqueness rule.

The supported deployment remains one active Scheduler process with SQLite WAL/write-lane coordination; Redis, Celery, PostgreSQL, distributed schedulers, and database-backed APScheduler job stores remain out of scope.

## Issue #133 polling implementation

Issue #133 is recommendation four in the scheduler-readiness sequence. It builds on the timezone policy from Issue #130, SQLite connection/write-lane policy from Issue #131, and durable match-window leases/finality from Issue #132. The implementation adds a conservative `PlayerStatPollingWorker` over existing `match_stat_windows`; it does not add per-occurrence durable APScheduler jobs, a second planner, a queue, fallback collection, or a new persistence path.

APScheduler wakes one coalesced planner job (`player_stat_polling_planner`) every 15 seconds. That wake-up is only a due-work decision point. Due windows are claimed through the existing match-window claim function and the scheduler-owned write lane. Each bounded attempt releases its worker after one current-state collection and persistence decision, so downtime coalesces into the next useful decision rather than replaying every missed interval.

### Lifecycle, cadence, and finality

Player-stat polling is disabled by default for safe rollout. When enabled, live collection requires an authoritative `LIVE` lifecycle already persisted by the planner; scheduled bounce time alone is not enough. The default live cadence is 60 seconds before jitter. Pre-match/unpublished observations use slower cadence, post-match/final-confirmation uses slower cadence, and HTTP/auth/transient failures use bounded backoff. `next_due_at` is persisted after every accepted attempt so restart behavior preserves the last cadence decision.

A match-domain closes only after CFS has written an authoritative concluded snapshot to `cfs_player_stats` and the shared season-report completeness predicate passes. Overall match conclusion, elapsed time, or estimated match duration alone cannot close the player-stat domain. If the post-match horizon expires without final complete evidence, the existing planner records `polling_horizon_exceeded` as visible incomplete operator-action state rather than falsely marking the domain complete.

### Concurrency, SQLite, and source authority

The conservative defaults are two collection workers, two CFS/player-stat network permits, one in-process owner per match, and one serialized Scheduler write lane. Network collection happens before the persistence callback enters the write lane; the write callback performs only short SQLite work: CFS upsert, finality inspection, audit finalisation, and window rescheduling/release. Writer wait and transaction timing continue to be emitted by `scheduler.write_lane` diagnostics.

CFS JSON remains the operational authority for player statistics. Accepted records are passed to the existing `upsert_player_stats` writer and therefore write only `cfs_player_stats`. The worker never invokes HTML fallback and never writes `player_stats`. Existing snapshot-authority protections prevent stale or lower-authority observations from regressing authoritative final data; lower-authority observations after completion are audited and ignored for persistence.

### CFS client lifecycle

The scheduler process owns a `SchedulerCfsClientPool`. It uses per-thread HTTP sessions to avoid sharing an unsafe `requests.Session` across uncontrolled threads, while sharing one process-local `WMCTokenProvider` so the CFS token is acquired lazily and reused. The underlying client still performs exactly one refresh after a 401. Owned sessions are closed on worker shutdown, and diagnostics use the existing audit redaction helpers rather than logging credentials, cookies, authorization headers, or tokens.

### Controls and operations

Environment/config controls follow the existing conventions:

* `AFL_PLAYER_STAT_POLLING_ENABLED` globally enables the pilot; default `false`.
* `AFL_PLAYER_STAT_POLLING_KILL_SWITCH` immediately prevents new claims.
* `AFL_PLAYER_STAT_POLLING_DRAIN` prevents new claims while active attempts settle.
* `AFL_PLAYER_STAT_POLLING_ALLOWED_COMPETITIONS`, `AFL_PLAYER_STAT_POLLING_ALLOWED_SEASONS`, and `AFL_PLAYER_STAT_POLLING_ALLOWED_MATCHES` provide rollout allowlists.
* `AFL_PLAYER_STAT_POLLING_LIVE_SECONDS` defaults to `60`; post/pre/unavailable cadence and failure backoff values are separately configurable.
* `AFL_PLAYER_STAT_POLLING_MAX_WORKERS`, `AFL_PLAYER_STAT_POLLING_NETWORK_CONCURRENCY`, `AFL_PLAYER_STAT_POLLING_CLAIM_LIMIT`, and `AFL_PLAYER_STAT_POLLING_JITTER_SECONDS` bound concurrency and spread simultaneous matches.

Disable, drain, and kill-switch controls do not delete planner history, leases, scrape-run evidence, registry rows, or authoritative statistics. For live validation, use a backed-up database, a single Scheduler process, and a small match allowlist; record request counts, 429s, token acquisitions, latency, write-lane waits, finality transitions, and restart behavior before expanding the rollout. Rollback by disabling new claims or draining, stopping the scheduler after active attempts settle or leases expire, taking a safe SQLite backup including WAL state, and deploying the previous image/SHA. Do not copy only the main database file while WAL is active.

### Troubleshooting

* Unpublished or temporarily unavailable CFS data should show `final_stats_unavailable_or_partial` or an unavailable cadence with zero writes and no failure increment.
* HTTP 429 and transient transport/server failures should move the window to backoff with a bounded future `next_due_at`; unrelated due matches continue.
* Repeated authentication failure should appear as an auth pause/backoff state with redacted diagnostics.
* SQLite lock pressure should be investigated with `scheduler.write_lane` wait/transaction diagnostics before increasing concurrency.
* Interrupted attempts are recovered by the existing lease-expiry reconciliation and replanned from current match/window/finality state.
* Bounded-horizon expiry remains visibly incomplete (`polling_horizon_exceeded`) and requires operator reconciliation rather than automatic completion.
