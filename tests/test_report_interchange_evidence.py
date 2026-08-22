"""Offline tests for scripts/report_interchange_evidence.py.

Focuses on the report's default suppression of noisy, continuously-changing
timeOnGround/timeOnBench-only transitions, and correlation-friendly output
(match_status_at_poll, observed_at) -- never talks to AFL/CFS.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from collection.match_interchange_evidence import (
    TRANSITION_FIRST_OBSERVATION,
    TRANSITION_PLAYER_INTERCHANGE_COUNT_CHANGED,
    TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED,
    parse_match_interchange,
    persist_observation,
)
from db.migration_runner import migrate_database
from scripts.report_interchange_evidence import main as report_main

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


def _player(player_id, *, count=1, tog=100):
    return {
        "teamId": "CD_T10",
        "player": {"playerId": player_id, "playerName": {"givenName": "A", "surname": "B"}, "captain": False, "playerJumperNumber": 1},
        "interchangeCount": count, "benchReason": "ROTATION", "timeOnGround": tog, "timeOnBench": 10, "powerRating": 3,
    }


def _payload(home):
    return {
        "matchId": "CD_M1", "homeInterchange": home, "awayInterchange": [],
        "homeInterchangeCounts": {"totalInterchangeCount": 0.0, "interchangeCap": 75.0,
                                   "interchangeCountQ1": 0.0, "interchangeCountQ2": 0.0,
                                   "interchangeCountQ3": 0.0, "interchangeCountQ4": 0.0},
        "awayInterchangeCounts": {"totalInterchangeCount": 0.0, "interchangeCap": 75.0,
                                   "interchangeCountQ1": 0.0, "interchangeCountQ2": 0.0,
                                   "interchangeCountQ3": 0.0, "interchangeCountQ4": 0.0},
    }


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "afl.db"
    monkeypatch.setenv("DB_PATH", str(path))
    import config
    monkeypatch.setattr(config, "DB_PATH", str(path), raising=False)
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, scraped_at) VALUES(1,'R1',73,1,?)",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, "
        "start_time_utc, season_id, scraped_at) VALUES(9001,'CD_M1',1,'A','B','V','LIVE',?,73,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    yield conn, path
    conn.close()


def _seed_three_polls(conn):
    """First observation (meaningful), a noisy-only poll (timeOnGround only),
    then a meaningful count-change poll -- the shape the report must filter."""
    first = parse_match_interchange(
        _payload([_player("CD_I1", count=1, tog=100)]), match_id=9001, match_provider_id="CD_M1",
        observed_at=NOW.isoformat(), match_status_at_poll="LIVE",
    )
    persist_observation(conn, first, [TRANSITION_FIRST_OBSERVATION])

    noisy = parse_match_interchange(
        _payload([_player("CD_I1", count=1, tog=115)]), match_id=9001, match_provider_id="CD_M1",
        observed_at=NOW.isoformat(), match_status_at_poll="LIVE",
    )
    persist_observation(conn, noisy, [TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED])

    meaningful = parse_match_interchange(
        _payload([_player("CD_I1", count=2, tog=115)]), match_id=9001, match_provider_id="CD_M1",
        observed_at=NOW.isoformat(), match_status_at_poll="LIVE",
    )
    persist_observation(conn, meaningful, [TRANSITION_PLAYER_INTERCHANGE_COUNT_CHANGED])
    conn.commit()


def test_report_default_output_suppresses_noisy_transitions(db, capsys):
    conn, _ = db
    _seed_three_polls(conn)
    assert report_main([]) == 0
    out = capsys.readouterr().out
    assert TRANSITION_FIRST_OBSERVATION in out
    assert TRANSITION_PLAYER_INTERCHANGE_COUNT_CHANGED in out
    assert TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED not in out


def test_report_verbose_output_includes_noisy_transitions(db, capsys):
    conn, _ = db
    _seed_three_polls(conn)
    assert report_main(["--verbose"]) == 0
    out = capsys.readouterr().out
    assert TRANSITION_PLAYER_TIME_ON_GROUND_CHANGED in out


def test_report_transitions_only_excludes_noisy_only_poll_from_loaded_rows(db, capsys):
    conn, _ = db
    _seed_three_polls(conn)
    assert report_main(["--transitions-only", "--json"]) == 0
    out = capsys.readouterr().out
    import json
    rows = json.loads(out)
    assert len(rows) == 2  # first_observation + the count-change poll, not the noisy-only one


def test_report_handles_no_evidence_gracefully(db, capsys):
    assert report_main([]) == 0
    out = capsys.readouterr().out
    assert "No match-interchange evidence has been captured yet" in out


def test_report_json_output_is_valid_and_includes_correlation_fields(db, capsys):
    conn, _ = db
    _seed_three_polls(conn)
    assert report_main(["--json"]) == 0
    import json
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["observed_at"] == NOW.isoformat()
    assert rows[0]["match_status_at_poll"] == "LIVE"
