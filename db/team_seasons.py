"""Canonical queries for team participation in AFL competition seasons."""
from __future__ import annotations

import sqlite3


def count_team_participants(conn: sqlite3.Connection, competition_season_id: int) -> int:
    """Count memberships which resolve to a stable canonical team identity."""
    return conn.execute(
        "SELECT COUNT(*) FROM afl_team_seasons ts "
        "JOIN afl_teams t ON t.afl_id=ts.team_id "
        "WHERE ts.competition_season_id=?",
        (competition_season_id,),
    ).fetchone()[0]
