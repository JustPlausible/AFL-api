# SQLite connection and Scheduler coordination policy

This document records the implemented issue #131 policy. It is deliberately a
narrow SQLite policy, not a general database-access rewrite.

## Decisions

| Decision | Value and rationale |
|---|---|
| Python connect timeout | **10 seconds**: bounded coexistence with short API/Admin writes without stalling a Scheduler worker indefinitely. |
| `busy_timeout` | **10,000 ms**, set on every shared connection so native SQLite calls use the same bound. |
| journal | **WAL**, established and verified by `migrate_database`; readers can proceed during a Scheduler write in the supported local/shared-volume topology. WAL is persistent database state. |
| synchronous | **NORMAL**, set per connection; with WAL this is the intended performance/durability balance (committed data remains consistent, though the newest transaction can be lost on host power loss). |
| transactions | Python **DEFERRED implicit transactions**. Existing service-owned boundaries remain intact; no global autocommit. Network and parsing must finish before a write transaction. |
| Scheduler lane | One fair-enough process-local mutex and fresh connection per callback. It coordinates worker threads in the sole Scheduler, not API/Admin processes. |
| waiting/retry | Lane wait is bounded only by preceding bounded callbacks; SQLite lock wait is at most 10 seconds. There is **no automatic retry**: busy/locked errors return to existing scheduling policy. |
| duplicate guard | Startup rejects `AFL_SCHEDULER_REPLICAS` other than `1`. This makes orchestrator configuration mistakes visible. Two independently launched processes that both declare `1` are **not detected**; operators must enforce the one-replica topology. This deliberately avoids pretending that a configuration assertion is a lease. |
| backup | Prefer Python/SQLite online backup API. For cold copies, drain/stop Scheduler writers and copy the database plus `-wal` and `-shm` as one state (or checkpoint first). |

`foreign_keys=ON`, `busy_timeout`, `synchronous=NORMAL`, the `Row` factory,
and (for reports) `query_only=ON` are per-connection. `journal_mode=WAL` is
persistent and is changed only at migration/startup. Read-only connections use
URI `mode=ro`, never initialise policy, and therefore cannot create or mutate a
database. Connections are owned and closed by callers; the write lane always
closes its connection.

## Active connection inventory

| Path | Process/access | Connection and transaction owner | Duration/network/overlap | Shared helper? |
|---|---|---|---|---|
| `api/routes.py`, `health.py`, `collection/source_policy.py` | API request threads; routes are read-mostly, collection is read/write | request/service owns helper connection and implicit transaction | one request/bounded persistence; API requests can overlap Scheduler | shared helper |
| `admin.py` | Admin request threads; read/write | endpoint owns direct connection and explicit commit | bounded form action; can overlap Scheduler | direct legacy connection, retained to avoid unrelated Admin rewrite |
| `auth.py` | API authentication request thread; read-only lookup | authentication helper owns direct connection; no write transaction | one lookup; can overlap Scheduler | direct legacy connection, retained because it has an isolated security contract |
| `scheduler/registry.py` | Scheduler planner/worker threads; read/write | registry owns connections; worker attempt/final states use lane-owned transactions | short single-row state transitions; overlaps other workers | helper; attempt/success/failure use lane, bootstrap registration remains direct before jobs start |
| `scheduler/schedule_*` and `scheduler/manual_triggers.py` | Scheduler workers (including Admin-enqueued work); read plus domain writes | scheduler collection wrapper and lane own write connections/transactions | HTTP and parsing outside transaction; frequent writes overlap API/Admin | shared helper and lane |
| `db/scrape_runs.py` | API, Scheduler, CLI; read/write audit | service or caller connection; explicit defensive start/final commits | short audit transitions; Scheduler wrapper lanes each transition separately | shared helper/caller-provided; Scheduler operational collection uses lane |
| `scraper/*.py` and `scraper/injuries/persistence.py` | CLI or low-frequency Scheduler injury job; read/write | scraper owns helper connection; injuries accepts caller connection | legacy collector duration; some legacy looping code can span HTTP waits and is excluded from future polling | mostly helper; intentionally outside lane until collector split, except lineup persistence routed by policy wrapper |
| `afl_json/season_sync.py`, bootstrap/player persistence | CLI/bootstrap thread; read/write | synchronizer owns caller connection and explicit bootstrap/per-match/audit commit/rollback | bounded phases; network precedes persistence; may overlap only if operator violates bootstrap guidance | caller-provided/helper; deliberately not one lane unit |
| `db/migration_runner.py` and `db/migrations/*` | startup/migration process; read/write | runner owns a direct connection, one startup `BEGIN IMMEDIATE`, and a savepoint per migration | startup-only, no network, before Scheduler admission; concurrent starters cannot observe an intermediate schema | direct by design; persistent WAL is established before the migration writer claim |
| `scripts/manage_api_keys.py`, `scripts/update_summary.py` | operator CLI; read/write | script owns connection and commits | operator-bounded; can overlap only if deliberately run live | API-key script direct legacy; summary uses helper |
| `utils/club_lookup.py` | utility in caller thread; read-only | context manager owns direct connection | one lookup; may overlap Scheduler | direct legacy lightweight lookup |
| `tests/*` | pytest thread/subprocess; mixed | fixture/test owns direct or helper connection | test-scoped temporary/in-memory state | direct intentionally for fixtures/contention; application-behaviour tests use helper |

Repository review also found explicit commits/rollbacks in player persistence,
injury persistence, scrape-run finalisation and season sync. Those boundaries
are intentionally preserved, including authoritative final-snapshot safeguards.

## Write-lane contract and diagnostics

`SchedulerWriteLane.execute(operation_name, target_id, callback)` accepts only
a persistence callback. The lane opens a fresh policy connection after entry,
passes it to the callback, commits its complete unit on return, rolls back on
error, closes it, releases ownership, and propagates values/exceptions. Ambient
caller transactions and nested lane calls are prohibited. The callback never
closes the lane-owned connection. Existing bounded persistence services that
deliberately own an internal commit/rollback (`persist_afl_metadata`, lineup and
scrape-run helpers) retain that contract; the lane's final commit is then a
no-op and still does not admit the next writer until the service unit finishes.
New callbacks leave commit/rollback to the lane. `drain()` rejects new
submissions and waits for queued/claimed units, for shutdown, migration, and
backup coordination.

The stable log fields are `operation`, `target_id`, `lane_wait_ms`,
`transaction_ms`, `result`, `failure_class`, `retry`, and `queued_writers`.
Failures distinguish SQLite busy/locked from application failures; rollback
failure has its own event. Payloads, SQL parameters, tokens and response bodies
are never logged.

### Production integration and ordering

`scheduler.collection.collect_scheduled` is the production entry point used by
scheduled CFS stats, match status, metadata/fixtures, lineups, and Admin-enqueued
equivalents. Registry `mark_running` enters the lane first. Audit creation then
enters and commits a separate lane unit. Provider lookup, HTTP, parsing, and
validation use a read connection with no active transaction. Only the domain
persistence callback enters the lane. Audit completion/failure then commits as
a separate defensive unit, followed by registry success/failure in its own lane
unit. Thus a failed domain callback rolls back without erasing the attempt, and
neither audit nor registry is left falsely successful.

Scheduler job correlation and nested-audit suppression use context-local state,
not mutable process environment, so concurrent APScheduler worker threads
cannot borrow another job's correlation ID or suppress its scrape-run record.

Injury orchestration remains a low-frequency legacy self-owned transaction and
is not represented as coordinated; it is not a future high-frequency polling
path. Registry bootstrap/upsert and planning failures run before worker
admission. Whole-season synchronization retains its per-bootstrap/per-match/
audit transactions and is never wrapped as one lane operation.

Calling `drain(timeout)` atomically rejects new submissions, allows already
accepted callbacks to complete, and returns false with a structured timeout
diagnostic rather than waiting forever. Scheduler shutdown first asks
APScheduler to stop admission and finish executor jobs, then drains the lane for
at most 30 seconds. Connections close on success and failure; rollback/close
failures are logged without replacing the original callback exception.

## Deployment, backup and restore

Exactly one Scheduler replica and any required API/Admin processes may share
one database **directory**. Mounting only the `.db` file is unsupported: WAL's
`-wal` and `-shm` sidecars must be on the same persistent volume. WAL does not
coordinate multiple Scheduler processes.

Copying the main file while WAL is active can omit committed pages. Prefer
`sqlite3.Connection.backup()` against a destination connection (it produces a
consistent online snapshot). `VACUUM INTO` is another controlled option when
supported. For a file-level backup, stop job admission, drain the lane, stop the
Scheduler, run `PRAGMA wal_checkpoint(TRUNCATE)`, close all processes, then copy
the main file and any remaining sidecars together. Migrations, restores, and
cold backups require the same drain/stop boundary.

Restore with all application processes stopped, replace the complete database
state in its mounted directory, then run `PRAGMA integrity_check` (must return
`ok`), run the migration command/startup to migration head, and inspect
`PRAGMA journal_mode` (must return `wal`) before restarting exactly one
Scheduler. A rollback must restore schema/data and WAL state from the same
snapshot. Follow the existing [release and rollback runbook](../operations/release_runbook_v0_5_0.md).

## Limits and future trigger

This supports a single host/volume and process-local Scheduler coordination.
Sustained lock timeouts, a need for Scheduler replicas, remote/network
filesystems without reliable SQLite locking, or write throughput beyond short
transactions are triggers to evaluate PostgreSQL rather than layering on file
or distributed locks.
