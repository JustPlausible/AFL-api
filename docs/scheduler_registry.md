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
* `match_state_evidence_capture` for the opt-in diagnostic live matchItem evidence-capture interval job (Issue #148); only registered when `AFL_CAPTURE_MATCH_STATE_EVIDENCE=true`. See `collection/match_state_evidence.py` and `scripts/report_match_state_evidence.py`. Issue #148's investigative findings (confirmed `score.matchClock.periods` payload shape and observationally supported ordinary-match period/break mapping) are summarised on that issue; any production polling/normalisation decision based on this evidence is scoped separately in Issue #187 and is not implemented by this diagnostic job.

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

## Timestamp and match-day policy

Scheduler planning treats AFL/public metadata timestamps as instants. ISO-8601
values ending in `Z` or carrying an explicit positive or negative offset are
accepted, and their offsets are preserved while the instant is converted to
UTC. Missing, blank, malformed, and offset-free (naive) values are rejected;
the scheduler never guesses UTC or the host timezone for ambiguous source data.

Rejected match timestamps produce a durable registry row with `failed` status,
no attempt increment, the safe match or round identifier, and one of the stable
summaries `planning_failed:timestamp_missing`,
`planning_failed:timestamp_malformed`, or `planning_failed:timestamp_naive`.
These rows are visible through `/scheduler/jobs`, the Admin Schedule page, and
normal registry queries. Raw source values are not copied into the diagnostic.

The AFL match-day calendar is controlled by the `AFL_MATCH_DAY_TIMEZONE`
environment setting, which defaults to the IANA zone `Australia/Perth`.
Application code converts local midnight boundaries to aware UTC instants and
compares parsed timestamps; it does not use SQLite `localtime` or the host
timezone. This setting defines day membership and the civil times derived from
fixtures. It is separate from APScheduler's trigger timezone, which remains
`Australia/Perth` for the current cron and interval scheduler configuration.
Date-triggered jobs carry aware instants, so their execution time is unaffected
by the display/trigger timezone.

### Observed CFS timestamp forms

The checked-in CFS captures were inspected before finalising Issue #130. The
representative `matchItem` capture and the full/minimal round-roster captures
contain 11 `utcStartTime` occurrences, all in a naive form such as
`2026-07-23T09:30:00`. They contain no `utcStartTime` value ending in `Z` and no
`utcStartTime` value with an explicit positive or negative offset. In the same
CFS match objects, the paired `date` field uses an explicit zero offset such as
`2026-07-23T09:30:00.000+0000`, while `venueLocalStartTime` is naive. Other CFS
fields such as `lastUpdated` also use the explicit `+0000` form.

For all 11 inspected match occurrences, aware `date` and naive
`utcStartTime` have identical clock components and resolve to the same instant
under the verified CFS `utcStartTime` semantics. Each `venueLocalStartTime`
also resolves to that instant when combined with the independently captured
venue IANA timezone. This consistency is evidence, not permission for the
scheduler to infer a timezone from the venue-local value.

The observed CFS payload is therefore a **mixture by field**: its `date` is the
only self-contained scheduled-start instant, `utcStartTime` is naive, and
`venueLocalStartTime` is a naive civil time. This is evidence only: no CFS field
is wired into scheduler planning, no CFS-specific parser or fallback is added,
and the generic parser continues to reject naive values. Public AFL JSON test
fixtures are a separate source contract and currently spell `utcStartTime`
with `Z`.

The APScheduler trigger timezone is not read from any CFS field. It remains
application configuration hard-coded as `local_tz =
pytz.timezone("Australia/Perth")` in `scheduler/scheduled_tasks.py`, and that
value is passed to `BlockingScheduler(timezone=local_tz)`.

### Scheduler fixture-time source trace

Current scheduler registration does **not** parse a CFS timestamp field. Its
fixture instant comes from `matches.start_time_utc`. The canonical AFL JSON
bootstrap populates that column from public AFL JSON `utcStartTime`; the public
JSON `startTime` is retained in the normalised/source record but is not selected
for scheduler planning. CFS match-roster and match-item collection does not
populate `matches.start_time_utc`, so CFS `date`, `utcStartTime`, and
`venueLocalStartTime` do not currently pass through the strict scheduler parser.
The legacy HTML fixture importer can also populate the column with its own
aware UTC result, independently of all five JSON fields.

The CFS fixture test parses only aware `date` to verify the captured instant and
match day. It does not establish new source authority or production behaviour.
If a future issue introduces a CFS-backed scheduling path, that work must define
the source contract, validate `utcStartTime`, persist a stable conflict reason,
and establish venue-timezone authority before interpreting
`venueLocalStartTime`.

## Operator checks

Use `/scheduler/jobs` or the admin Schedule page to compare `apscheduler_state` with `persisted_status`. `apscheduler_state` describes the current in-memory APScheduler view (`scheduled`, `paused`, or `absent`). `persisted_status` describes the durable application registry.

For a missed or failed match-window job, search for the stable job ID (for example `stats_match_8216` or `lineups_match_8216`), compare its scheduled run time with APScheduler state, review `attempt_count`, `last_attempt_time`, `last_success_time`, and the concise `last_error_summary`, then decide whether a manual rerun is safe. Do not infer detailed scrape/import outcomes from this registry; those belong to Issue #26.


## Match-window planner compatibility

Player-stat scheduling is no longer described solely as a single `stats_match_<id>` job. Durable polling-series state lives in `match_stat_windows`; future individual attempts use distinct `mw_attempt_<window_id>_<lease_generation>_<attempt_number>` scheduler job IDs, while existing `stats_match_<id>` one-shot rows remain compatibility records and are not blindly replayed. See [durable match-window planner](architecture/match_window_planner.md).
# Interrupted polling recovery

Dynamic CFS polling rows include window, attempt, scrape-run, lease and
Scheduler-instance correlations. `interrupted` is a terminal historical status,
not a retry queue. Recovery reason, run/time, persistence evidence, and an
optional superseding attempt are machine-readable. Operators must not change an
interrupted row back to `pending`.

Inspect a correlation without relying on error text:

```sql
SELECT job_id, window_id, attempt_id, scrape_run_id, lease_generation,
       status, recovery_reason, attempt_persistence_evidence,
       match_authoritative_evidence,
       superseded_by_attempt_id
FROM scheduler_job_registry
WHERE window_id = ?
ORDER BY created_at;
```

Run a report safely while Scheduler is active:

```bash
python -m scheduler.recovery --dry-run --match-id 12345
python -m scheduler.recovery --dry-run --attempt-id mw_cfs_stats_12345_cfs_match_stats_v1_attempt_2_2
```

Mutation mode uses the same SQLite immediate write lane and optimistic lease
checks, but the supported deployment has one active Scheduler. Stop or drain
that Scheduler before a manual mutation run, then use one conservative bound:

```bash
python -m scheduler.recovery --window-id mw_cfs_stats_12345_cfs_match_stats_v1
python -m scheduler.recovery --since 2026-08-06T00:00:00+00:00
```

The JSON report lists thresholds, scope, inspected items, repairs, completions,
replans, superseded and unresolved attempts, redacted per-item failures, and
duration. Repeating it against unchanged state is a no-op. Investigate
`outcome_unresolved` by joining the IDs above to `scrape_runs` and inspecting
current CFS finality; do not infer success or rollback from age or error text.
