# AFL-api v0.4.0 Architectural Review

## Executive summary

After the v0.4.0 milestone, AFL-api is no longer an early-stage scraper prototype. It has a real service boundary, an authenticated public API, an operator admin UI, scheduled background work, SQLite migrations, Docker packaging, CI, and regression tests. The project has moved from "scripts that scrape pages" to a small operational data product.

The remaining risk is concentrated in the scraper subsystem. Most scrapers still couple page navigation, DOM selectors, parsing, persistence, CLI concerns, logging, and audit bookkeeping in the same modules. Playwright is still the default fetch path for fixture, match, lineup, and player-stat collection even though the repository now contains a reusable HTTP client. The next release should therefore focus less on adding user-facing features and more on making AFL data acquisition observable, fixture-backed, selector-resilient, and cheaper to run.

## Maturity by area

| Area | Strengths | Technical debt | Unnecessary complexity | Missing capabilities | Maturity |
| --- | --- | --- | --- | --- | --- |
| Repository structure | Clear top-level domains for `api`, `db`, `scheduler`, `scraper`, `templates`, `tests`, and `utils`; deployment files live at the root. | Flat application modules (`admin.py`, `main.py`, `cli.py`, `config.py`) make boundaries less explicit; docs are split between root `docs_*.md` files and `docs/`. | Multiple one-off root docs and compatibility modules increase navigation cost. | Package-level architecture overview and ownership map. | 7/10 |
| Scraper architecture | Separate modules exist for fixtures, matches, lineups, injuries, players, and stats; active CSS selectors are centralised; scrape-run auditing wraps major entrypoints. | Scrapers mix browser/network loading, parsing, transformation, persistence, retry policy, CLI parsing, and logging; import-time filesystem/DNS work exists in the stats scraper. | Legacy scraper variants and special-case retry wrappers duplicate concepts now handled elsewhere. | Source inventory, page contract docs, scraper interface, fixtures for every active scraper, and explicit fetch-mode choice per source. | 5/10 |
| Parser architecture | Some parser functions are pure enough for tests (`parse_fixtures_metadata`, `parse_round_list`, `parse_matches`, `parse_lineups_html`, `parse_live_stats`). | Parser outputs are loose dictionaries with inconsistent naming (`round_number` vs `round_id`, `champion_id` vs `champion_data_id`) and broad exception swallowing inside row parsing. | Dataclass selector containers are useful, but parser abstractions are not yet present, so central selectors alone do not enforce contracts. | Typed domain models, parse diagnostics, golden fixtures for injuries/players/stats/lineups, and explicit nullability rules. | 5/10 |
| Networking layer | `ScraperHttpClient` provides timeouts, retry/backoff, per-host rate limiting, redaction, and test coverage. | Active high-value scrapers still call Playwright directly or through `load_page_with_playwright`; HTTP client is not yet the primary acquisition path. | Both bespoke Playwright retry code and shared HTTP retry policy coexist. | JSON/API discovery, HTTP-first fetchers, cacheable fixture collection, and consistent user-agent/timeout use by all scrapers. | 6/10 |
| Database layer | Additive migration runner, baseline validation, API-key hash migration, job registry, and scrape-run audit tables are substantial maturity improvements. | SQLite connection handling is distributed; some scrapers hard-code `data/afl_players.db` instead of using `get_db_connection`/configured paths. | Strict baseline classification protects production but can make ad-hoc recovery harder. | Schema docs, indexes review for query endpoints, foreign-key policy, backup/restore runbook, and stronger repository-level data-access conventions. | 7/10 |
| Scheduler | APScheduler has deterministic job IDs, registry persistence, startup reconciliation, manual trigger endpoints, listener logging, and a FastAPI management API. | Scheduler mutation endpoints rely on Compose network isolation rather than route-level service authentication; refresh registration path does not fully mirror startup registration. | Multiple registration modules and wrapper layers make job lifecycle reasoning harder. | Misfire/coalescing policy docs, job concurrency limits per AFL host/source, admin-visible run history linked to jobs, and stale job cleanup policies. | 7/10 |
| Admin interface | Basic auth, production secret enforcement, CSRF protection, API-key management, table browsing, schedule view, and manual scheduler triggers are present. | Admin code is a large module with direct SQL and scheduler HTTP calls; table browsing needs stricter allowlisting and operator-focused UX. | Generic DB table viewer may expose internals while duplicating better purpose-built operational pages. | Scrape-run dashboard, failed-job retry workflow, data freshness view, role separation, and clearer deployment guidance for private exposure. | 6/10 |
| Testing | Pytest suite covers security, migrations, scheduler registry/startup, manual triggers, scrape-run auditing, HTTP client behavior, selectors, and fixture/match parsers. | Coverage is strongest around infrastructure and weaker around live scraper page variations. Browser-dependent behavior is mostly mocked or unexercised. | AST selector policing is clever but may be brittle as parser abstractions evolve. | Golden-fixture corpus, parser contract tests for all scraper types, property/edge-case parser tests, Docker smoke tests, and coverage reporting. | 7/10 |
| Documentation | README documents API endpoints, Docker layout, admin/scheduler exposure, API keys, and common commands; focused docs exist for migrations, CSRF, scheduler registry, manual triggers, and scrape-run audit. | Planning state is split across TODO and completed milestone docs; root-level docs naming is inconsistent. | Some operational decisions are repeated between README and individual docs. | Architecture decision records, scraper source map, runbooks, release roadmap, issue index, and data dictionary. | 6/10 |
| Docker deployment | Playwright base image pins browser/runtime compatibility; Compose separates public API, admin, and scheduler; healthchecks and named volumes exist. | Example Compose remains development-oriented with source bind mounts and reload enabled for API/admin. | Four networks are justified for security, but may be hard for casual operators to understand. | Production Compose template, backup volumes guidance, resource limits, non-root runtime review, log rotation, and startup migration strategy per service. | 7/10 |
| CI | GitHub Actions runs pytest and Docker build on push and pull request. | CI does not run lint/type checks, coverage thresholds, compose config validation, or parser fixture refresh checks. | Current CI is intentionally simple and maintainable. | Ruff/format check, mypy/pyright once types improve, coverage artifact, Docker smoke test, and dependency/audit scanning. | 6/10 |
| Operational readiness | Health endpoints, scheduler registry, scrape-run audit, secret redaction, and Compose isolation move the project toward production operations. | No formal SLOs, alerts, freshness checks, backup restore tests, or incident runbooks. | Separate admin and scheduler services are operationally valuable but increase deployment surface. | Data freshness alerts, metrics, structured logs, dashboard, backup/restore verification, and graceful degraded-mode behavior for AFL site changes. | 6/10 |

## Highest-value improvements remaining

1. Build a scraper source inventory and AFL page-structure documentation for fixtures, matches, lineups, injuries, players, and stats. **Create GitHub Issue.** Do this before large redesign work.
2. Establish a golden fixture corpus for every active scraper, including representative upcoming, live, completed, bye/special-round, missing-field, and changed-selector pages. **Create GitHub Issue.** Start immediately.
3. Split scraper modules into fetchers, parsers, normalisers, and persistence adapters. **Create GitHub Issue.** Begin after page documentation starts.
4. Make HTTP/JSON discovery the default investigation path and reserve Playwright for sources that require browser execution. **Create GitHub Issue.** Best after source inventory identifies candidates.
5. Introduce typed scraper result models and parse diagnostics rather than loose dictionaries and broad row-level exception swallowing. **Create GitHub Issue.** Can wait until parser boundaries are designed.
6. Remove hard-coded database paths from scrapers and standardise on configured connection helpers. **Create GitHub Issue.** Do soon; it is small and reduces test/deploy surprises.
7. Add an operational scrape-run dashboard showing recent runs, failure summaries, row counts, durations, and data freshness. **Create GitHub Issue.** Useful before broader production use.
8. Add production deployment docs/templates covering non-reload Compose, source-free containers, backups, log rotation, and service resource limits. **Create GitHub Issue.** Can proceed independently of scraper redesign.
9. Add CI checks for formatting/linting, compose validation, and a Docker smoke test against `/readyz`. **Create GitHub Issue.** Can proceed independently.
10. Consolidate root documentation into a `docs/` index with ADRs and milestone/roadmap pages. **Create GitHub Issue.** Nice to do but can wait behind scraper work.

## Work that should wait until after scraper redesign

- Broad parser abstraction across all modules should wait until the source inventory and fixture corpus define the contracts.
- Type-heavy domain model work should wait until naming and nullability are agreed.
- Large admin UX additions should wait until scrape-run and job data models are stable.
- Performance tuning should wait until Playwright-vs-HTTP decisions are measured per source.
- Removing legacy scraper files should wait until equivalent fixture coverage confirms no behavior is still needed.

## Issues that can likely be closed as obsolete

The following completed TODO items appear obsolete as open issues if they still exist in GitHub:

- Add fixture/match parser fixture tests.
- Centralise AFL scraper selectors.
- Add GitHub Actions CI.
- Add SQLite migration runner.
- Persist scheduler job registry state.
- Add unified scrape-run audit model.
- Harden admin/scheduler Compose exposure.
- Add admin manual scheduler triggers.
- Fix scheduler startup duplicate-registration safety.
- Add shared scraper HTTP client.

## Simplification opportunities

- Move root `docs_*.md` files under `docs/` and add a single index.
- Convert root application modules into a package once import paths are stable.
- Delete or archive `scrape_afl_lineups-early2025.py` after fixture parity is proven.
- Replace bespoke `retry_load_page` with the shared network policy or a browser-specific equivalent.
- Remove import-time side effects from `scrape_afl_player_stats.py` so imports are cheap and deterministic.
- Use one DB connection helper consistently instead of mixing direct `sqlite3.connect("data/afl_players.db")` calls with configured helpers.
- Make scheduler refresh registration call the same registration path as startup to reduce drift.
- Replace generic admin table browsing with a smaller set of allowlisted operational views if security/UX becomes a concern.

## Technical debt register

| Area | Debt | Risk | Effort | Priority | Can wait? |
| --- | --- | --- | --- | --- | --- |
| Scrapers | Browser loading is default for sources that may have HTTP/JSON alternatives. | High runtime cost, fragility, slower CI/ops. | Medium | P1 | No |
| Scrapers | Fetch, parse, persist, audit, and CLI concerns are mixed. | Slows feature work and makes failures hard to isolate. | High | P1 | No |
| Scrapers | Import-time filesystem/DNS work in stats scraper. | Test flakiness and unexpected startup failures. | Low | P1 | No |
| Parsers | Loose dictionaries and inconsistent field names. | Silent data-shape drift and harder refactors. | Medium | P2 | Partly |
| Parsers | Broad exception swallowing while parsing stat rows. | Bad selectors/data can degrade silently. | Low | P2 | Partly |
| Fixtures | Limited golden fixture corpus. | AFL markup changes may break production before tests detect it. | Medium | P1 | No |
| Networking | Shared HTTP client not widely adopted. | Duplicate retry/rate-limit behavior. | Medium | P2 | Partly |
| Database | Hard-coded DB paths in some scrapers. | Incorrect DB in tests/containers/custom deployments. | Low | P1 | No |
| Scheduler | Startup and manual refresh registration paths can drift. | Missing jobs after refresh. | Low | P2 | Partly |
| Scheduler | Mutation API lacks route-level auth. | Risk if Compose boundary is misconfigured. | Low | P2 | Yes, while internal-only |
| Admin | Large module with direct SQL and HTTP calls. | Harder to test and extend safely. | Medium | P3 | Yes |
| Admin | Generic table browser exposes internals. | Operator mistakes or accidental sensitive data display. | Low | P2 | Partly |
| Docs | Docs split across root and `docs/`. | Onboarding friction. | Low | P3 | Yes |
| Docker | Example Compose is development-first. | Production copy/paste risk. | Low | P2 | Partly |
| CI | No lint/type/coverage/smoke checks. | Style drift and missed integration failures. | Medium | P2 | Partly |
| Ops | No data freshness alerts or backup restore test. | Failures may be noticed late. | Medium | P1 | No before production reliance |

## Scraper subsystem assessment and next-release recommendation

The next release should be a **scraper reliability and source-intelligence release**. The highest-value sequence is:

1. **Document AFL page structures and source contracts.** Capture URL patterns, required fields, DOM anchors, suspected embedded JSON/API calls, and known page states for every active scraper.
2. **Collect fixtures while documenting.** Every documented page state should become a test fixture or recorded sample.
3. **Discover JSON/API sources before optimizing Playwright.** Use browser devtools/manual inspection outside the app and document whether each scraper can become HTTP-first.
4. **Reduce Playwright usage where evidence supports it.** Do not replace browser flows blindly; retire Playwright per source only after fixture-backed parity tests exist.
5. **Introduce parser boundaries and typed results.** Once fixtures define contracts, split parsing out of side-effectful scraper modules.
6. **Improve selector resilience.** Add selector fallback strategies, required/optional selector classification, and parse diagnostics.
7. **Measure scraper performance after fetch-mode changes.** Performance work is valuable, but less important than reducing fragility and understanding sources.

Recommended release theme: **v0.5.0 Scraper Reliability Foundation**.

Suggested v0.5.0 scope:

- AFL source inventory and page-structure docs.
- Golden fixture corpus for all active scrapers.
- HTTP-vs-Playwright decision matrix per source.
- Parser/fetcher/persistence split for one pilot scraper, preferably match fixtures or player stats.
- Removal of hard-coded DB paths in active scrapers.
- Import-time side-effect cleanup in stats scraper.
- Initial scrape-run admin dashboard or data freshness report if time allows.
