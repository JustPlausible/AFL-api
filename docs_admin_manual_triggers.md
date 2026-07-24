# Admin manual scheduler triggers

The authenticated admin schedule page includes manual controls for narrowly scoped scheduler work:

- Refresh injuries, with no identifier.
- Refresh fixtures for one round.
- Refresh lineups for one round.
- Refresh lineups for one match.
- Refresh player statistics for one match using the existing explicit once-only scraper entry point.

Each form submits a POST with the existing admin CSRF token. The admin route validates the submitted identifier, then calls only the matching internal scheduler mutation endpoint on the private scheduler service. Admin handlers do not execute shell commands, arbitrary Python functions, or scraper logic directly.

## Operator workflow

1. Open the authenticated admin schedule page.
2. Choose one manual trigger.
3. Enter exactly one round ID or match ID when the selected trigger requires one.
4. Submit the POST form.
5. Treat the success message as acceptance that work was queued, not proof that scraping completed.
6. Inspect the scheduler listing and the `scrape_runs` audit table to confirm queued, running, completed, failed, or duplicate status.

For safety, each request is limited to one round or one match. Submit separate requests for separate targets.

## Internal endpoint protection

The scheduler mutation endpoints are internal-only and must not be publicly exposed. In the supported Compose deployment, the admin service reaches `afl-scheduler` over the internal management network. Do not publish scheduler ports or route these endpoints through the public API service.

## Troubleshooting

- Validation failures: reload the schedule page, confirm the CSRF token is current, and verify the round or match identifier exists in application data.
- Scheduler unavailable: confirm the `afl-scheduler` service is healthy and reachable on the internal management network.
- Missing migrations: run database migrations if the scheduler reports that `scheduler_job_registry` or `scrape_runs` is missing.
- Duplicate jobs: an equivalent manual job may already be queued or running; inspect scheduler status before retrying.
- Failed scrapes: inspect the scheduler registry row and the associated `scrape_runs` audit record for concise failure summaries. Full tracebacks, secrets, and internal endpoint details are not shown in admin messages.
