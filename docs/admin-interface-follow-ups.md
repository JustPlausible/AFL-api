# Admin interface follow-up recommendations

These recommendations were identified during the admin interface review but are intentionally outside the scope of the interface-only change.

## Make operational logs discoverable

**Resolved by Issue #179.** `logging_sources.py` is now the authoritative registry of known operational log sources (id, description, enabled/disabled state, resolved container path, existence, size, last-modified time). The Admin Logs & diagnostics page renders each source's status from it instead of a hard-coded filename mapping.

1. **Current admin limitation:** The log viewer relies on a fixed list of relative file names. It cannot distinguish a collector that has never run from one that logs elsewhere, has had its log rotated, or is configured not to write a file.
2. **Underlying change:** Give the logging subsystem a supported way to report available log streams, resolved paths, and basic metadata such as last update time. The admin interface could consume that information instead of duplicating configuration.
3. **Why useful:** Operators would know whether an absent file is expected, misconfigured, or stale without inspecting the deployment filesystem.
4. **Likely component:** Logging configuration/runtime and deployment configuration; an admin-facing adapter could consume the result later.
5. **Suggested issue title:** `Expose configured operational log sources and status`
6. **Issue detail:** Define a read-only representation of configured operational log sources, including display name, resolved location, availability, and last-modified time. Account for container paths and rotation. Do not make log-file availability a service health requirement. Update the admin viewer in a later interface change to consume it.

## Expose scheduler service diagnostics

**Resolved by Issue #178.** `GET /scheduler/health` now reports readiness, APScheduler running state, and database/registry accessibility as a stable, sanitized contract, and the admin Schedule page renders it independently of the jobs list. See [scheduler health and diagnostics](scheduler_health.md).

1. **Current admin limitation:** When the scheduler jobs request fails, the admin can only report that the service was unreachable; it cannot show whether the cause is startup, health, configuration, or network connectivity.
2. **Underlying change:** Provide a stable scheduler health/status contract with service state and a safe diagnostic summary.
3. **Why useful:** Operators could distinguish an empty schedule from an unavailable or unhealthy scheduler and choose the correct recovery action.
4. **Likely component:** Scheduler HTTP API and scheduler runtime health reporting.
5. **Suggested issue title:** `Add a stable scheduler health and diagnostics endpoint`
6. **Issue detail:** Add a read-only endpoint that reports readiness, scheduler running state, registry/database connectivity, and a sanitized last startup or runtime error. Keep job execution logic unchanged. A future admin change could display this alongside the schedule.
