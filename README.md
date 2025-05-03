# AFL Player Scraper & Enrichment Tool

Scrapes AFL club websites and enriches player data with official AFL IDs, Champion Data IDs, and profile links.

## Features

- 🏉 Scrape club squad lists from official club websites
- 🔍 Resolve player names to AFL.com.au profile IDs
- 📄 Output enriched JSON files (per club)
- 🧠 CLI interface with `--all`, `--skip-existing`, `--scrape`, `--enrich`

## Usage

```bash
# Run full scrape and enrich
python3 src/main.py --all

# Scrape or enrich individually
python3 src/main.py --scrape richmond
python3 src/main.py --enrich richmond
