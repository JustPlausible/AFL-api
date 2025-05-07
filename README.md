# 🏉 AFL Player Scraper & Enrichment Tool

This project scrapes, enriches, and serves AFL player data across all clubs. It provides a FastAPI-powered JSON API and supports data persistence via SQLite for both player and API key data.

---

## 📦 Features

- 🌐 Scrape player data from official AFL club websites
- 🔍 Enrich player data with AFL.com.au IDs, Champion Data IDs, club profile info
- 🧠 Output enriched JSON files (per club)
- 🗃 Store and serve all data via SQLite
- 🔐 API key authentication (stored in SQLite)
- 🧪 CLI tools for managing data and access
- ⚙️ FastAPI server with structured routes
- 📅 Track injury data per player and update status in the database
- 🧠 Auto-resolve names using nickname mapping, suffix cleaning, fuzzy matching
- 📝 Save unmatched names for admin review via `nickname_suggestions.txt`
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

---

## 📁 Project Structure

```
src/
├── api/               # FastAPI routes
├── data/              # Scraped + enriched player data
├── db/                # SQLite init + import logic
├── merge/             # Data reconciliation and matching logic
├── scraper/           # Club scraping logic
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
