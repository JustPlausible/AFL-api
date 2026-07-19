# Add shared rate-limited HTTP client for AFL scraping

## Background / problem statement

Scrapers make network requests to AFL-related endpoints from multiple modules. Without one shared request policy, retries, timeouts, user-agent headers, and rate limiting can drift between scrapers. That increases the risk of flaky scrapes, excessive load on upstream services, and inconsistent error handling.

## Scope

Create and adopt a shared HTTP utility for scraper network access with conservative defaults.

## Implementation requirements

- Extend or replace the existing HTTP helper with a single shared client interface for scraper modules.
- Provide default connect/read timeouts, a repository-specific user agent, bounded retries for transient failures, and exponential backoff with jitter.
- Add per-host rate limiting suitable for AFL web requests.
- Ensure errors include enough context for debugging while redacting sensitive headers or credentials.
- Update the highest-volume scraper modules to use the shared client instead of direct `requests`, `httpx`, or ad hoc Playwright navigation where a simple HTTP fetch is sufficient.
- Keep Playwright-based scraping available where JavaScript rendering is required.

## Out of scope

- Removing Playwright from player-stat scraping if rendered pages still require it.
- Implementing a distributed rate limiter across multiple containers.
- Changing the parsed data model for fixtures, lineups, injuries, or stats.
- Adding third-party paid scraping/proxy services.

## Acceptance criteria

- Scraper HTTP calls use common timeout, retry, and user-agent behavior unless a module documents why it needs an override.
- Transient 429/5xx/network failures retry within bounded limits.
- Permanent 4xx failures do not retry unnecessarily.
- Rate limiting prevents bursts from concurrent scraper invocations in the same process.
- Existing scraper CLI commands still work with the new client.

## Testing requirements

- Unit tests for retry eligibility, backoff bounds, timeout configuration, and rate-limit behavior.
- Tests verifying sensitive headers are redacted from logged/request error output.
- Regression tests or mocks for updated scraper modules showing they call the shared client.
- Existing scraper-related tests continue to pass.

## Documentation updates required

- Document the shared HTTP client defaults and override policy.
- Add guidance for future scraper modules to avoid direct network calls.
- Note operational knobs if any timeout, retry, or rate-limit values are configurable.

## Migration / backward-compatibility considerations

- Existing CLI behavior and output should remain compatible.
- Any new dependencies must be added to the appropriate requirements file and remain compatible with the Docker image.
- Network policy changes should be conservative to avoid surprising production scrape duration increases.
