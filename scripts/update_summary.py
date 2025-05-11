# scripts/update_summary.py

import sqlite3
from db.import_to_db import update_scrape_summary

def main():
    conn = sqlite3.connect("data/afl_players.db")
    match_id = 7043
    update_scrape_summary(conn, match_id)
    conn.close()

if __name__ == "__main__":
    main()
