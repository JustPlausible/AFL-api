# 🏉 AFL Player Scraper & Enrichment Tool

This project scrapes, enriches, and serves AFL player data across all clubs. It provides a FastAPI-powered JSON API and supports data persistence via SQLite for players, injuries, team line-ups, and API key data.

## Project scope and design intent

AFL-api originated from a need to supply AFL data to external reporting and
fantasy-football tools. FastAPI makes the collected, normalised data simple to
consume from clients such as Google Apps Script as well as other applications.

The service itself remains consumer-neutral: it collects AFL data from supported
sources, validates and normalises it, maintains canonical identities and source
authority, persists supported data safely, and exposes stable, documented APIs
or exports. Consumer-specific scoring rules, fantasy-league management,
presentation, and client workflows belong in separate downstream applications,
not in this repository.

**Current version: 0.5.0.** See the [v0.5.0 release notes](docs/releases/v0.5.0.md),
[upgrade runbook](docs/operations/release_runbook_v0_5_0.md), and
[operational source policy](docs/operational_source_policy.md). Runtime code
reads the authoritative declaration in [`version.py`](version.py).

---

## 📦 Features

- 🌐 Scrape player data from official AFL club sites
- 🧠 Enrich player info with AFL.com.au & Champion Data IDs
- 📁 Export enriched data per club to JSON
- 🗃 Persist all data in SQLite (players, matches, injuries, lineups, clubs)
- 📅 Scrape AFL fixture by round: team names, venue, status, and scores
- ✅ Match team names to club codes with alias support
- 🩼 Injury list scraper with timestamped records
- 👨‍💻 FastAPI admin portal with:
  - Visual diff of `clubs.json` vs DB
  - One-click sync (import/export)
  - Flash messages for admin actions
- 🔐 API key management (hashed in SQLite with one-time key display)
- 🧪 CLI tools for scraping, importing, and enrichment
- 🧠 Nickname resolution and suffix cleaning for fuzzy player matching
- ⏰ Scheduled scraping of injuries, lineups, and live player stats using APScheduler
- 🧭 Lineup scrapes run at predictable times (T-1 day 5pm, Thursday 5pm, and 1h before each match)
- 📈 Durable match windows drive bounded live/post-match polling through final authoritative snapshots
- ♻️ Scheduler heartbeats and leases recover interrupted collection attempts after restart
- 🕒 Admin page shows all upcoming scheduled jobs
---

## 🚀 Development Quick Start

For a clean database, follow the authoritative
**[supported first-run sequence and command-selection table](docs/cli.md#which-command-should-i-run)**
before starting API, Scheduler, or Admin services. The abbreviated Docker example
below demonstrates the portable development stack; it does not replace database
migration, season bootstrap, and verification.

### Prerequisites

- Docker & Docker Compose installed
- Ports `8000` and `8001` available on your host, or override `AFL_API_PORT` and `AFL_ADMIN_PORT` in `.env`

### Clone and run the demo stack

```bash
git clone https://github.com/JustPlausible/afl-api.git
cd afl-api
cp .env.example .env
docker compose -f compose.example.yaml up --build
```

The repository root is the Docker build context. The Python source layout remains flat at the repository root; do not `cd` into a nested `src` directory.

This development/demo path uses source mounts and reload processes. For a clean
database, run migrations and bootstrap before starting the stack; the complete
native sequence is maintained in the [CLI first-run guide](docs/cli.md#supported-first-run-on-a-clean-installation).

## 🚀 Production-like Fresh Install

Use `compose.production.example.yaml` as the starting point for one carefully
operated SQLite instance. Put the Compose file and uncommitted `.env` outside the
checkout as described below, adapt its build context and bind mounts to the
operator's absolute host paths, and set the container database path to the
absolute persistent path `DB_PATH=/app/data/afl_players.db`.

Before exposure, set `ENVIRONMENT=production`, replace `ADMIN_PASSWORD` and
`SESSION_SECRET` with strong secrets, and select one reviewed release or exact
commit tag with `AFL_API_IMAGE` and `AFL_API_TAG`. API, Admin, and the single
Scheduler replica must all use that same image. Do not add a Scheduler host port.
For 2026, the canonical selectors are `AFL_COMPETITION_CODE=AFL`,
`AFL_COMPETITION_PROVIDER_ID=CD_C014`, and `AFL_SEASON_YEAR=2026`. The similarly
named `AFL_COMPETITION_ID`, `AFL_SEASON_ID`, and `AFL_SEASON_PID` values are
legacy scraper identifiers: confirm them from current provider data rather than
assuming the illustrative values in `.env.example` identify 2026.

From the directory containing the adapted production Compose file, initialise
the deployment in this order (replace the file flag if the deployed file has a
different name):

```bash
docker compose -f compose.production.example.yaml build --pull
docker compose -f compose.production.example.yaml run --rm --no-build afl-api python -m db.migrate
docker compose -f compose.production.example.yaml run --rm --no-build afl-api python cli.py --bootstrap-afl-season 2026
docker compose -f compose.production.example.yaml run --rm --no-build afl-api python cli.py --sync-afl-season 2026
docker compose -f compose.production.example.yaml run --rm --no-build afl-api python cli.py --report-afl-season 2026
docker compose -f compose.production.example.yaml up -d
docker compose -f compose.production.example.yaml exec afl-api python cli.py --add-api-key "2026-live"
```

Bootstrap creates or refreshes the canonical season foundation and membership;
sync then populates authoritative CFS statistics for concluded matches; report
finally performs a read-only completeness and reconciliation check. This
bootstrap → sync → report order is the normal full-season production-like
first-run path. Sync may be omitted only when an operator explicitly wants the
canonical metadata/player foundation without historical concluded-match
statistics. See the
[CLI command selector](docs/cli.md#which-command-should-i-run) for their detailed
contracts rather than treating these abbreviated commands as the full reference.

After startup, run `docker compose -f compose.production.example.yaml ps`, check
`/healthz` and `/readyz`, test the newly printed key in an `x-api-key` request,
confirm every service resolves the same `DB_PATH`, and inspect Scheduler startup,
heartbeat, and recovery logs. Also confirm exactly one Scheduler replica exists
and that it has no published host port. Deeper deployment guidance remains in
the [Docker deployment guide](docs/operations/docker_deployment.md); use the
[release/backup/rollback runbook](docs/operations/release_runbook_v0_5_0.md) for
state-safe operational procedures.

---

## 🐳 Docker and Deployment Layout

This repository is now intended to be the canonical application project. The root-level `Dockerfile` builds the app image from the repository root and installs runtime dependencies from `requirements.txt`. The image default command starts the public API without `--reload`; the example Compose file uses `--reload` only for local development convenience.

Dependency manifests are split by purpose:

- `requirements.txt` contains runtime dependencies required by the API, admin UI, scheduler, and scrapers.
- `requirements-dev.txt` extends runtime dependencies and adds test/development-only dependencies.

`compose.example.yaml` is the portable local-development, evaluation, and
test/demo configuration. Its source bind mount and `--reload` processes are
development conveniences, not stable-deployment settings.

`compose.production.example.yaml` is a concise production-like example for the
supported single-instance SQLite model. It builds the checkout once under an
operator-selected image tag shared by API, Admin, and Scheduler, omits source
mounts and reload, and persists `/app/data` and `/app/logs`. See the
[Docker deployment guide](docs/operations/docker_deployment.md) for adaptable
commands, image versioning and reproducibility, persistent paths, network and
secret boundaries, and links to release/backup/rollback operations.

For production, keep the real Compose file, `.env`, paths, ports, and secrets outside this repository. A recommended server layout is:

```text
/opt/projects/afl-api              # Git checkout and Docker build context
/opt/docker/compose/afl-api        # Production Compose and .env files
/opt/docker/appdata/afl-api/data   # Persistent runtime database/data
/opt/docker/appdata/afl-api/logs   # Persistent runtime logs
```

These paths are illustrative operator guidance, not repository defaults. A
deployment should build from its reviewed checkout and should not mount source
over `/app`. Mount only persistent runtime state, for example:

```yaml
services:
  afl-api:
    build: /opt/projects/afl-api
    env_file:
      - /opt/docker/compose/afl-api/.env
    volumes:
      - /opt/docker/appdata/afl-api/data:/app/data
      - /opt/docker/appdata/afl-api/logs:/app/logs
```

This keeps `/opt/docker/appdata/afl-api` as persistent runtime state only while the application source and Docker build context live under `/opt/projects/afl-api`.


## 🔐 Production Admin and Scheduler Exposure

The intended production boundary is:

- `afl-api` is the only service designed for public exposure, either by publishing its API port directly or by placing a reverse proxy in front of it. The public API still requires `x-api-key` authentication on API routes.
- `afl-admin` is an operator-facing management interface protected by HTTP Basic authentication and CSRF protection. It remains reachable through the configured `${AFL_ADMIN_PORT:-8001}` port on a normal `admin-access` bridge network so deployments can secure it with trusted LAN access, firewall rules, VPNs, SSH forwarding, a private reverse proxy, or equivalent controls. Do not treat it as a public unauthenticated service.
- `afl-scheduler` has scheduler management and mutation routes such as `/scheduler/refresh`, `/scheduler/run/{job_id}`, and `/scheduler/job/{job_id}`. These routes must not be published to a host port or exposed by a public reverse proxy.
- `afl-admin` and `afl-scheduler` communicate over the Compose `management` network at `http://afl-scheduler:8000`. That network is marked `internal: true`, so it is the trusted service-to-service management path.
- `afl-admin` is not attached to Docker's default network in the documented Compose layout. It joins `management` for private scheduler calls and `admin-access` so Docker can create a usable host-side mapping for the authenticated operator port. `afl-scheduler` is not attached to the default application network or `admin-access`; it joins the internal `management` network for admin calls and a separate `scheduler-egress` bridge network only to preserve outbound internet access required by AFL scraping jobs.

Do not publish scheduler ports in production Compose files. If scheduler mutation endpoints become reachable outside the trusted management network in a supported deployment, add the smallest practical shared internal token for those mutation routes before exposing that deployment. For the documented production Compose model, a separate scheduler token is not required because the scheduler is not host-published, admin reaches it on the internal management network, `admin-access` is only for the authenticated admin host port, and the configured scheduler egress network is for outbound scraping rather than inbound management access.

## 🧩 Operator CLI

Use the [operator command selector and supported first-run sequence](docs/cli.md#which-command-should-i-run)
to choose a workflow, and the implemented parser as the complete option reference:

```bash
python cli.py --help
```

JSON is the preferred operational source. For example, public AFL metadata can
be inspected without database writes, or bootstrapped into the canonical
tables:

```bash
python cli.py --collect-afl-metadata --afl-season 2026 --print-json
python cli.py --bootstrap-afl-season 2026
```

### Whole-season persistent synchronisation

`--bootstrap-afl-season` only establishes canonical metadata, player identity,
provider crosswalks, and season membership. To initialise or refresh that
foundation **and** authoritative concluded match statistics in one operation,
use:

```bash
python cli.py --sync-afl-season 2026
python cli.py --sync-afl-season 2026 --round 8
python cli.py --sync-afl-season 2026 --round-from 8 --round-to 12
python cli.py --sync-afl-season 2026 --match-id 8204
python cli.py --sync-afl-season 2026 --refresh-complete --print-json
```

The command requires the same public AFL access and authenticated CFS endpoints
used by bootstrap and single-match CFS collection. The client automatically
obtains and refreshes its WMC token; there is no configured operator credential
variable for that token. The command selects
matches from the persisted canonical season: only concluded/final matches with
an explicit `CD_M...` mapping are collected. Existing concluded authoritative
snapshots are skipped unless `--refresh-complete` is given. Each match is a
separate transaction, so one failure does not undo earlier matches.

Reruns use existing metadata/player upserts and CFS snapshot-authority rules:
unchanged observations are zero-write, corrected concluded observations may be
updated, and live, stale, empty, unavailable, or unknown responses never delete
or downgrade concluded data. Statistics go only to `cfs_player_stats`; there is
no rendered-HTML fallback or legacy `player_stats` dual write.

The JSON result distinguishes skipped, unavailable/unpublished, empty, partial,
unknown, failed, already-complete, and collected matches and includes correlated
audit IDs. Default output is a concise human-readable summary; `--print-json`
emits the complete machine-readable result, including match-level details, to
stdout even when the command subsequently exits non-zero.

Exit status `0` means all currently actionable concluded matches were collected
or already complete; scheduled, current live/postgame, and recognised future
placeholders may be safely skipped. Exit status `1` means material actionable or
explicitly requested work was unavailable, empty, partial, rejected, unknown,
missing provider identity, absent, or failed. Exit status `2` is invalid CLI
usage. A bounded round/range returning no matches exits `1`; an unbounded season
with no published fixtures reports `empty_unbounded` and exits `0`.

Round ranges are inclusive. Repeatable `--match-id` values are deduplicated in
first-seen order. Combining match IDs with a round or range uses intersection
semantics, so every requested ID must also satisfy the round bound. Round `0` is
valid, negative rounds are invalid, and match IDs must be positive.

Summary units are explicit: `records_received` and
`total_matches_discovered` count selected matches; `eligible_matches` counts
concluded matches with provider identity, including already-complete matches;
`collected_successfully` counts matches actually recollected; and
`already_complete_unchanged` counts matches skipped as complete. `rows_inserted`,
`rows_updated`, `rows_unchanged`, and `rows_written` count CFS statistic rows,
with `rows_written` equal to inserted plus updated rows. Deeper relationship completeness and
reconciliation reporting remains the responsibility of Issue #107.

The existing commands remain deliberately distinct:

* `--bootstrap-afl-season`: canonical foundation only;
* `--sync-afl-season`: whole-season persistent foundation and CFS statistics;
* `--collect-match-player-stats`: one explicitly identified CFS match;
* `--collect-afl-data --no-database`: diagnostic files only, never persistence.

Authenticated Champion Data/CFS commands require opaque provider IDs, not AFL
numeric IDs. Roster collection is read-only; match player-stat collection
persists to `cfs_player_stats`:

```bash
python cli.py --collect-match-rosters CD_R202601421 --print-json
python cli.py --collect-match-player-stats CD_M20260142001 --print-json
```

Rendered-HTML commands remain available only as explicit legacy tools; the CLI
never silently falls back from JSON to HTML. Club tools, persistence behavior,
identifier formats, diagnostics, prerequisites, and every legacy command are
documented in the **[operator CLI guide](docs/cli.md)**.

Scheduler and Admin player-stat jobs use the same operational CFS JSON collector
and write `cfs_player_stats`. The explicit CLI command
`--collect-match-player-stats` uses that path too; the separate manual
`--scrape-match` command renders HTML and writes only the legacy `player_stats`
table. The tables are not interchangeable and these workflows do not dual-write.
The complete identifier, reader, lifecycle, fallback, and migration rules are in
the **[player-stat storage contract](docs/architecture/player_stats_storage_contract.md)**.
Injuries and operational lineups deliberately remain rendered-HTML sources:
Scheduler and Admin persist them through the shared policy, while the CLI exposes
`--scrape-injuries` and `--scrape-lineups` explicitly. Injury rows are persisted
only after canonical AFL player-ID resolution.

## 🔐 API Key Authentication

All API routes require an `x-api-key` header. Full API keys are shown only when created or renewed; SQLite stores only a non-reversible hash plus a short administrative prefix. Existing plaintext keys are migrated in place during database initialisation as described in [`docs/api_key_migration.md`](docs/api_key_migration.md).

Manage your keys via the operator CLI, run from the repository root (or the
equivalent working directory inside the container — no `PYTHONPATH` needed):

```bash
python cli.py --add-api-key "my-label"
python cli.py --list-api-keys
python cli.py --remove-api-key "my-label"
```

See [`docs/cli.md`](docs/cli.md#api-key-management) for the full reference.

Example request:

```bash
curl -H "x-api-key: your_key_here" http://localhost:9900/players
```

---


## 🔒 Admin Security

The admin application is protected with HTTP Basic authentication. Configure credentials with:

```bash
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-me
SESSION_SECRET=replace-with-a-long-random-secret
```

`ADMIN_PASSWORD` and `SESSION_SECRET` must be set explicitly when `ENVIRONMENT=production`. The public API continues to use the `x-api-key` header, and disabled API keys are rejected.

Diagnostic header echoing at `/api/echo-headers` requires a valid API key and redacts sensitive headers such as `x-api-key`, `authorization`, `cookie`, and `x-admin-key`.

## 📡 API Endpoints
> 📘 For full interactive API docs, visit [`/docs`](http://localhost:8801/docs) while the app is running.

👤 Player Endpoints
| Method | Endpoint                   | Description                      |
|--------|----------------------------|----------------------------------|
| GET    | `/players`                | All players                      |
| GET    | `/players?club=RIC`       | Filter by club short code        |
| GET    | `/players/club/richmond`  | By club slug                     |
| GET    | `/players/{afl_id}`       | Single player by AFL ID          |

🩼 Injury Endpoints
| Method | Endpoint                           | Description             |
| ------ | ---------------------------------- | ----------------------- |
| GET    | `/api/injuries/{afl_id}`           | Injuries for a player   |
| GET    | `/api/injuries/{afl_id}?history=1` | All historical injuries |

🧍‍♂️ Line-up Endpoints
| Method | Endpoint                        | Description                            |
| ------ | ------------------------------- | -------------------------------------- |
| GET    | `/api/lineups/{round}`          | Line-up data for the full round        |
| GET    | `/api/lineups/{round}/{afl_id}` | Line-up data for a player in a round   |
| GET    | `/api/lineups/latest/{afl_id}`  | Most recent line-up entry for a player |

🔁 Round Endpoints
| Method | Endpoint                 | Description                   |
| ------ | ------------------------ | ----------------------------- |
| GET    | `/api/rounds`            | All available rounds          |
| GET    | `/api/rounds/{round_id}` | Metadata for a specific round |

🏟 Match Endpoints
| Method | Endpoint                     | Description                              |
| ------ | ---------------------------- | ---------------------------------------- |
| GET    | `/api/matches`               | All matches (optionally filter by round) |
| GET    | `/api/matches?round_id=1155` | Matches for a specific round             |
| GET    | `/api/matches/{match_id}`    | Details of a single match                |

📊 Legacy Player Stats Compatibility Endpoint
| Method | Endpoint                                     | Description                               |
| ------ | -------------------------------------------- | ----------------------------------------- |
| GET    | `/api/player-stats?match_id=7043`            | Legacy HTML-scraped stats for a match                  |
| GET    | `/api/player-stats?round_id=1155`            | Legacy HTML-scraped stats for a round                  |
| GET    | `/api/player-stats?afl_id=145`               | Legacy HTML-scraped stats for an individual player |
| GET    | `/api/player-stats?round_id=1155&afl_id=145` | Legacy HTML-scraped stats for a player across a round  |
| GET    | `/api/player-stats?match_id=7043&afl_id=145` | One legacy HTML-scraped player/match row |

This compatibility route reads `player_stats`; it is not an authoritative CFS
read API. New API and scoring work must follow the [player-stat storage
contract](docs/architecture/player_stats_storage_contract.md).

---

## 📁 Project Structure

```
src/
├── api/               # FastAPI routes
├── data/              # Scraped + enriched player data
├── db/                # SQLite init + import logic
├── merge/             # Data reconciliation and matching logic
├── scraper/           # Club, player, injuries and team lineup scrapers
├── scripts/           # API key persistence + admin CLI helpers (invoked via cli.py)
├── utils/             # Logging, nicknames, config
├── cli.py             # Unified CLI (scrape, enrich, import)
└── main.py            # api entry point
```

## 🧠 Job Scheduler Overview

The system uses APScheduler for timing recurring and per-match scraping jobs.

Jobs are registered during startup via `scheduler/start.py`, including:
- Daily injury scrapes (11:00 AM AWST)
- Line-up scrapes (T-1 day 5pm, Thursday 5pm, 1h before each match)
- Durable match-window planning with configured, concurrency-bounded live and
  post-match polling until a final authoritative player-stat snapshot is stored
- Runtime heartbeats plus lease-based recovery of interrupted attempts

Run exactly one Scheduler replica for each SQLite database.

Admin UI `/schedule` shows all currently registered jobs.

### Scheduler startup and shutdown

The supported production scheduler command is:

```bash
python -m uvicorn scheduler.start:app --host 0.0.0.0 --port 8000
```

Uvicorn owns the FastAPI process lifecycle. During application startup, `scheduler/start.py` runs database migrations, registers scheduler jobs once, and starts APScheduler in a managed background thread. During application shutdown, it stops APScheduler and waits for executors to finish before the interpreter exits.

Standalone module execution is also supported for local operation and diagnostics:

```bash
python -m scheduler.start
```

This standalone mode starts the same scheduler bootstrap path, then blocks the main process in APScheduler. Stop it with normal process interruption or termination, such as `Ctrl+C` or `SIGTERM`; the signal handler shuts down APScheduler and its executors before exit.

At startup, historical one-time date jobs whose scheduled run time is already in the past are intentionally marked `skipped` instead of being submitted to APScheduler. This prevents old fixture data from creating a backlog of obsolete scrapes while preserving future one-time jobs and recurring cron or interval jobs.


---

## 💬 Contributing

PRs welcome. Please raise an issue first if you’d like to propose a major change.

---

## 📄 License

MIT

---

## 📚 Documentation

- [Documentation index](docs/README.md)
- [Administrator CSRF protection](docs/admin_csrf.md)
- [Admin manual scheduler triggers](docs/admin_manual_triggers.md)
- [API key storage migration](docs/api_key_migration.md)
- [SQLite database migrations](docs/database_migrations.md)
- [Scheduler registry and restart recovery](docs/scheduler_registry.md)
- [Scrape run audit records](docs/scrape_run_audit.md)
- [AFL scraper source inventory and page contracts](docs/scraper_source_inventory.md)

## Scraper HTTP network policy

Plain HTTP scraper calls must use the shared synchronous client in `utils.http_utils` instead of calling `requests`, `httpx`, or `urllib` directly. Rendered-page scraping remains a separate Playwright responsibility: future modules should keep Playwright for pages whose usable content depends on JavaScript rendering, including player-stat pages unless deterministic fixtures/tests prove plain HTTP contains all required data.

The default scraper HTTP policy is intentionally conservative:

- User-Agent: `AFL-api/1.0 (+https://github.com/JustPlausible/AFL-api)`.
- Timeout: 5 seconds to connect and 20 seconds to read by default.
- Retries: at most 3 attempts for transient failures only.
- Retryable failures: HTTP 429, HTTP 500, 502, 503, 504, connection failures, timeouts, and clearly transient transport errors such as chunked transfer or content decoding failures.
- Non-retryable failures: permanent HTTP 4xx responses are not retried unnecessarily.
- Backoff: bounded exponential backoff starting at 0.5 seconds, capped at 8 seconds, with up to 0.25 seconds of jitter. `Retry-After` is honoured where practical and capped at 10 seconds.
- Rate limiting: one thread-safe bucket per host, per Python process, with a default 1 second spacing between same-host requests. This is not a distributed or cross-container rate limiter.

The supported operational knobs are the `ScraperHttpPolicy` values passed to `ScraperHttpClient` for explicit module-specific overrides. Overrides should be rare, local to the module that needs them, and documented in code with the reason; do not introduce competing request helpers for one-off timeout, retry, or rate-limit changes. Tests can inject sessions, clocks, sleep functions, jitter functions, and rate limiters, and can reset the process default client with `reset_scraper_http_client()` to avoid live network access or real sleeping.
