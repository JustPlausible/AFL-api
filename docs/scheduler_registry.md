# Scheduler registry and restart recovery

Issue #25 adds a durable `scheduler_job_registry` table via migration `0004_scheduler_job_registry`. The registry tracks application-defined scheduler metadata and wrapper status only: planned job identity, type, durable match/round identifiers where relevant, scheduled run time, status, attempt timestamps, attempt count, and a concise last error summary. Detailed scrape execution records, imported row counts, validation outcomes, and per-run audit history are deliberately deferred to Issue #26.

## Stable job IDs

Job IDs are deterministic and never include random values or timestamps:

* `stats_match_<match_id>` for player-stat match jobs.
* `lineups_match_<match_id>` for pre-match lineup jobs.
* `lineups_round_<round_id>_<slot>` for round lineup jobs such as `day_before_5pm` and `thursday_5pm`.
* `match_refresh_<match_id>` for match-specific refresh jobs when a durable match ID is available.
* `match_refresh_live` for the interval job that refreshes live matches.
* `match_refresh_match_day` for match-day interval scraping.
* `fixtures_daily` for fixture refresh jobs.
* `injuries_daily` for injury refresh jobs.
* `refresh_<name>` for general refresh jobs, for example `refresh_players` and `refresh_matches_daily`.

## Persisted statuses

* `pending`: planned by application code and waiting to run.
* `running`: the common scheduler wrapper has started an attempt; the attempt count and last-attempt time have been updated.
* `succeeded`: the wrapper observed successful completion and recorded last-success time.
* `failed`: the wrapper observed an exception or unsuccessful command result. The registry stores only a concise, redacted summary, not a full traceback or secrets.
* `skipped`: startup reconciliation determined that a job is expired or otherwise unsafe to recover.

Upserting a planned job preserves useful history. Existing attempt counts, previous success timestamps, and failed/succeeded statuses are not reset merely because planning code sees the same logical job again.

## Startup reconciliation

APScheduler still uses its in-memory job store. On startup, after normal planning, the application compares `scheduler_job_registry` rows with APScheduler jobs. Reconciliation is idempotent: if APScheduler already has an equivalent job ID, no duplicate is created.

Conservative recovery rules:

* Pending future date-triggered jobs missing from APScheduler are re-registered exactly once by stable job ID.
* Pending past jobs are not blindly executed; they are marked `skipped` with a reason.
* Pending non-date jobs without a safe one-shot scheduled time are marked `skipped` if absent and cannot be reconstructed safely.
* Succeeded jobs are not re-registered solely because APScheduler lacks them.
* Failed jobs are not retried automatically. Operators should inspect the error summary and schedule a deliberate manual rerun if appropriate.

An individual reconciliation error is logged and counted but does not crash scheduler startup.

## Operator checks

Use `/scheduler/jobs` or the admin Schedule page to compare `apscheduler_state` with `persisted_status`. `apscheduler_state` describes the current in-memory APScheduler view (`scheduled`, `paused`, or `absent`). `persisted_status` describes the durable application registry.

For a missed or failed match-window job, search for the stable job ID (for example `stats_match_8216` or `lineups_match_8216`), compare its scheduled run time with APScheduler state, review `attempt_count`, `last_attempt_time`, `last_success_time`, and the concise `last_error_summary`, then decide whether a manual rerun is safe. Do not infer detailed scrape/import outcomes from this registry; those belong to Issue #26.
