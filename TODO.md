# AFL Scraper TODO

This is a planning document to help break down development of the AFL scraping and stat tracking system.

---

## ✅ Setup & Environment

- [x] Initialise Git repo
- [x] Setup virtual environment and requirements.txt
- [x] Add logging to all modules
- [x] Prepare `.env` or config for season/year

---

## ✅ Fixtures Index Scraper

- [x] Scrape `data-season-id`, `data-competition-id`, and `data-round-id` from `.js-react-fixtures`
- [x] Extract list of `round_id` and round names from HTML
- [x] Store season and round info in `rounds` table
- [x] Add CLI command: `cli.py scrape-fixtures-index`

---

## ✅ Match Scraper (per round)

- [x] Given a round ID, fetch fixture page (via Playwright)
- [x] Parse match details: teams (with club code), venue, time, status
- [x] Create or update `matches` table
- [x] Add CLI command: `cli.py scrape-round --round-id=1147`

---

## 🧍‍♂️ Team Lineups Scraper

- [ ] Use `/matches/team-lineups` with round context
- [ ] Parse player names, subs, emergencies
- [ ] Store in `lineups` table with match ID and team
- [ ] Handle AFL player ID if available (Champion Data or scraped)

---

## 🩼 Injury List Scraper

- [x] Scrape from `https://www.afl.com.au/news/injury-list`
- [x] Parse club, player name, injury, return estimate
- [x] Store with timestamp or round context
- [x] Add command: `cli.py scrape-injuries`

---

## ✅ Data Mapping

- [x] Maintain player ID cross-reference table
- [x] Add matching logic for team names → club codes
- [x] Use alias matching (with DB integration)
- [ ] Add fuzzy matching fallback (optional)

---

## 📦 Database & Exports

- [x] Finalise schema (`rounds`, `matches`, `clubs`)
- [x] Export clubs to JSON
- [x] Import clubs from JSON to DB
- [x] Compare clubs.json ↔ DB in admin
- [ ] Export CSV or JSON for Sheets import (matches, injuries, etc.)
- [ ] Optional: Push data to Google Sheets API

---

## 🧪 Future Modules

- [ ] Match stats scraper (disposals, goals, tackles, etc.)
- [ ] Live match polling logic
