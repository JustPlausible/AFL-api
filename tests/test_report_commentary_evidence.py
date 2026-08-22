"""Offline tests for scripts/report_commentary_evidence.py.

Focuses on the report's default suppression of uncategorised narrative
commentary (the bulk of a real feed), and on quarter markers, score events,
possible edits and endpoint-outcome transitions being easy to inspect --
never talks to AFL/CFS. Since commentary_evidence_events is already
deduplicated, the report never repeats the same event twice regardless of
how many polls observed it.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from collection.match_commentary_evidence import (
    parse_match_commentary,
    persist_observation,
    persist_poll_outcome,
)
from db.migration_runner import migrate_database
from scripts.report_commentary_evidence import main as report_main

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


def _event(*, comment, period_number=1, period_seconds=0, player_id=None, team_id=None, score_event=False):
    return {
        "comment": comment, "periodNumber": period_number, "periodSeconds": period_seconds,
        "playerId": player_id, "teamId": team_id, "scoreEvent": score_event,
    }


def _payload(events):
    return {"matchId": "CD_M1", "lastUpdated": "2026-08-22T03:00:00.000+0000", "commentaryEvent": events}


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


def _seed_representative_events(conn):
    payload = _payload([
        _event(comment="Q1 is now underway.", period_number=1, period_seconds=0),
        _event(comment="Some uncategorised narrative stat blurb.", period_number=1, period_seconds=400),
        _event(comment="GOAL - Crows (Toby Murray)", period_number=1, period_seconds=500,
               player_id="CD_I1", team_id="CD_T10", score_event=True),
        _event(comment="The siren has sounded to end Q1.", period_number=1, period_seconds=1900),
    ])
    observation = parse_match_commentary(
        payload, match_id=9001, match_provider_id="CD_M1", observed_at=NOW.isoformat(), match_status_at_poll="LIVE",
    )
    persist_observation(conn, observation)
    conn.commit()


def test_report_default_output_shows_quarter_markers_and_score_events(db, capsys):
    conn, _ = db
    _seed_representative_events(conn)
    assert report_main([]) == 0
    out = capsys.readouterr().out
    assert "Q1 is now underway." in out
    assert "The siren has sounded to end Q1." in out
    assert "GOAL - Crows (Toby Murray)" in out


def test_report_default_output_suppresses_uncategorised_narrative(db, capsys):
    conn, _ = db
    _seed_representative_events(conn)
    assert report_main([]) == 0
    out = capsys.readouterr().out
    assert "Some uncategorised narrative stat blurb." not in out


def test_report_all_events_includes_uncategorised_narrative(db, capsys):
    conn, _ = db
    _seed_representative_events(conn)
    assert report_main(["--all-events"]) == 0
    out = capsys.readouterr().out
    assert "Some uncategorised narrative stat blurb." in out


def test_report_never_repeats_the_same_event_across_multiple_polls(db, capsys):
    """The default report reflects deduplicated evidence -- an event observed
    on every poll of a long-running capture must still appear exactly once."""
    conn, _ = db
    payload = _payload([_event(comment="Q1 is now underway.", period_number=1, period_seconds=0)])
    for i in range(5):
        observation = parse_match_commentary(
            payload, match_id=9001, match_provider_id="CD_M1",
            observed_at=(NOW + timedelta(seconds=15 * i)).isoformat(), match_status_at_poll="LIVE",
        )
        persist_observation(conn, observation)
        conn.commit()

    assert report_main([]) == 0
    out = capsys.readouterr().out
    assert out.count("Q1 is now underway.") == 1


def test_report_shows_possible_edit_linkage(db, capsys):
    conn, _ = db
    first = parse_match_commentary(
        _payload([_event(comment="GOAL - Crows (Toby Murray)", period_number=1, period_seconds=500,
                          player_id="CD_I1", team_id="CD_T10", score_event=True)]),
        match_id=9001, match_provider_id="CD_M1", observed_at=NOW.isoformat(), match_status_at_poll="LIVE",
    )
    persist_observation(conn, first)
    second = parse_match_commentary(
        _payload([_event(comment="GOAL - Crows (Toby Murray) [corrected]", period_number=1, period_seconds=500,
                          player_id="CD_I1", team_id="CD_T10", score_event=True)]),
        match_id=9001, match_provider_id="CD_M1", observed_at=(NOW + timedelta(seconds=15)).isoformat(),
        match_status_at_poll="LIVE",
    )
    persist_observation(conn, second)
    conn.commit()

    assert report_main([]) == 0
    out = capsys.readouterr().out
    assert "possible edits/changes detected" in out
    assert "possibly replaces event id=" in out


def test_report_shows_endpoint_outcome_transitions(db, capsys):
    conn, _ = db
    persist_poll_outcome(
        conn, match_id=9001, match_provider_id="CD_M1", observed_at=NOW.isoformat(),
        match_status_at_poll="SCHEDULED", outcome="not_published",
    )
    conn.commit()
    persist_observation(conn, parse_match_commentary(
        _payload([_event(comment="Q1 is now underway.", period_number=1, period_seconds=0)]),
        match_id=9001, match_provider_id="CD_M1", observed_at=(NOW + timedelta(seconds=15)).isoformat(),
        match_status_at_poll="LIVE",
    ))
    conn.commit()

    assert report_main([]) == 0
    out = capsys.readouterr().out
    assert "endpoint availability/outcome transitions" in out
    assert "outcome=not_published" in out
    # The recovery poll (not_published -> success) must also be visible,
    # not just the initial failure -- see collection/match_commentary_evidence.py's
    # TRANSITION_OUTCOME_SUCCESS handling.
    assert "outcome=success" in out


def test_report_handles_no_evidence_gracefully(db, capsys):
    assert report_main([]) == 0
    out = capsys.readouterr().out
    assert "No commentary evidence has been captured yet" in out


def test_report_json_output_is_valid_and_includes_correlation_fields(db, capsys):
    conn, _ = db
    _seed_representative_events(conn)
    assert report_main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "polls" in payload and "events" in payload
    assert payload["polls"][0]["observed_at"] == NOW.isoformat()
    score_events = [e for e in payload["events"] if e["category"] == "score_event"]
    assert score_events[0]["period_number"] == 1
    assert score_events[0]["period_seconds"] == 500


def test_report_filters_by_match_provider_id(db, capsys):
    conn, _ = db
    _seed_representative_events(conn)
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, "
        "start_time_utc, season_id, scraped_at) VALUES(9002,'CD_M2',1,'C','D','V','LIVE',?,73,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    persist_observation(conn, parse_match_commentary(
        _payload([_event(comment="Q1 is now underway.", period_number=1, period_seconds=0)]),
        match_id=9002, match_provider_id="CD_M2", observed_at=NOW.isoformat(), match_status_at_poll="LIVE",
    ))
    conn.commit()

    assert report_main(["--match-provider-id", "CD_M2"]) == 0
    out = capsys.readouterr().out
    assert "CD_M2" in out
    assert "CD_M1" not in out
