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
- 🔐 API key management (stored in SQLite)
- 🧪 CLI tools for scraping, importing, and enrichment
- 🧠 Nickname resolution and suffix cleaning for fuzzy player matching
---

## 🚀 Quick Start

### Prerequisites

- Docker & Docker Compose installed
- Port `9900` available on your host

### 1. Clone and Run

```bash
git clone https://github.com/yourname/afl-api.git
cd afl-api/src
docker-compose up --build
```

---

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
---

### Scrape and import team line-ups for a round:

```bash
python3 cli.py --scrape-lineups 9
---

# Scrape match fixtures for a round
```bash
python3 cli.py --scrape-round 9
---

# Import or export clubs
```bash
python3 cli.py --import-clubs
python3 cli.py --export-clubs
---

## 🔐 API Key Authentication

All API routes require an `x-api-key` header.

Manage your keys via:

```bash
PYTHONPATH=/app python3 scripts/manage_api_keys.py --add
```

Example request:

```bash
curl -H "x-api-key: your_key_here" http://localhost:9900/players
```

---

## 📡 API Endpoints

| Method | Endpoint                   | Description                      |
|--------|----------------------------|----------------------------------|
| GET    | `/players`                | All players                      |
| GET    | `/players?club=RIC`       | Filter by club short code        |
| GET    | `/players/club/richmond`  | By club slug                     |
| GET    | `/players/{afl_id}`       | Single player by AFL ID          |

| Method | Endpoint                           | Description             |
| ------ | ---------------------------------- | ----------------------- |
| GET    | `/api/injuries/{afl_id}`           | Injuries for a player   |
| GET    | `/api/injuries/{afl_id}?history=1` | All historical injuries |

| Method | Endpoint                        | Description                            |
| ------ | ------------------------------- | -------------------------------------- |
| GET    | `/api/lineups/{round}`          | Line-up data for the full round        |
| GET    | `/api/lineups/{round}/{afl_id}` | Line-up data for a player in a round   |
| GET    | `/api/lineups/latest/{afl_id}`  | Most recent line-up entry for a player |

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

---

## 💬 Contributing

PRs welcome. Please raise an issue first if you’d like to propose a major change.

---

## 📄 License

MIT
