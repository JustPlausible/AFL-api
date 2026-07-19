# Persist scheduler job state and recover safely after restarts

## Background / problem statement

Scheduled injury, lineup, fixture, and player-stat jobs are registered dynamically at runtime. After container restarts or process crashes, the scheduler depends on startup reconstruction and can lose important context about what was expected to run, what actually ran, and what needs to be retried. This creates operational risk during AFL match windows, when missed player-stat scrapes are time-sensitive.

## Scope

Add durable scheduler job metadata and recovery logic so the service can reconcile expected jobs with actual APScheduler state after startup.

## Implementation requirements

- Add a database-backed scheduler job registry for application-defined jobs, including job id, job type, match/round identifiers where applicable, scheduled run time, status, last attempt time, last success time, attempt count, and last error summary.
- Standardise generated job ids using a documented pattern such as `stats_match_<match_id>`, `lineups_match_<match_id>`, or `injuries_daily`.
- Update scheduler registration code to upsert registry rows when jobs are planned.
- On scheduler startup, reconcile registry rows with APScheduler jobs and re-register eligible pending jobs that have not expired.
- Mark jobs as running, succeeded, failed, or skipped from the wrapper that invokes scraper commands.
- Expose the persisted status in the scheduler JSON endpoint and admin schedule view.

## Out of scope

- Adding distributed locking for multiple scheduler replicas.
- Replacing APScheduler.
- Building a full retry queue or background worker platform.
- Changing scrape frequency rules except where necessary to support recovery.

## Acceptance criteria

- Restarting the scheduler preserves visibility into jobs registered before shutdown.
- Pending future jobs are re-registered after restart.
- Past jobs are not blindly rerun unless they are explicitly eligible and documented as safe.
- Failed job attempts record an actionable error summary.
- Admin and JSON views distinguish APScheduler in-memory state from persisted job status.

## Testing requirements

- Unit tests for job id generation and registry upsert behavior.
- Integration tests for startup reconciliation with pending, succeeded, failed, and expired jobs.
- Tests that scraper wrapper failures update job status without crashing scheduler startup.
- Existing scheduler endpoint tests continue to pass.

## Documentation updates required

- Update scheduler documentation to describe persisted job states and restart behavior.
- Document job id naming conventions.
- Add operational notes for investigating missed or failed match-window jobs.

## Migration / backward-compatibility considerations

- Existing deployments without scheduler registry rows must start normally and begin populating the registry as jobs are registered.
- Existing `/scheduler/jobs` consumers should continue to receive current fields; new persisted fields should be additive.
- Admin schedule page changes should not require new environment variables.
