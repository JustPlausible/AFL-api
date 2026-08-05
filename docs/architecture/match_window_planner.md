# Durable match-window planner

Issue #132 adds `match_stat_windows` as the durable control-plane table for lifecycle-driven CFS match-stat collection. APScheduler remains memory-backed, `scheduler_job_registry` remains a per-attempt execution registry, and `scrape_runs` remains the audit record for one collection execution.

## Identity

* Polling series / match window: `mw_cfs_stats_<canonical_match_id>_<policy_version>`.
* Attempt identity: `<window_id>_attempt_<lease_generation>_<attempt_number>`.
* Scheduler registry job identity for future attempts: `mw_attempt_<window_id>_<lease_generation>_<attempt_number>`.
* Legacy one-shot jobs keep the compatibility format `stats_match_<id>` and are not blindly migrated, replayed, or reused as polling-series identifiers.

## State vocabulary

Statuses are `planned`, `due`, `leased`, `backoff`, `awaiting_final`, `complete`, `failed_terminal`, `disabled`, `cancelled`, and `not_applicable`. Collection phases are `not_started`, `pre_match`, `live`, `post_game`, `final_confirmation`, `complete`, and `none`. Stable reason codes include `future_outside_window`, `approaching_start`, `live`, `awaiting_final`, `final_stats_unavailable_or_partial`, `authoritative_final_confirmed`, `postponed`, `cancelled`, `unknown_lifecycle`, `contradictory_lifecycle`, `missing_start_time`, `missing_provider_identity`, `polling_horizon_exceeded`, `feature_disabled`, `unsupported_competition_or_season`, `lease_expired_reclaimed`, `attempt_failed_backoff`, `attempt_succeeded_non_final`, and `released`.

## Transition rules and finality

The planner reads persisted canonical `matches` and `rounds`, applies the existing AFL timestamp/timezone policy, and uses `afl_json.match_status.normalise_match_status` rather than duplicating lifecycle parsing. It never invokes collectors, network clients, HTML fallback, or player-stat writes.

A window is complete only when CFS has an authoritative concluded snapshot: `cfs_player_stats.snapshot_authority=2`, both sides are present, and the persisted authoritative row count reaches the repository's conservative completed-match floor. Elapsed time or estimated match end alone is never finality proof. Concluded matches with unavailable or partial statistics remain `awaiting_final` until the bounded post-match horizon, after which they become terminal with `polling_horizon_exceeded`.

## Leases

Due claiming runs through the Scheduler SQLite write lane and opens a short `BEGIN IMMEDIATE` transaction. The claim query checks due status/time and lease expiry, then mutates the same row to `leased`, assigning `lease_owner`, `lease_token`, `lease_generation`, `lease_claimed_at`, and `lease_expires_at`. Unexpired leases cannot be stolen. Expired leases can be reclaimed and are annotated with `lease_expired_reclaimed`. Attempt result updates compare against the current `lease_token`, preventing stale owners from completing or mutating a replaced lease.

## Startup and inspection

Scheduler startup runs bounded reconciliation before dynamic job registration. Reconciliation creates missing windows idempotently, updates lifecycle/start-time decisions, preserves attempt/failure history, clears expired leases, and leaves completed/cancelled/terminal rows inspectable. It does not schedule repeated production CFS polling; cadence names are placeholders for Issue #133.

Operators can inspect `/scheduler/match-windows` for match IDs, provider IDs, competition/season context, lifecycle, phase, status, next due time, cadence, lease owner/expiry, last attempts/successes, finality, counts, reason code, and series ID. Public/admin mutation controls are intentionally out of scope.

## Configuration

Settings use the existing environment/config mechanism:

* `AFL_MATCH_WINDOW_PLANNER_ENABLED` disables planning without deleting plans or authoritative data.
* `AFL_MATCH_WINDOW_PRE_MATCH_SECONDS` controls the pre-match eligibility window.
* `AFL_MATCH_WINDOW_POST_HORIZON_SECONDS` bounds post-match final confirmation.
* `AFL_MATCH_WINDOW_LEASE_SECONDS` sets bounded attempt ownership.
* `AFL_MATCH_WINDOW_RECONCILE_SECONDS` documents reconciliation cadence only; it does not enable polling.
* `AFL_MATCH_WINDOW_SUPPORTED_COMPETITIONS` and `AFL_MATCH_WINDOW_SUPPORTED_SEASONS` are optional comma-separated allowlists.
* `AFL_MATCH_WINDOW_POLICY_VERSION` participates in the one-active-window-per-match-policy uniqueness rule.

The supported deployment remains one active Scheduler process with SQLite WAL/write-lane coordination; Redis, Celery, PostgreSQL, distributed schedulers, and database-backed APScheduler job stores remain out of scope.
