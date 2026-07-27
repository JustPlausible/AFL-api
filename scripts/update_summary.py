# scripts/update_summary.py

from db.connection import get_db_connection
from db.import_to_db import update_scrape_summary

def main():
    conn = get_db_connection()
    try:
        match_id = 7043
        update_scrape_summary(conn, match_id)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
