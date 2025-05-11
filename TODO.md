# AFL Scraper TODO

This is a planning document to help break down development of the AFL scraping and stat tracking system.

---

## ✅ Setup & Environment

- [x] Initialise Git repo
- [x] Setup virtual environment and requirements.txt
- [x] Add logging to all modules
- [x] Prepare `.env` or config for season/year

---

## 🧩 Fixtures Index Scraper

- [x] Scrape `data-season-id`, `data-competition-id`, and `data-round-id` from `.js-react-fixtures`
- [x] Extract list of `round_id` and round names from HTML
- [ ] Store season and round info in `rounds` table
- [ ] Add CLI command: `cli.py scrape-fixtures-index`

---

## 🏟 Match Scraper (per round)

- [x] Given a round ID, fetch fixture page
- [x] Parse match details: teams, venue, time, status
- [x] Create or update `matches` table
- [x] Add CLI command: `cli.py scrape-round --round-id=1147`
- [x] Track match `status` transitions: UPCOMING → LIVE → COMPLETED

---

## 🧍‍♂️ Team Lineups Scraper

- [x] Use `/matches/team-lineups` with round context
- [x] Parse player names and positions
- [x] Store in `lineups` table with match ID and team
- [x] Capture AFL and Champion Data player IDs when available

---

## 🩼 Injury List Scraper

- [x] Scrape from `https://www.afl.com.au/news/injury-list`
- [x] Parse club, player name, injury, return estimate
- [x] Store with timestamp or round context
- [x] Add command: `cli.py scrape-injuries`

---

## 🧠 Data Mapping

- [x] Maintain player ID cross-reference table
- [x] Add matching logic for player name + club
- [ ] Optionally use fuzzy matching fallback

---

## 📦 Database & Exports

- [x] Finalise schema (`rounds`, `matches`, `lineups`, `injuries`, `players`)
- [ ] Export CSV or JSON for Sheets import
- [ ] Optional: Push data to Google Sheets API

---

## 🧪 Stats Scraper (Live + Completed)

- [x] Scrape from single match page using Playwright
- [x] Add `--match-id` and `--once` options
- [x] Add --round-id to backfill entire round of stats
- [x] Parse individual player stat fields (AF, G, B, etc.)
- [x] Upsert to `player_stats` table with `UNIQUE(match_id, afl_id)`
- [x] Normalise `status` field with CHECK constraint handling
- [x] Avoid excess page loads with delay + reuse
- [x] Add `scraped_at` for timing precision
- [ ] Add `match_clock_label` field to capture page state (e.g. “Q3 03:10”)
- [x] Add scraper scheduling (2 mins before match)
- [x] Confirm Playwright runs in parallel across matches
- [x] Refactor job registration to FastAPI-compatible APScheduler
---

## 🗂 Scraping Logs & Summary

- [x] Log every scrape attempt to `scrape_log` table
- [x] Store summary per match in `scrape_summary` table
- [x] Update `scrape_summary` with `first_scraped`, `last_scraped`, total count, and completion status
- [x] Ensure scheduled match jobs log exact run time per match_id
- [ ] Add CLI or admin tool to summarise all completed matches
- [ ] Add option to clear `scrape_log` once summarised
- [ ] Visualise scrape count and status for each match

---

## 🔄 Job Scheduler System

- [x] Move to APScheduler with FastAPI and background threads
- [x] Create scheduler/start.py as unified scheduler + API launcher
- [x] Separate daily and dynamic job registration
- [x] Use docker-compose `command:` to start Uvicorn with `start.py`
- [x] Fix logging issues with PYTHONUNBUFFERED=1
- [x] Expose /scheduler/jobs as JSON endpoint
- [x] View grouped schedule in /schedule admin page
- [ ] Add job ID naming standardisation (e.g. stat_match_7041)
- [ ] Add manual job trigger from admin panel

---
