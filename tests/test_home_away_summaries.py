import importlib
import sqlite3
from datetime import datetime, timezone

import pytest

from afl_json.home_away_summaries import (SummaryNotReady,
    build_home_and_away_player_summaries, select_home_and_away_matches)


def database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      PRAGMA foreign_keys=ON;
      CREATE TABLE afl_seasons(afl_id INTEGER PRIMARY KEY);
      CREATE TABLE canonical_players(id INTEGER PRIMARY KEY);
      CREATE TABLE afl_teams(afl_id INTEGER PRIMARY KEY,name TEXT);
      CREATE TABLE rounds(round_id INTEGER PRIMARY KEY,season_id INTEGER,round_label TEXT);
      CREATE TABLE matches(match_id INTEGER PRIMARY KEY,match_provider_id TEXT,round_id INTEGER,
        season_id INTEGER,status TEXT);
      CREATE TABLE competition_season_players(player_id INTEGER,competition_season_id INTEGER,
        team_id INTEGER);
      CREATE TABLE player_provider_ids(player_id INTEGER,provider TEXT,provider_player_id TEXT);
      CREATE TABLE cfs_player_stats(id INTEGER PRIMARY KEY,match_provider_id TEXT,
        champion_data_player_id TEXT,canonical_player_id INTEGER,side TEXT,collected_at TEXT,
        snapshot_authority INTEGER,goals NUMERIC,behinds NUMERIC,kicks NUMERIC,handballs NUMERIC,
        disposals NUMERIC,marks NUMERIC,tackles NUMERIC,hitouts NUMERIC);
      CREATE TABLE match_data_exceptions(match_id INTEGER,provider_match_id TEXT,exception_type TEXT,
        reason_code TEXT,display_reason TEXT,evidence_url TEXT,evidence_note TEXT,created_by TEXT,
        created_at TEXT,updated_at TEXT,revoked_at TEXT);
      INSERT INTO afl_seasons VALUES(85);
      INSERT INTO canonical_players VALUES(1),(2);
      INSERT INTO afl_teams VALUES(10,'Historical Team');
      INSERT INTO competition_season_players VALUES(1,85,10),(2,85,10);
      INSERT INTO rounds VALUES(1,85,'Opening Round'),(2,85,'Round 99'),(3,85,'Not named as a final');
      INSERT INTO matches VALUES(101,'CD_M1',1,85,'CONCLUDED'),(102,'CD_M2',2,85,'CONCLUDED'),
        (103,'CD_M3',3,85,'CONCLUDED');
    """)
    importlib.import_module("db.migrations.0028_home_away_player_summaries").migrate(conn)
    conn.execute("UPDATE rounds SET competition_phase='HOME_AND_AWAY' WHERE round_id IN (1,2)")
    conn.execute("UPDATE rounds SET competition_phase='FINALS' WHERE round_id=3")
    return conn


def add_snapshot(conn, match="CD_M1", goals=1, behinds=1, kicks=2):
    for number in range(20):
        conn.execute("INSERT INTO cfs_player_stats VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (None,match,f'CD_I{number}',1 if number == 0 else 999,'home' if number < 10 else 'away',
             '2026-08-31T00:00:00+00:00',2,goals if number == 0 else 0,
             behinds if number == 0 else 0,kicks if number == 0 else 0,0,
             kicks if number == 0 else 0,0,0,0))


def test_semantic_selection_includes_opening_and_nonstandard_numbers_excludes_finals():
    conn = database()
    assert [row["match_id"] for row in select_home_and_away_matches(conn,85)] == [101,102]
    conn.execute("UPDATE rounds SET competition_phase=NULL WHERE round_id=2")
    with pytest.raises(SummaryNotReady, match="competition_phase"):
        select_home_and_away_matches(conn,85)


def test_build_adds_facts_retains_zero_game_members_and_is_idempotent():
    conn = database()
    add_snapshot(conn,"CD_M1",2,1,4); add_snapshot(conn,"CD_M2",1,1,3)
    report = build_home_and_away_player_summaries(conn,85,
        clock=lambda: datetime(2026,9,1,tzinfo=timezone.utc))
    assert (report.inserted,report.players_with_games,report.zero_game_players) == (2,1,1)
    rows = conn.execute("SELECT * FROM derived_player_season_summaries ORDER BY canonical_player_id").fetchall()
    assert rows[0]["games_played"] == 2
    assert '"kicks":7' in rows[0]["totals"] and '"goal_accuracy":60.0' in rows[0]["derived_rates"]
    assert rows[1]["games_played"] == 0 and '"kicks":0' in rows[1]["totals"]
    assert '"goal_accuracy":null' in rows[1]["derived_rates"]
    again = build_home_and_away_player_summaries(conn,85)
    assert (again.inserted,again.updated,again.unchanged) == (0,0,2)


def test_missing_blocks_but_reviewed_exception_allows_and_real_stats_win():
    conn = database(); add_snapshot(conn,"CD_M1")
    with pytest.raises(SummaryNotReady):
        build_home_and_away_player_summaries(conn,85)
    conn.execute("INSERT INTO match_data_exceptions VALUES(102,'CD_M2','stats_not_expected',"
                 "'abandoned','reviewed',NULL,NULL,'operator','now','now',NULL)")
    report = build_home_and_away_player_summaries(conn,85)
    assert report.reviewed_exceptions == 1
    add_snapshot(conn,"CD_M2")
    report = build_home_and_away_player_summaries(conn,85)
    assert report.reviewed_exceptions == 0 and report.authoritative_snapshots == 2
