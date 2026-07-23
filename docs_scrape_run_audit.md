# Scrape run audit records

`scrape_runs` is an additive operational audit table for one operator-requested scrape invocation. It does not replace or backfill the existing `scrape_log` and `scrape_summary` tables; those tables and their existing writers remain available for current admin and reporting consumers.

## Lifecycle and trigger sources

Every run starts as `running` before the scraper performs its main network or database work. Normal success changes the row to `completed`; normal exceptions change it to `failed` and re-raise the original exception. Canonical trigger sources are `cli`, `scheduler`, `admin_manual`, and `startup_recovery`.

CLI scrapers default to `cli`. Scheduler-launched jobs use the Issue #25 scheduler `job_id` contract, stored as `correlation_id`, such as `fixtures_daily`, `injuries_daily`, `lineups_match_<match_id>`, `lineups_round_<round>_<slot>`, `match_refresh_<match_id>`, or `stats_match_<match_id>`. Admin-manual callers should pass `admin_manual`; stale-run recovery records failed rows with a concise recovery reason.

## Nullable fields

`target_type`, `target_identifier`, `finished_at`, `duration_ms`, `rows_read`, `rows_written`, `error_class`, `error_summary`, and `correlation_id` may be `NULL`. Row counts are only populated when the scraper can report them cheaply and accurately; match refreshes may leave them empty rather than storing misleading values.

## Error summaries

`scrape_runs.error_summary` stores a concise summary capped at 500 characters. Authorization headers, cookies, API keys, tokens, session identifiers, secret-bearing query parameters, and database connection passwords are redacted before storage. Full tracebacks are not stored in `scrape_runs`; existing application logs keep their current behavior.

## Stale running rows

A row that remains `running` after an explicit age threshold usually means the process, container, or host terminated before normal exception handling ran. Use `db.scrape_runs.recover_stale_running_runs(older_than=...)` during an intentional startup-recovery operation to mark only rows older than a safe cutoff as `failed`; do not use a cutoff that could include active scrapes.

## SQLite inspection queries

Most recent runs:

```sql
SELECT run_id, scrape_type, target_type, target_identifier, trigger_source,
       status, started_at, finished_at, duration_ms, correlation_id
FROM scrape_runs
ORDER BY started_at DESC
LIMIT 20;
```

Failed runs newest first:

```sql
SELECT scrape_type, target_type, target_identifier, started_at, finished_at,
       error_class, error_summary
FROM scrape_runs
WHERE status = 'failed'
ORDER BY started_at DESC;
```

Runs filtered by scrape type:

```sql
SELECT *
FROM scrape_runs
WHERE scrape_type = 'player_stats'
ORDER BY started_at DESC
LIMIT 50;
```

Runs linked to a scheduler job ID:

```sql
SELECT *
FROM scrape_runs
WHERE correlation_id = 'stats_match_8042'
ORDER BY started_at DESC;
```

Stale rows still marked running:

```sql
SELECT run_id, scrape_type, target_type, target_identifier, started_at, correlation_id
FROM scrape_runs
WHERE status = 'running'
  AND started_at < datetime('now', '-2 hours')
ORDER BY started_at ASC;
```
