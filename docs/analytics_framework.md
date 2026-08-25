# Modular analytics/telemetry framework (Issue #205)

## Purpose

AFL-api's match-based scheduler now runs several production collectors
against upstream AFL/CFS endpoints, and `/api/v1` is the stable consumer
surface those collectors feed. Until this framework, the system could say
what was persisted, but not how the collection process itself behaved:
how often an endpoint was polled, whether the response actually changed,
how long meaningful changes took to arrive, or which `/api/v1` resources
are actually requested.

This framework adds a small, modular, historical/domain analytics layer
that answers those questions from real observations, without becoming a
generic observability platform. It measures **facts about the AFL-api
value chain**:

```text
upstream AFL/CFS polling
    -> collection behaviour
    -> persisted/normalised data
    -> consumer API exposure
    -> actual consumer use
```

Concrete questions it is designed to answer:

* How many times was a resource polled, and how often did it actually
  change?
* How does change frequency differ by match lifecycle state (LIVE vs
  POSTGAME vs CONCLUDED)?
* How many polls fail, time out, or find data not yet published?
* How much polling occurs per meaningful change (is the configured cadence
  well-matched to real update frequency)?
* Which `/api/v1` resources are actually being requested?

## What this is not

This framework is deliberately narrower than "collect everything." It is
**not**:

* **the diagnostics framework** (`diagnostics/`, see
  [`diagnostics_framework.md`](diagnostics_framework.md)) -- diagnostics is
  opt-in, bounded-investigation raw-evidence capture (e.g. "what does the
  live `matchInterchange` payload actually look like second-by-second").
  Analytics is small factual counters/timings/outcomes, always-on by
  default, answering aggregate historical questions, never archiving raw
  payloads.
* **normal application logging** (`utils.log`, per-module `*.log` files)
  -- logging is for operators reading text during/after an incident.
  Analytics is structured, queryable, aggregable data meant to answer a
  reporting question across many observations.
* **process/Prometheus-style metrics** -- analytics does not expose a
  `/metrics` endpoint, does not measure CPU/memory/GC, and requires no
  external time-series database. It answers AFL-domain questions
  (`match_commentary` polls per meaningful change during `LIVE`), not
  process-health questions.
* **full source payload archival** -- no analytics table ever stores a
  response body. Raw evidence retention remains the diagnostics
  framework's job where it is needed at all.
* **consumer-specific fantasy analytics** -- nothing here is specific to
  any particular downstream consumer or fantasy-scoring semantics. It
  measures AFL-api's own collection and API surface only.

These may all be *complementary* to analytics, but are implemented
separately and must stay that way -- see "Relationship to diagnostics and
logging" below.

## Architecture

```text
collector / API request
        |
        v  (one factual observation: resource id, timestamp, lifecycle,
        |   duration, outcome, changed?, small bounded change magnitude)
analytics.record.record_upstream_poll() / record_consumer_request()
        |
        v  (queue.put_nowait -- microseconds, never touches SQLite)
bounded in-memory queue (analytics.record._queue)
        |
        v  (single background daemon thread)
analytics.storage.insert_upstream_poll() / insert_consumer_request()
        |
        v
analytics_upstream_polls / analytics_consumer_requests  (bounded raw detail)
        |
        v  (analytics.rollup, daily scheduled job)
analytics_upstream_daily_rollups / analytics_consumer_daily_rollups  (kept indefinitely)
```

```text
analytics/
    contracts.py   common observation dataclasses + outcome vocabulary +
                   resource/route registries (metadata only, never a gate)
    record.py      the only module a collector/route needs to import:
                   record_upstream_poll(), record_consumer_request()
    storage.py     low-level SQLite insert/aggregate helpers
    rollup.py      retention + daily roll-up (run_rollup_and_retention)
    reporting.py   shared read-only reporter (CLI + optional Admin page)
    middleware.py  the single /api/v1 instrumentation point
```

This mirrors the diagnostics framework's successful shape -- a small common
contract, independently addable modules, no new scheduler infrastructure
per module -- without reusing diagnostics' code or tables (see "Why not
reuse the diagnostics framework directly" below).

## The observation contract

Two frozen dataclasses in `analytics/contracts.py` are the entire contract:

* `UpstreamPollObservation` -- one logical poll of an upstream resource:
  `resource`, `match_id`/`match_provider_id`, `observed_at` (UTC),
  `lifecycle_state`, `configured_interval_seconds`,
  `actual_interval_seconds` (computed by `analytics.record`, best-effort,
  in-process), `duration_ms`, `outcome`, `http_status`, `changed`,
  `change_magnitude`, `note`.
* `ConsumerRequestObservation` -- one `/api/v1` request: `route`,
  `observed_at`, `duration_ms`, `status_code`, `api_key_id`,
  `request_mode`.

A collector or route builds one of these from facts it already has (or can
compute cheaply) and calls `analytics.record.record_upstream_poll(...)` /
`record_consumer_request(...)`. That is the entire integration surface.

### Outcome vocabulary

`UpstreamOutcome`: `success`, `not_published`, `unavailable`, `auth_error`,
`transport_error`, `http_error`, `invalid_response`, `malformed_payload`,
`error`. This mirrors the outcome categories the production commentary and
interchange collectors already classify responses into
(`afl_json/match_commentary.py`, `afl_json/match_interchange.py`) rather
than inventing a parallel taxonomy. There is no separate `timeout`
category -- the existing `AflJsonClient` classifies a timeout as a
transport failure, and analytics follows that classification rather than
re-deriving it.

`changed`/`unchanged` are **not** separate outcomes. Both are recorded as
`outcome=success`, distinguished by the `changed` boolean, so "successful
poll rate" and "changed vs unchanged" are independently answerable from the
same rows -- exactly the

```text
100 successful polls
 72 unchanged
 28 meaningful changes
```

shape the issue asks for.

### Meaningful-change semantics are resource-specific

The framework does not define what "changed" means -- each collector does,
reusing whatever change-detection it already has rather than performing
new comparison work:

| Resource | `changed` source | `change_magnitude` meaning |
| --- | --- | --- |
| `cfs_player_stats` | `upsert_player_stats()`'s existing `WHERE ... OR (... meaningful_change)` upsert guard, which already skips a write when every compared column is identical (`afl_json/player_stats.py`) | number of players whose row was actually inserted/updated |
| `match_commentary` | the production collector's existing SHA-256 event-fingerprint dedup (`afl_json/match_commentary.py`) | number of newly observed (deduplicated) commentary events |
| `match_interchange` | the production collector's existing durable-state diff (`afl_json/match_interchange.py`) | number of appear/disappear/field-change transitions |

No new hashing, diffing, or comparison logic was written for analytics --
every `changed`/`change_magnitude` value is read off a result the collector
was already computing for its own persistence decision. This is also why
per-observation overhead is so low (see "Analytics overhead" below): the
expensive comparison work happens once, for production persistence, and
analytics reuses its result.

### Resource/route registries are metadata, not a gate

`analytics.contracts.register_resource()` / `register_route()` attach a
display name and (for upstream resources) a short description of what
`change_magnitude` counts, purely so reports are readable. An observation
for an unregistered identifier is still recorded -- forgetting to register
a new resource must never silently drop real data. Re-registering the same
identifier with different metadata raises, to catch a copy-paste collision
early.

## Instrumentation points

### Stage 2: match-scheduler collectors

The three principal production match-based collectors are instrumented,
identified by these stable resource identifiers:

* **`cfs_player_stats`** (`scheduler/player_stat_polling.py`) -- the
  lease-based CFS player-statistics poller. Instrumented at the end of
  `_persist_success` (using the `network_ms` and `result.status` already
  computed there, plus the row count `upsert_player_stats` already
  returns) and in each of the four failure branches of `poll_once`
  (auth/HTTP/transport-or-invalid/unexpected), each passing the correct
  `UpstreamOutcome` for the exception it caught.
* **`match_commentary`** (`scheduler/match_commentary_production.py`) --
  production `commentaryFeed` polling. Instrumented in `_capture_one`,
  timed from before the HTTP request to after `write_lane.execute(...)`
  returns, using the persistence result's own `outcome` and
  `new_event_count`.
* **`match_interchange`** (`scheduler/match_interchange_production.py`) --
  production `matchInterchange` polling. Instrumented the same way, using
  `len(appeared) + len(disappeared) + len(changed)` as the change
  magnitude.

Each collector reads `matches.status` at poll time via the same one-row
`SELECT status FROM matches WHERE match_id=?` read it already performs for
its own persistence logic (`_current_match_status`/`_current_canonical_status`)
-- lifecycle context costs nothing extra to capture.

`configured_interval_seconds` is each collector's own configured/derived
cadence (`AFL_COMMENTARY_PRODUCTION_INTERVAL_SECONDS`,
`AFL_INTERCHANGE_PRODUCTION_INTERVAL_SECONDS`, or the lease system's
computed `cadence` for player stats). `actual_interval_seconds` is computed
by `analytics.record` itself from an in-process `(resource, match_id,
match_provider_id) -> last observed_at` map -- best-effort, resets on
process restart, and intentionally not a second source of truth for
anything authoritative.

Deliberately **not** instrumented in Stage 1: fixtures/injuries/lineups/
match-status refresh jobs, and the diagnostics-only evidence-capture
collectors. These are lower-frequency, and diagnostics evidence capture is
already a separate, working investigation tool with its own reporting
(`scripts/report_*_evidence.py`) -- duplicating it into analytics would
blur the two frameworks' boundary. A future analytics module can add these
without new scheduler infrastructure (see "How to add a new analytics
module").

### Stage: consumer `/api/v1` telemetry

A single FastAPI middleware, `analytics.middleware.analytics_http_middleware`,
registered once in `main.py` via `app.middleware("http")(...)` -- not
per-route. It:

* only instruments requests under `/api/v1` (Admin, health, and the legacy
  unversioned `/api/*` routes are out of scope);
* records the **route template** Starlette resolved
  (`request.scope["route"].path`, e.g.
  `/api/v1/matches/{match_id}/player-stats`), never the literal URL --
  bounded cardinality, no path-parameter values leak into the identifier;
* records status code and duration for every request, including one that
  raised an unhandled exception (measured in a `finally` block around
  `call_next`, so a 5xx is captured even though the response object itself
  is unavailable);
* attaches `api_key_id` when the route authenticates: `auth.authenticate_api_key`
  now stashes the already-resolved, non-secret internal `api_keys.id` onto
  `request.state.api_key_id` (one extra line in an existing dependency,
  never a new auth path) for the middleware to read after the request
  completes;
* extracts `request_mode` from a **fixed allow-list** of already-existing
  bounded query flags (`REQUEST_MODE_NORMALIZERS` in
  `analytics/middleware.py`: `advanced`, `score_events_only`,
  `on_bench_only`, `event_type`) -- never arbitrary query-string content.
  Each flag is additionally **normalized**, not copied verbatim: the three
  boolean flags accept only the same string forms FastAPI/Pydantic itself
  accepts for a bool query parameter (`1`/`true`/`on`/`yes` -> `true`,
  `0`/`false`/`off`/`no` -> `false`); `event_type` accepts only its route's
  exact `Literal[...]` values. An unrecognized value for a listed
  parameter -- including one FastAPI would itself reject with 422 -- is
  simply omitted, never persisted as-supplied.

## Privacy rules

Consumer telemetry persists exactly: route template, UTC timestamp, status
code, duration, the internal `api_keys.id` integer, and a bounded
mode string built only from the fixed allow-list above.

It **never** persists: the API key secret, the `Authorization`/`X-API-Key`
header value, any other request header, client IP, request or response
bodies, or arbitrary query-string content. `tests/test_analytics_consumer_middleware.py::test_authenticated_request_records_internal_api_key_id_not_secret`
asserts this directly -- it sends a real API key and a real
`Authorization` header and then scans every column of the resulting row to
confirm neither value (nor the substring `"Bearer"`) appears anywhere in
it.

## Failure isolation

Both `record_upstream_poll` and `record_consumer_request`:

1. check their feature flag and return immediately if disabled;
2. otherwise build the observation and hand it to a bounded in-memory
   queue (`queue.Queue(maxsize=AFL_ANALYTICS_QUEUE_MAX_SIZE)`,
   `put_nowait`) -- this is the only work done on the calling
   collector/request thread, and it never touches SQLite;
3. **never raise**. A disabled flag, a full queue, or any exception while
   building/enqueueing the observation is caught, logged at `DEBUG`, and
   counted in `analytics.record.dropped_observation_count()`.

A single background daemon thread drains the queue and performs the actual
SQLite write, each write further wrapped in its own `try/except` -- a
storage failure (lock contention, disk error, a schema mismatch) is logged
and dropped, and never propagates anywhere a collector or `/api/v1` request
could observe it. The consumer middleware additionally wraps its own
post-request bookkeeping in `try/except`, so even a bug in the telemetry
code path itself cannot turn a valid response into an error --
`test_analytics_failure_never_breaks_a_valid_response` and
`test_analytics_write_failure_never_blocks_collector_persistence` assert
this for both sides directly.

Analytics never influences match finality, snapshot authority,
source-selection rules, or polling cadence -- it is purely observational,
exactly as Issue #205 requires. No adaptive polling is implemented; this is
explicitly deferred (see "Non-goals" in the PR description).

### Shutdown

`analytics.record.wait_until_idle(timeout)` blocks until the write queue
drains (or the timeout elapses) and is called as a best-effort drain on
process shutdown -- `scheduler/start.py`'s `shutdown_scheduler` (alongside
the existing `write_lane.drain(...)` call it already made) and `main.py`'s
new `lifespan` shutdown handler. Unlike the write lane's drain, a timed-out
analytics drain is only logged, never raised: analytics is observational
and must never turn an otherwise-clean shutdown into a failure. Anything
still queued past the timeout is simply lost, the same outcome as any other
dropped observation (disabled flag, full queue, storage error) -- shutdown
is not a special case, just another way an observation can fail to persist.

## Storage schema and retention

Four tables (migration `0023_analytics_foundation.py`):

* `analytics_upstream_polls` -- bounded raw detail, one row per poll.
* `analytics_consumer_requests` -- bounded raw detail, one row per
  `/api/v1` request.
* `analytics_upstream_daily_rollups` -- `UNIQUE(resource, lifecycle_state,
  observation_date)`, aggregated counts (`polls`, `successes`, `changed`,
  `unchanged`, `failures`, `total_duration_ms`).
* `analytics_consumer_daily_rollups` -- `UNIQUE(route, observation_date)`,
  same shape for consumer requests (`status_2xx`/`4xx`/`5xx` instead of
  changed/unchanged).

Every raw row carries a UTC `observation_date` (derived once, at write
time, from `observed_at`) purely so the roll-up job can group/purge by
date with a plain indexed scan.

`analytics.rollup.run_rollup_and_retention()` (registered as a single new
daily scheduled job, `analytics_rollup` in
`scheduler/scheduled_tasks.py`, 04:20 AWST) finds every distinct
`observation_date` in the raw tables older than
`AFL_ANALYTICS_RETENTION_DAYS` (default 14) days ago, aggregates it into
the matching rollup table (`INSERT ... ON CONFLICT DO UPDATE`, so re-running
is safe), and deletes the now-represented raw rows. This is the **one**
piece of genuinely new scheduler infrastructure Stage 1 adds -- it is
shared across every resource/route already present in the raw tables, so
adding a new analytics module never needs a second job.

`analytics_upstream_polls.lifecycle_state` is nullable (a collector may not
always have it cheaply available), but the rollup table's equivalent column
is `NOT NULL` -- the roll-up query already normalizes a missing value to
the literal string `'UNKNOWN'` when aggregating. `AnalyticsReporter`'s
raw-detail queries apply the identical `COALESCE(lifecycle_state,
'UNKNOWN')` normalization, so a report spanning the retention boundary
merges both representations into one `UNKNOWN` group instead of splitting
it into `NULL` (recent, raw) and `'UNKNOWN'` (older, rolled up) --
see `test_missing_lifecycle_state_is_normalized_consistently_across_retention_boundary`.

Match-level detail (`AnalyticsReporter.match_summary`) is **raw-only by
design**: the rollup tables intentionally do not retain `match_id` (that
would make the rollup table grow roughly as fast as the raw one, defeating
its purpose). A match's poll-by-poll detail is therefore only queryable
within the retention window; resource/route/lifecycle/time-period reports
work across all time by combining raw detail with rollups transparently
(`analytics.reporting.AnalyticsReporter.resource_summary`/`consumer_summary`,
via a `UNION ALL` over both).

## Analytics overhead (measured)

Issue #205 asks for evidence, not architectural speculation. Measured in
this environment (Python 3.11, SQLite WAL, the real migrated schema and
connection PRAGMAs from `db/connection.py`; no live match traffic was
available in this sandboxed session, so this is a same-schema
microbenchmark rather than a live-match capture -- see the note at the end
of this section):

| Measurement | Result |
| --- | --- |
| `record_upstream_poll()` call latency (the only cost on the calling collector/request thread), 2000 calls | mean 4.9µs, p50 4.2µs, p95 6.3µs, p99 18.8µs |
| Background single-row write (open connection + insert + close), 500 writes | mean 2.23ms, p50 2.17ms, p95 2.70ms, p99 3.08ms -- entirely off the collector/request's critical path |
| Steady-state storage cost per row (20,000-row table, WAL-checkpointed, including all 4 indexes) | ~370 bytes/row |

The calling-thread cost (single-digit microseconds) is 3-4 orders of
magnitude smaller than any of the network calls it instruments (tens to
hundreds of milliseconds per AFL/CFS request), so instrumentation is
"extremely cheap" as required. The background write cost is irrelevant to
collection latency by construction (it never runs on the collector's
thread), and is well within a single scheduler tick even under the busiest
production cadence (20s).

### Expected row volume (from real evidence + configured cadence)

Real Round 24 evidence already in this repository
(`docs/investigation/afl-json/ENDPOINT_CATALOG.md`) shows real per-match
poll counts and change-transition counts (e.g. matchInterchange membership
changed 442/435 times for one match's home side alone across a ~3 hour LIVE
window at a ~15s diagnostic cadence). Using the actual *production*
cadences each collector is configured with:

| Resource | Cadence | Approx. window | Approx. polls/match |
| --- | --- | --- | --- |
| `cfs_player_stats` | 60s LIVE / 300s pre-match / 120s post-match (config defaults) | ~2h LIVE + ~2h pre-match + ~30min post-match | ~190 |
| `match_commentary` | 20s | LIVE (~2.5h) + kickoff tolerance (10min) + POSTGAME grace (30min) | ~570 |
| `match_interchange` | 20s | LIVE (~2.5h) + kickoff tolerance (10min) + one final POSTGAME poll | ~480 |

-> **~1,250 upstream poll rows per match**, ~11,250 per round (9 matches),
~270,000 for a full ~216-match season *if retention were unbounded*.

With the default 14-day retention window (roughly 2 rounds' worth of
matches resident at once), the **raw** table's steady-state size is
~22,500 rows, or **~8MB** at the measured ~370 bytes/row. The daily rollup
tables grow by at most (3 resources x ~5 lifecycle-state buckets) rows per
day -- a full season adds a few thousand rollup rows, i.e. well under 1MB
for the entire season's aggregated history.

Consumer `/api/v1` request volume has no equivalent real-evidence basis
yet (no production traffic data was available in this session) -- this is
the one figure in this document that is an assumption, not a measurement.
Even a generously estimated few requests per second sustained would still
be materially smaller than the upstream volume above, and the same 14-day
raw / indefinite-rollup retention model applies identically. Confirming
this with real traffic once available is a natural first follow-up (see
the PR description's recommended follow-up issues).

### Synchronous writes are unnecessary and were not used

Given the measured background-write cost (~2.2ms) and expected volume
(under one row per second on average, bursting to a few per second during
LIVE play), a synchronous write on the calling thread would already have
been comfortably affordable. The queue+background-thread design was chosen
anyway, because it makes the "instrumentation is extremely cheap" and
"analytics failure never blocks collection" requirements true *by
construction* rather than by current-volume luck -- the calling thread
never touches SQLite at all, so no future increase in write contention (a
busy `write_lane`, a slow disk) can ever leak into collector or request
latency. No batching/buffering beyond the existing single-item queue was
added, since the measured per-write cost does not justify it.

## Reporting

`analytics.reporting.AnalyticsReporter` is a shared, side-effect-free
reporter class (mirroring `afl_json.season_report.SeasonCompletenessReporter`,
which already backs both `cli.py --report-afl-season` and Admin's Season
Review page) used by both surfaces below, so neither duplicates the SQL:

* **CLI**: `python -m scripts.report_analytics [--since DATE] [--until DATE]
  [--resource NAME] [--lifecycle-state STATE] [--by-lifecycle]
  [--match-id ID] [--json]` -- prints a table shaped like:

  ```text
  Resource                Lifecycle       Polls  Changes  Unchanged  Errors  Polls/change   Avg ms
  CFS player statistics   ALL              1240      418        812      10          2.97      312.4
  CFS match commentary    ALL               620      291        321       8          2.13       94.1
  CFS match interchange   ALL               620       93        520       7          6.67       88.6
  ```

* **Admin**: a small read-only page at `/analytics` (added alongside the
  existing Season Review page, same nav group, same
  `get_read_only_db_connection()` pattern, no new auth wiring needed since
  Admin's `verify_admin` dependency already covers every route on that
  app) with `since`/`until`/`by-lifecycle` filters.

## Collection-to-consumer mapping

See [`architecture/collection_to_consumer_map.md`](architecture/collection_to_consumer_map.md)
for the maintained, explicit mapping between upstream datasets, their
persistence, their `/api/v1` exposure, and their current usage
classification (collected+exposed+consumed vs. collected+exposed+presently
unused vs. diagnostic/research-only, etc.). This is documentation, not
runtime state -- Issue #205 explicitly asks that architectural
"consumed/unused" truth not be inferred purely from request counts, and a
maintained document is the more honest artifact for that judgement than an
automatically-computed table.

## Relationship to diagnostics and logging

| | Diagnostics (`diagnostics/`) | Analytics (`analytics/`) | Logging (`utils.log`) |
| --- | --- | --- | --- |
| Purpose | Bounded investigation of uncertain upstream behaviour | Ongoing historical/domain measurement | Human-readable operational trace |
| Default state | Off (`AFL_DIAGNOSTICS_ENABLED=false`) | On (`AFL_ANALYTICS_ENABLED=true`) | Always on |
| Retains | Selective raw payloads on notable rows | Small counters/timings/outcomes only, never payloads | Free-text lines, rotated |
| Read by | A human, via its own report scripts | Reports, aggregated | A human, via `tail`/Admin logs page |
| Ever authoritative for collection/API behaviour? | No -- never | No -- never | No -- never |
| Table shape | One bespoke table per profile (deliberately not generalised -- see `diagnostics_framework.md` "Persistence") | One shared table per observation kind, reused by every resource | N/A (files) |

### Why not reuse the diagnostics framework directly

The diagnostics framework's own documentation explains why it *deliberately
does not* generalise its persistence across profiles: with few profiles,
each shaped differently (a per-poll snapshot vs. an accumulated
deduplicated event stream), a generic schema was "pure abstraction cost."
Analytics is the opposite situation from day one: three collectors
producing **structurally identical** facts (a resource id, a match, a
timestamp, a lifecycle state, a duration, an outcome, a changed flag, a
small bounded magnitude) -- exactly the "three or more profiles with
materially the same shape" trigger that document names as the point where
a shared schema stops being premature. A shared `analytics_upstream_polls`
table is therefore the right call *for analytics*, without implying
diagnostics should also be generalised -- the two frameworks solve
differently-shaped problems and are expected to keep making different
architectural choices.

Diagnostics' proven patterns *were* reused conceptually, not by import:
gate-then-recheck config flags (`is_profile_selected` -> the same shape as
`AFL_ANALYTICS_ENABLED`/`AFL_ANALYTICS_CONSUMER_ENABLED`), independent
failure isolation per unit of work, and "the framework never becomes
production source authority."

## How to add a new analytics module

Adding a new upstream resource:

1. Pick a stable, human-readable identifier (snake_case, matching the
   collector's domain -- e.g. `cfs_match_rosters`).
2. Register it in `analytics/contracts.py`:
   `register_resource("cfs_match_rosters", display_name="...",
   change_magnitude_label="...", description="...")`.
3. In the collector, after persistence completes (success or failure),
   call `analytics.record.record_upstream_poll(...)` with the fields you
   already have. Reuse whatever change-detection the collector already
   performs for `changed`/`change_magnitude` -- do not add new comparison
   logic purely for analytics.
4. That's it. No new migration, no new scheduled job, no new report code
   -- the existing table, rollup job, reporter, and CLI/Admin report all
   pick up the new resource automatically because they group by whatever
   `resource` values are actually present.

Adding a new consumer route: nothing to do -- every `/api/v1` route is
already covered by the single middleware. Optionally call
`register_route(...)` in `analytics/contracts.py` for a friendlier display
name in reports.

## Testing

See `tests/test_analytics_record.py`, `test_analytics_rollup.py`,
`test_analytics_reporting.py`, `test_analytics_consumer_middleware.py`, and
`test_analytics_scheduler_integration.py` for the full coverage list
(enabled/disabled behaviour, changed vs unchanged, every failure outcome,
repeated polls' interval bookkeeping, lifecycle context, modular
registration and its collision detection, write-failure isolation on both
the collection and consumer-request paths, no-secrets-persisted, and
retention/roll-up idempotency). The full existing suite
(`tests/test_match_commentary_production*.py`,
`test_match_interchange_production*.py`, `test_player_stat_polling.py`, and
every `/api/v1` test) continues to pass unmodified, demonstrating the
instrumentation did not change existing collector or API behaviour.

## Possible future extensions and non-goals

Evaluated against "could this framework reasonably support it later,"
classified as a **natural extension** (fits this architecture directly), a
**possible but separate concern** (plausible, but deserves its own
evidence-backed issue and design, not a silent addition here), or **not
appropriate** (would turn AFL-api into a generic observability platform):

| Idea | Classification |
| --- | --- |
| Automatic polling-cadence recommendations from observed change frequency | Possible but separate concern -- Issue #205 explicitly defers adaptive polling; this framework's data is the evidence a future issue would need, not a mandate to build it now |
| Scheduler efficiency trends over a season | Natural extension -- same tables, a report over a wider date range |
| Provider (AFL/CFS) reliability comparison over time | Natural extension -- already answerable from `outcome`/`http_status` distributions |
| Detection of abnormal provider update behaviour (e.g. a resource suddenly stops changing) | Possible but separate concern -- needs a defined "abnormal" threshold and alerting path, which is new scope beyond passive reporting |
| Latency between upstream change and consumer availability | Possible but separate concern -- would need to correlate an `analytics_upstream_polls` change with a specific later `analytics_consumer_requests` row for the same resource, a new join/semantic this framework does not attempt |
| Reconciliation/change behaviour after POSTGAME | Natural extension -- `lifecycle_state` is already a first-class grouping dimension |
| Identifying persistently unused exposed API resources | Natural extension -- directly what `consumer_summary()`/the collection-to-consumer map are for |
| Identifying expensive collection with little demonstrated downstream use | Natural extension -- comparing `resource_summary()` poll volume against the mapped route's `consumer_summary()` request volume |
| Seasonal/round/match collection-volume reports | Natural extension -- `match_summary()` plus date-range `resource_summary()` |
| Performance regression evidence (collector duration trending up) | Natural extension -- `duration_ms`/`avg_duration_ms` already tracked per rollup |
| Admin analytics dashboards beyond the current read-only table view | Possible but separate concern -- a richer dashboard is real UI work Issue #205 explicitly says not to make a prerequisite |
| Optional Prometheus/OpenTelemetry export of aggregate operational metrics | Possible but separate concern, and only ever *optional* -- Issue #205's non-goals explicitly rule out requiring either as a prerequisite for AFL-api; an export adapter reading the existing rollup tables would not violate that if it stayed genuinely optional |
| Full request/response payload capture "just in case" | Not appropriate -- directly contradicts the "question-driven, not blanket capture" design principle and the diagnostics/analytics boundary |
| Personally-identifying consumer tracking, third-party web analytics | Not appropriate -- explicitly ruled out by Issue #205 and by the privacy rules above |
| A general-purpose event bus / pluggable middleware framework for arbitrary future telemetry | Not appropriate -- exactly the "abstraction so generic it becomes difficult to understand or maintain" Issue #205 warns against; two purpose-built contracts (`UpstreamPollObservation`, `ConsumerRequestObservation`) are enough for the value chain this issue defines |
