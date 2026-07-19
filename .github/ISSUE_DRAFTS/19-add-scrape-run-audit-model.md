# Add a unified scrape run audit model

## Background / problem statement

The project records scrape attempts and summaries, but the logging is spread across scraper-specific code and does not provide a consistent audit trail for all scrape types. Operators need one place to answer: what ran, for which entity, with which parameters, how long it took, what changed, and why it failed. A unified scrape run model will make debugging, alerting, and admin reporting much easier.

## Scope

Create a normalized `scrape_runs` audit table and helper API used by scrapers and scheduled jobs.

## Implementation requirements

- Add a `scrape_runs` table via migration with fields for scrape type, target type, target identifier, trigger source, status, started timestamp, finished timestamp, duration, rows read/written where available, error class, error message summary, and correlation/job id.
- Add helper functions for starting, completing, and failing a scrape run.
- Update at least the fixture, injury, lineup, match, and player-stat scraping entry points to use the shared helper.
- Preserve existing `scrape_log` and `scrape_summary` behavior during the first implementation so current admin/reporting code does not break.
- Include a lightweight query helper for recent runs filtered by scrape type and status.

## Out of scope

- Removing existing `scrape_log` or `scrape_summary` tables.
- Building a new dashboard page beyond minimal visibility needed for acceptance.
- Adding external observability services.
- Guaranteeing exact row-change counts for every scraper if a scraper cannot provide that information cheaply.

## Acceptance criteria

- Every supported scraper records a `running` row before network/database work begins.
- Successful runs are marked complete with a finish time and duration.
- Failed runs are marked failed with a concise error summary and do not leave stale `running` rows in normal exception paths.
- Scheduled jobs can link scrape runs back to their scheduler job id.
- Existing summary/log consumers continue to work.

## Testing requirements

- Unit tests for audit helper state transitions and error truncation/redaction.
- Tests for at least one successful and one failing invocation per updated scraper entry-point wrapper.
- Regression tests that existing scrape summary/log writes are still performed where they existed before.

## Documentation updates required

- Document the scrape run lifecycle and status values.
- Add examples of queries operators can use to inspect recent failures.
- Update any admin/scheduler docs that refer to scrape logging.

## Migration / backward-compatibility considerations

- The new table must be additive and safe for existing databases.
- Existing log tables remain intact and are not backfilled in this issue.
- Error messages must avoid storing secrets, API keys, cookies, or full request headers.
