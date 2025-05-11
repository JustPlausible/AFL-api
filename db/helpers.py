import sqlite3

def get_round_start_times(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """
    Returns a list of (round_id, round_start_utc) for rounds with at least one known match start time.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT round_id, MIN(start_time_utc) as round_start_utc
        FROM matches
        WHERE start_time_utc IS NOT NULL
        GROUP BY round_id
    """)
    return cursor.fetchall()