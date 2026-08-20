# Scheduler health and diagnostics endpoint

Issue #178 adds a stable, read-only scheduler health/status contract so
operational consumers — especially the Admin interface — can tell *why* a
scheduler request failed instead of inferring health from `/scheduler/jobs`.
Before this change the admin UI could only distinguish "jobs request
succeeded" from "jobs request failed"; it could not distinguish an empty but
healthy scheduler from a starting, unreachable, or dependency-failing one.

## Route

```
GET /scheduler/health
```

Served by the scheduler process alongside the other endpoints in
`scheduler/api.py` (for example `http://afl-scheduler:8000/scheduler/health`,
matching the existing `/scheduler/jobs`, `/scheduler/match-windows`, and
`/scheduler/player-stat-polling` convention). It takes no parameters, performs
no writes, and triggers no job execution.

This is deliberately separate from the generic `/healthz` and `/readyz`
endpoints in `health.py` (also mounted on the scheduler process). Those report
whether the scheduler *HTTP process* is up and can reach its database; this
endpoint reports the health of the *scheduler domain* specifically — APScheduler
runtime state and the persisted job registry it depends on.

## Response shape

```json
{
  "state": "healthy",
  "scheduler_running": true,
  "database_accessible": true,
  "registry_accessible": true,
  "job_count": 0,
  "diagnostics": [],
  "version": "0.6.0"
}
```

| Field | Meaning |
|---|---|
| `state` | One of `healthy`, `starting`, `unhealthy`. See below. |
| `scheduler_running` | Whether APScheduler has been started (`scheduler.running`, i.e. not `STATE_STOPPED`). |
| `database_accessible` | Whether a read-only connection to the application database succeeded and executed a trivial query. |
| `registry_accessible` | Whether the persisted `scheduler_job_registry` table could be read. |
| `job_count` | Current in-memory APScheduler job count. Informational only — see "Zero jobs is healthy" below. |
| `diagnostics` | A list of stable, sanitized diagnostic codes (never raw exception text). Empty when nothing is wrong. |
| `version` | The application version (`version.__version__`), matching the convention already used by `api/routes_v1.py`'s discovery response. |

HTTP status is `200` for `healthy` and `starting`, and `503` for `unhealthy`
(mirroring the existing `readyz` convention in `health.py`). Consumers should
primarily branch on the `state` field in the body, not the HTTP status code,
since a `200`/`starting` response is not itself an error condition.

### Diagnostic codes

| Code | Meaning |
|---|---|
| `database_unavailable` | The application database could not be opened or queried. |
| `registry_unreadable` | The persisted `scheduler_job_registry` table could not be read (for example, migrations have not been applied). |
| `scheduler_not_running` | APScheduler has not been started yet (present only in the `starting` state). |

Diagnostic codes are a fixed, hardcoded vocabulary — never string-interpolated
from an exception. This makes it structurally impossible for this endpoint to
leak a stack trace, a database path, a connection string, or any other raw
runtime detail.

## State model and rationale

* **`healthy`** — the scheduler is running (APScheduler has been started) and
  both required dependencies (database, job registry) are accessible.
  `job_count` may be zero. This is intentional: a fresh deployment, or one
  where all scheduled work has been drained, is not unhealthy.
* **`starting`** — APScheduler has not been started yet. This is a real,
  meaningfully detectable window: `scheduler/start.py` runs
  `start_scheduler_for_app()` during the FastAPI lifespan, which registers
  jobs synchronously but starts APScheduler itself (`scheduler.start()`) on a
  background thread. There is a brief interval where the HTTP server is
  already accepting requests but `scheduler.running` is still `False`.
* **`unhealthy`** — a *required* dependency could not be reached: the
  application database, or the persisted job registry table it hosts.
  Dependency failure is reported even during what would otherwise look like
  the "starting" window, because a broken database is not a transient
  startup condition.

### Why zero jobs is not unhealthy

`job_count == 0` never changes `state`. A scheduler that is running, whose
database and registry are both reachable, is `healthy` whether it has 500
registered jobs or none. This mirrors the existing registry documentation
(`docs/scheduler_registry.md`), which already treats an empty schedule as
ordinary rather than degraded, and matches the acceptance criteria for Issue
#178: consumers must be able to tell "empty but healthy" apart from
"unavailable" or "unhealthy" without guessing from job counts.

### Why the database and registry are the only required dependencies

Every other scheduler endpoint (`/scheduler/jobs`, `/scheduler/refresh`, the
manual-trigger endpoints, match-window inspection) reads or writes through
this same database and the same registry table. They are load-bearing for the
entire scheduler HTTP surface, so their unavailability is treated as a real
health failure. By contrast:

* Match-window state, CFS player-stat polling state, and match-state-evidence
  capture are optional, feature-flagged, or purely diagnostic subsystems (see
  `scheduler/match_windows.py`, `scheduler/player_stat_polling.py`,
  `scheduler/match_state_capture.py`). None of them gate scheduler readiness;
  their absence or misconfiguration is not surfaced by this endpoint.
* Job execution semantics are untouched by this endpoint. It performs a
  read-only `SELECT 1` against the database and a read-only registry query
  (the same query `/scheduler/jobs` already performs); it does not trigger,
  pause, or modify any job.

## Admin interface integration

The Admin Schedule page (`GET /schedule`, `templates/schedule_grouped.html`)
renders a "Scheduler health" card independently of the jobs table below it,
sourced from `admin._fetch_scheduler_health()` /
`admin._scheduler_health_display()` in `admin.py`. It never inspects scheduler
internals directly — only the fields in this contract — and maps them to one
of:

* **Healthy** — `state == "healthy"` and `job_count > 0`.
* **Healthy — no jobs registered** — `state == "healthy"` and `job_count == 0`.
* **Starting / Not ready** — `state == "starting"`.
* **Unhealthy** — `state == "unhealthy"`, with the sanitized diagnostic codes
  shown alongside.
* **Unavailable** — the health endpoint could not be reached at all (network
  error, timeout) *or* returned a body this contract does not recognise (for
  example, missing the `state` field). Both cases are normalised to the same
  display state so a transport failure is never mistaken for, or handled
  differently from, a malformed response.

This is deliberately independent from the existing jobs-list fetch on the same
page: a failure fetching `/scheduler/jobs` no longer needs to be the only
signal of scheduler trouble, and a healthy-but-empty scheduler no longer looks
identical to an unreachable one.

## Non-goals

This endpoint does not change job execution behaviour, scheduler
configuration, or the job registry schema. It does not make any currently
optional job or subsystem a health requirement. It does not expose job
payloads, credentials, connection strings, filesystem paths, or raw exception
text.
