# 🏉 AFL Player Scraper & Enrichment Tool

This project scrapes, enriches, and serves AFL player data across all clubs. It provides a FastAPI-powered JSON API and supports data persistence via SQLite for players, injuries, team line-ups, and API key data.

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
- 📈 Player stat scrapes run automatically 2 minutes before each match start
- 🕒 Admin page shows all upcoming scheduled jobs
---

## 🚀 Quick Start

### Prerequisites

- Docker & Docker Compose installed
- Ports `8000` and `8001` available on your host, or override `AFL_API_PORT` and `AFL_ADMIN_PORT` in `.env`

### 1. Clone and Run

```bash
git clone https://github.com/JustPlausible/afl-api.git
cd afl-api
cp .env.example .env
docker compose -f compose.example.yaml up --build
```

The repository root is the Docker build context. The Python source layout remains flat at the repository root; do not `cd` into a nested `src` directory.

---

## 🐳 Docker and Deployment Layout

This repository is now intended to be the canonical application project. The root-level `Dockerfile` builds the app image from the repository root and installs runtime dependencies from `requirements.txt`. The image default command starts the public API without `--reload`; the example Compose file uses `--reload` only for local development convenience.

Dependency manifests are split by purpose:

- `requirements.txt` contains runtime dependencies required by the API, admin UI, scheduler, and scrapers.
- `requirements-dev.txt` extends runtime dependencies and adds test/development-only dependencies.

`compose.example.yaml` is a portable example/development configuration. It intentionally avoids real production paths, ports, and secrets. It uses named volumes for local `data` and `logs` state and bind-mounts `.:/app` only for development reload workflows.

For production, keep the real Compose file, `.env`, paths, ports, and secrets outside this repository. A recommended server layout is:

```text
/opt/projects/afl-api              # Git checkout and Docker build context
/opt/docker/compose/afl-api        # Production Compose and .env files
/opt/docker/appdata/afl-api/data   # Persistent runtime database/data
/opt/docker/appdata/afl-api/logs   # Persistent runtime logs
```

Production should build from `/opt/projects/afl-api` and should not mount source over `/app`. Mount only persistent runtime state, for example:

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

## 🧩 Commands

### Run full scrape and enrich:

```bash
python3 cli.py --all
```

### Scrape or enrich individually:

```bash
python3 cli.py --scrape richmond
python3 cli.py --enrich richmond
```

### Scrape and import injury list:

```bash
python3 cli.py --scrape-injuries
```

### Scrape team line-ups:

```bash
python3 -m scraper.scrape_afl_lineups --round 9
python3 -m scraper.scrape_afl_lineups --match 7043
python3 -m scraper.scrape_afl_lineups 9  # backward-compatible round form
python3 cli.py --scrape-lineups 9
```

# Scrape match fixtures for a round
```bash
python3 cli.py --scrape-round 9
```

# Import or export clubs
```bash
python3 cli.py --import-clubs
python3 cli.py --export-clubs
```

# Scrape player stats for a match or round
```bash
python3 scraper/scrape_afl_player_stats.py --match-id 7043
python3 scraper/scrape_afl_player_stats.py --round-id 1155 --once
```

## 🔐 API Key Authentication

All API routes require an `x-api-key` header. Full API keys are shown only when created or renewed; SQLite stores only a non-reversible hash plus a short administrative prefix. Existing plaintext keys are migrated in place during database initialisation as described in `docs_api_key_migration.md`.

Manage your keys via:

```bash
PYTHONPATH=/app python3 scripts/manage_api_keys.py --add "my-label"
```

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

📊 Player Stats Endpoints
| Method | Endpoint                                     | Description                               |
| ------ | -------------------------------------------- | ----------------------------------------- |
| GET    | `/api/player-stats?match_id=7043`            | Player stats for a match                  |
| GET    | `/api/player-stats?round_id=1155`            | Player stats for a round                  |
| GET    | `/api/player-stats?afl_id=145`               | All player stats for an individual player |
| GET    | `/api/player-stats?round_id=1155&afl_id=145` | Player stats for a player across a round  |
| GET    | `/api/player-stats?match_id=7043&afl_id=145` | Single player’s stats in a specific match |

---

## 📁 Project Structure

```
src/
├── api/               # FastAPI routes
├── data/              # Scraped + enriched player data
├── db/                # SQLite init + import logic
├── merge/             # Data reconciliation and matching logic
├── scraper/           # Club, player, injuries and team lineup scrapers
├── scripts/           # API key + admin CLI tools
├── utils/             # Logging, nicknames, config
├── cli.py             # Unified CLI (scrape, enrich, import)
└── main.py            # api entry point
```

## 🧠 Job Scheduler Overview

The system uses APScheduler for timing recurring and per-match scraping jobs.

Jobs are registered during startup via `scheduler/start.py`, including:
- Daily injury scrapes (11:00 AM AWST)
- Line-up scrapes (T-1 day 5pm, Thursday 5pm, 1h before each match)
- Player stats scraping (2 minutes before match start)

Admin UI `/schedule` shows all currently registered jobs.

Jobs run in background threads using Python's `threading` and `os.system` to launch specific scraping modules.


---

## 💬 Contributing

PRs welcome. Please raise an issue first if you’d like to propose a major change.

---

## 📄 License

MIT

- [SQLite database migrations](docs_database_migrations.md)
- [Scheduler registry and restart recovery](docs_scheduler_registry.md)
