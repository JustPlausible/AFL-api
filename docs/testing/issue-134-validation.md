# Issue #134 Validation Report

**Issue:** #134 – Recover interrupted scheduler and scrape-run attempts safely

**Pull Request:** #141

**Validated Commit:** `bf61f4f`

**Validation Date:** 2026-08-06

**Tester:** Steve

---

# Objective

Validate the implementation of Issue #134 against a production-like environment before merge.

The primary goals were to verify:

- schema migration correctness;
- scheduler startup behaviour;
- runtime identity and heartbeat management;
- interrupted-attempt recovery infrastructure;
- recovery CLI behaviour;
- database integrity;
- graceful and unclean restart behaviour;
- recovery idempotency;
- Docker deployment using the production compose configuration.

---

# Test Environment

## Source

```
/opt/projects/afl-api
```

Validated commit:

```
bf61f4f
```

---

## Docker Compose

Compose project:

```
/opt/docker/compose/afl-api
```

Testing used:

- `compose.yaml`
- `compose.issue134-test.yaml`
- `.env.issue134-test`

The compose override ensured the scheduler used an isolated SQLite database rather than the normal application database.

---

## Test Database

Source database:

```
/opt/docker/appdata/afl-api/data/afl_players.db
```

Test database:

```
/opt/docker/appdata/afl-api/data/tmp/afl_players_issue134_test.db
```

The test database was created using SQLite `.backup`.

No testing was performed against the production database.

---

# Automated Testing

Executed from the source checkout:

```
pytest -q
```

Result:

```
646 passed in 23.35 seconds
```

---

# Docker Build Validation

Container image rebuilt using:

```bash
docker compose build --no-cache
```

The scheduler image was rebuilt successfully from the validated source checkout.

---

# Migration Validation

Starting database migration:

```
0012
```

The recovery CLI automatically applied any outstanding database migrations before executing reconciliation.

Verified migrations:

- 0013
- 0014

Migration was performed against a copied production-style database created from migration 0012.

Verified new schema included:

- scheduler_runtime_instances
- match_stat_windows
- expanded scheduler_job_registry
- expanded scrape_runs

---

# Database Integrity

Verified after migration:

```
PRAGMA quick_check;
```

Result:

```
ok
```

Verified:

```
PRAGMA foreign_key_check;
```

Result:

```
(no rows)
```

No integrity problems were detected.

---

## Recovery CLI Validation

The recovery CLI was executed immediately after migration using:

```bash
python -m scheduler.recovery --dry-run
```

### Result

| Field | Value |
|-------|------:|
| Trigger source | `manual` |
| Dry run | `true` |
| Windows inspected | **0** |
| Attempts inspected | **0** |
| Registry repairs | **0** |
| Scrape-run repairs | **0** |
| Stale leases found | **0** |
| Compatibility records | **0** |
| Unresolved cases | **0** |
| Startup candidates truncated | **false** |
| Duration | **25 ms** |

### Interpretation

The copied database had already been successfully migrated but no scheduler startup had yet occurred. No eligible match windows existed because the Scheduler planner had not yet populated the newly created match_stat_windows table. This represents the expected pre-startup state.

The recovery subsystem correctly:

- completed successfully;
- produced no unexpected compatibility findings;
- required no repair actions;
- reported no stale leases;
- performed no database mutations during dry-run.

This established a clean baseline prior to startup testing.

---

## Scheduler Startup Validation

The Scheduler was started from the production Docker image with polling disabled.

Observed startup order:

1. Scheduler process initialised.
2. Interrupted-attempt reconciliation executed.
3. Recovery completed successfully.
4. Dynamic scrape jobs registered.
5. Scheduler entered normal operation.

Representative startup log:

```text
INFO: Started server process [1]
INFO: Waiting for application startup.

[15:33:33] INFO: Interrupted-attempt reconciliation summary:
    trigger_source = startup
    dry_run = False
    inspected_windows = 0
    inspected_attempts = 0
    compatibility_records = 0
    startup_candidates_truncated = False
    duration_ms = 27

[15:33:33] INFO: 🧠 Registering dynamic scrape jobs...
```

The recovery phase completed successfully before scheduler job registration and no recovery errors or integrity failures were reported.

Validation observation: Recovery completed before dynamic scheduler job registration, confirming that interrupted-attempt reconciliation occurs before APScheduler begins accepting new scheduled work. This ordering matches the intended startup design introduced by Issue #134.

---

# Runtime Identity

Verified:

- runtime instance creation
- runtime heartbeat updates
- runtime persistence
- unique runtime instance IDs

Heartbeat advanced correctly while the scheduler was running.

---

# Graceful Shutdown

Verified:

```
docker compose stop
```

Confirmed:

- runtime recorded graceful shutdown;
- stopped_at populated;
- shutdown_kind recorded correctly.

Restart completed normally.

No stale recovery occurred after graceful shutdown.

---

# Unclean Shutdown

Verified using:

```
docker compose kill -s KILL
```

Confirmed:

- previous runtime detected as unclean;
- new runtime created;
- scheduler restarted normally;
- heartbeat resumed.

---

# Recovery State

With polling disabled:

Verified no remaining:

- leased windows
- running scheduler registry entries
- running scrape runs

Recovery correctly reported no outstanding interrupted attempts.

---

# Idempotency

Repeated execution of:

```bash
python -m scheduler.recovery --dry-run
```

produced:

- identical logical database contents;
- identical recovery decisions;
- no additional mutations.

---

# Production Configuration Validation

Testing was performed against:

- production Dockerfile;
- production compose configuration;
- production Scheduler service;
- copied production-style SQLite database.

This verified behaviour in an environment representative of deployment.

---

# Observations

During testing an issue was identified with Docker Compose environment handling.

`docker compose --env-file` does **not** replace service-level `env_file:` declarations.

A compose override file (`compose.issue134-test.yaml`) was introduced to ensure the scheduler used the isolated Issue #134 test database.

This was a testing configuration issue only and did not affect the implementation of Issue #134.

---

# Validation Artifacts

The following artifacts were retained during local validation but are not committed to the repository:

- issue134-first-dry-run.json
- issue134-after-startup-dry-run.json
- issue134-final-dry-run.json
- issue134-scheduler-startup.log
- issue134-graceful-restart.log
- issue134-unclean-restart.log
- issue134-final-runtime-history.txt
- issue134-final-window-state.txt
- issue134-final-migrations.txt

These files were generated from a copied production-style SQLite database and contain environment-specific runtime information. They were used to support the validation described in this document but are intentionally excluded from version control.

---

# Limitations

The following scenarios were **not** exercised during this validation:

- recovery of an intentionally interrupted live polling attempt;
- synthetic interrupted-attempt reconstruction;
- recovery during an active CFS collection;
- multi-worker scheduler scenarios.

These remain suitable candidates for future integration testing.

---

# Conclusion

Issue #134 successfully completed validation.

The following functionality was verified:

- successful schema migration;
- recovery CLI;
- scheduler startup sequence;
- runtime identity management;
- heartbeat persistence;
- graceful shutdown;
- unclean restart handling;
- database integrity;
- dry-run idempotency;
- Docker deployment using the production image.

No defects requiring changes to the implementation were identified during local validation.

This validation provides confidence that Issue #134 correctly introduces durable recovery infrastructure while preserving normal scheduler behaviour. No unexpected recovery actions, schema integrity issues, or restart regressions were observed during production-like local testing.

The implementation was considered suitable for merge.
