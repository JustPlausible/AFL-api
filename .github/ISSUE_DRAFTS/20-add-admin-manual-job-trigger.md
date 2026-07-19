# Add manual scheduler job trigger controls to the admin UI

## Background / problem statement

When fixture data changes, a scrape fails, or an operator needs to refresh a single round/match, the current workflow requires shell access and knowledge of the relevant CLI command. The admin portal already exposes scheduler visibility, so the next incremental improvement is controlled manual triggering for common scrape jobs.

## Scope

Add authenticated admin controls to trigger selected scheduler/scraper jobs from the web UI.

## Implementation requirements

- Add manual trigger actions for the following narrowly scoped operations:
  - Refresh injuries.
  - Refresh fixtures for a round.
  - Refresh lineups for a round or match where supported by existing code.
  - Refresh player stats for a match with an explicit `once` behavior.
- Reuse existing scraper entry points rather than duplicating scraping logic in route handlers.
- Protect all trigger forms with existing admin authentication and CSRF mechanisms.
- Validate user-provided round and match identifiers before launching work.
- Record trigger source as `admin_manual` in scheduler/audit logging where available.
- Return a clear success/failure flash message and do not block the request longer than necessary for long-running scrapes.

## Out of scope

- Adding arbitrary command execution from the admin UI.
- Allowing unauthenticated or API-key-only users to trigger jobs.
- Implementing multi-user RBAC.
- Creating a general background queue beyond the minimal mechanism needed to invoke existing jobs safely.

## Acceptance criteria

- Admin users can trigger each supported scrape type from the schedule/admin area.
- Invalid identifiers produce validation errors and do not start jobs.
- CSRF protection is enforced on every manual trigger form.
- Triggered jobs are visible in existing scheduler/logging/audit views or tables.
- Long-running triggers do not cause the admin server to hang indefinitely.

## Testing requirements

- Route tests for authenticated success paths and unauthenticated rejection.
- CSRF regression tests for each new POST action.
- Validation tests for missing, non-numeric, and unknown identifiers.
- Tests that the route invokes the expected existing scraper/scheduler function with the correct arguments.

## Documentation updates required

- Update admin documentation with the available manual triggers and expected operator workflow.
- Document any safety limits, such as one match/round per request.
- Add troubleshooting notes for failed manual scrapes.

## Migration / backward-compatibility considerations

- No breaking API changes are expected.
- Manual triggers should be additive to existing scheduled behavior and must not disable automatic jobs.
- If new audit fields are unavailable in older databases, handlers should fail clearly with migration guidance rather than silently skipping logging.
