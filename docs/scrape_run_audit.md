# Scrape run audit records

`scrape_runs` is an additive operational audit table for one operator-requested scrape invocation. It does not replace or backfill the existing `scrape_log` and `scrape_summary` tables; those tables and their existing writers remain available for current admin and reporting consumers.

## Lifecycle and trigger sources

Every run starts as `running` before the scraper performs its main network or database work. Normal success changes the row to `completed`; normal exceptions change it to `failed` and re-raise the original exception. Canonical trigger sources are `cli`, `scheduler`, `admin_manual`, and `startup_recovery`.

## Season synchronizer transaction and outcome contract

`SeasonSynchronizer` owns its supplied SQLite connection for the complete
operation. The connection must be usable and have no active transaction at
entry. The service rejects `conn.in_transaction == true` before it creates an
audit row, calls a remote collector, or performs persistence. Callers must not
place unrelated work on that connection: bootstrap, per-match authoritative
persistence, and audit helpers use separate commits and rollbacks.

Season-sync machine results report collection, authoritative persistence, and
audit finalisation independently. A match with `persistence_outcome` equal to
`committed` remains collected with its exact row counts if its audit update
fails; `audit_outcome`, the audit and correlation IDs, a redacted audit error
class/summary, and `processing_continued` describe that operational failure.
The season result reports aggregate `audit_failures` and parent
`audit_outcome`. Safe audit-only failures produce a partial CLI result and
allow later independent matches to run. Processing stops only when rollback
and a simple connection probe cannot establish a clean, usable connection.

Matches deliberately not sent to `MatchPlayerStatsCollector` are stored as
`afl_season_sync_decision` child rows. They share the parent season run's
`correlation_id`, retain distinct `run_id` values, and use `reason_code` plus
`decision_class` (`safe` or `material`) rather than conflating pre-collection
decisions with unavailable, empty, partial, unknown, or failed collection.
Their `rows_read` and `rows_written` are both zero. Canonical, provider, and
round identities are populated only when known; an absent explicit target is
kept in `target_identifier` without a fabricated match relationship.

CLI scrapers default to `cli`. Scheduler-launched jobs use the Issue #25 scheduler `job_id` contract, stored as `correlation_id`, such as `fixtures_daily`, `injuries_daily`, `lineups_match_<match_id>`, `lineups_round_<round>_<slot>`, `match_refresh_<match_id>`, or `stats_match_<match_id>`. Admin-manual callers should pass `admin_manual`; stale-run recovery records failed rows with a concise recovery reason.

## Nullable fields

`target_type`, `target_identifier`, `finished_at`, `duration_ms`, `rows_read`, `rows_written`, `error_class`, `error_summary`, and `correlation_id` may be `NULL`. Row counts are only populated when the scraper can report them cheaply and accurately; match refreshes may leave them empty rather than storing misleading values.

## Error summaries

`scrape_runs.error_summary` stores a concise summary capped at 500 characters. Authorization headers, cookies, API keys, tokens, session identifiers, secret-bearing query parameters, and database connection passwords are redacted before storage. Full tracebacks are not stored in `scrape_runs`; existing application logs keep their current behavior.

## Stale running rows

A row that remains `running` after an explicit age threshold usually means the process, container, or host terminated before normal exception handling ran. Use `db.scrape_runs.recover_stale_running_runs(older_than=...)` during an intentional startup-recovery operation to mark only rows older than a safe cutoff as `failed`; do not use a cutoff that could include active scrapes.

## SQLite inspection queries

Season-sync decisions from one parent invocation:

```sql
SELECT run_id, target_identifier, canonical_match_id, provider_match_id,
       round_identifier, reason_code, decision_class, status,
       rows_read, rows_written, diagnostic_summary
FROM scrape_runs
WHERE scrape_type = 'afl_season_sync_decision'
  AND correlation_id = '<season-correlation-id>'
ORDER BY started_at, run_id;
```

Decision history for a canonical match or unresolved requested target:

```sql
SELECT run_id, correlation_id, target_type, target_identifier, reason_code,
       decision_class, started_at
FROM scrape_runs
WHERE scrape_type = 'afl_season_sync_decision'
  AND target_identifier = '8001'
ORDER BY started_at DESC;
```

Decisions with one stable reason code:

```sql
SELECT run_id, correlation_id, target_identifier, decision_class, started_at
FROM scrape_runs
WHERE scrape_type = 'afl_season_sync_decision'
  AND reason_code = 'missing_provider_identity'
ORDER BY started_at DESC;
```

Recent parent runs and their correlated decisions:

```sql
SELECT run_id, correlation_id, scrape_type, target_identifier, reason_code,
       decision_class, status, started_at
FROM scrape_runs
WHERE scrape_type IN ('afl_season_sync', 'afl_season_sync_decision')
ORDER BY started_at DESC
LIMIT 100;
```

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
# Interrupted polling audits

Polling audits now checkpoint response receipt separately from the atomic CFS
persistence transaction. `persistence_committed_at` is positive,
attempt-specific commit evidence; its absence is not proof of rollback.
Recovered `interrupted` rows retain original correlation and timing fields and
add a stable recovery classification, reconciliation run/time, persistence
evidence (`committed`, `uncommitted`, or `unknown`), and any superseding attempt.
Counts remain null unless the normal transaction actually recorded them; the
reconciler never invents inserted, updated, unchanged, written, or completion
counts from match-wide rows.

```sql
SELECT run_id, window_id, attempt_id, scheduler_job_id, status,
       response_received_at, persistence_committed_at, rows_read, rows_written,
       recovery_reason, attempt_persistence_evidence,
       match_authoritative_evidence, superseded_by_attempt_id
FROM scrape_runs
WHERE window_id = ?
ORDER BY started_at;
```

Only `cfs_player_stats` supplies authoritative domain evidence. Legacy
`player_stats` and HTML are never consulted by recovery.
