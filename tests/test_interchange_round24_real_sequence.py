"""Production persistence exercised against a real multi-poll Round 24
matchInterchange sequence (Issue #204 / PR #206).

Unlike tests/test_match_interchange_production.py's synthetic multi-poll
payloads, every fixture this module reads is real captured evidence for
CD_M20260142409 (see the companion
CD_M20260142409_round_trip_and_postgame_freeze.metadata.json for full
provenance): three verbatim raw matchInterchange responses spanning a real
appear -> disappear -> reappear cycle for a named Champion Data player, and
two reconstructed-from-real-fields responses spanning the first and last
POSTGAME poll captured for this match, which are field-for-field identical.

This closes the two residual caveats documented in
afl_json/match_interchange.py's "Array-membership semantics" docstring
section at the time this module was written: individual player round-trip
citation, and POSTGAME behaviour (for POSTGAME specifically -- CONCLUDED
remains unverified, since this match's capture never reached it).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from afl_json.match_interchange import (
    EVENT_APPEARED,
    EVENT_DISAPPEARED,
    current_state_rows,
    event_rows,
    parse_match_interchange,
    persist_match_interchange,
)
from db.migration_runner import migrate_database

FIXTURES = Path(__file__).parent / "fixtures" / "afl" / "interchange"

MATCH_ID = 8241  # matches the real internal match_id recorded alongside this match_provider_id in the source evidence
MATCH_PROVIDER_ID = "CD_M20260142409"

# Real observed_at timestamps and statuses, taken from the source evidence rows.
POLL_002 = ("match_interchange_CD_M20260142409_poll002_appeared.json", "2026-08-23T09:20:29.030394+00:00", "LIVE")
POLL_048 = ("match_interchange_CD_M20260142409_poll048_disappeared.json", "2026-08-23T09:31:59.008947+00:00", "LIVE")
POLL_100 = ("match_interchange_CD_M20260142409_poll100_reappeared.json", "2026-08-23T09:44:59.008233+00:00", "LIVE")
POSTGAME_654 = ("match_interchange_CD_M20260142409_postgame_poll654.json", "2026-08-23T12:03:59.018988+00:00", "POSTGAME")
POSTGAME_693 = ("match_interchange_CD_M20260142409_postgame_poll693.json", "2026-08-23T12:13:44.011925+00:00", "POSTGAME")

TARGET_PLAYER_PROVIDER_ID = "CD_I1028561"  # Tom Gross, home side (CD_T150)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _persist_poll(conn, name: str, observed_at: str, status: str) -> dict:
    parsed = parse_match_interchange(
        _fixture(name), match_id=MATCH_ID, match_provider_id=MATCH_PROVIDER_ID,
        observed_at=observed_at, match_status_at_poll=status,
    )
    return persist_match_interchange(conn, parsed)


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
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id, scraped_at) VALUES(1,'R24',73,1,?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, home_team, away_team, venue, status, "
        "start_time_utc, season_id, scraped_at) VALUES(?,?,1,'A','B','V','LIVE',?,73,?)",
        (MATCH_ID, MATCH_PROVIDER_ID, POLL_002[1], POLL_002[1]),
    )
    conn.commit()
    yield conn, path
    conn.close()


def test_real_fixtures_have_the_documented_membership_at_each_poll():
    """Sanity-checks the fixtures themselves match the metadata's claims,
    independent of persist_match_interchange, before relying on them."""
    for name, expect_present in (
        (POLL_002[0], True), (POLL_048[0], False), (POLL_100[0], True),
    ):
        payload = _fixture(name)
        home_ids = {e["player"]["playerId"] for e in payload["homeInterchange"]}
        assert (TARGET_PLAYER_PROVIDER_ID in home_ids) is expect_present


def test_real_appear_disappear_reappear_cycle_for_a_named_player(db):
    """CD_I1028561 (Tom Gross) genuinely appears, disappears, and reappears
    in CD_M20260142409's real homeInterchange[] -- not a synthetic payload."""
    conn, _ = db
    name, observed_at, status = POLL_002
    result = _persist_poll(conn, name, observed_at, status)
    conn.commit()
    appeared_ids = {e["player_provider_id"] for e in result["appeared"]}
    assert TARGET_PLAYER_PROVIDER_ID in appeared_ids

    row = next(
        r for r in current_state_rows(conn, match_id=MATCH_ID)
        if r["player_provider_id"] == TARGET_PLAYER_PROVIDER_ID
    )
    assert row["on_bench"] is True
    assert row["side"] == "home"

    name, observed_at, status = POLL_048
    result = _persist_poll(conn, name, observed_at, status)
    conn.commit()
    disappeared_ids = {e["player_provider_id"] for e in result["disappeared"]}
    assert TARGET_PLAYER_PROVIDER_ID in disappeared_ids

    row = next(
        r for r in current_state_rows(conn, match_id=MATCH_ID)
        if r["player_provider_id"] == TARGET_PLAYER_PROVIDER_ID
    )
    assert row["on_bench"] is False

    name, observed_at, status = POLL_100
    result = _persist_poll(conn, name, observed_at, status)
    conn.commit()
    reappeared_ids = {e["player_provider_id"] for e in result["appeared"]}
    assert TARGET_PLAYER_PROVIDER_ID in reappeared_ids

    row = next(
        r for r in current_state_rows(conn, match_id=MATCH_ID)
        if r["player_provider_id"] == TARGET_PLAYER_PROVIDER_ID
    )
    assert row["on_bench"] is True

    # The full real event history for this one player: appeared, disappeared, appeared again.
    player_events = [
        e for e in event_rows(conn, match_id=MATCH_ID)
        if e["player_provider_id"] == TARGET_PLAYER_PROVIDER_ID
    ]
    assert [e["event_type"] for e in player_events] == [EVENT_APPEARED, EVENT_DISAPPEARED, EVENT_APPEARED]


def test_real_postgame_polls_are_idempotent_no_new_events_across_ten_minutes(db):
    """The real POSTGAME payloads at poll_sequence 654 and 693 (~10 minutes
    apart) are field-for-field identical -- persisting both must not create
    any new transition events, exactly like a repeated identical poll."""
    conn, _ = db
    name, observed_at, status = POSTGAME_654
    first = _persist_poll(conn, name, observed_at, status)
    conn.commit()
    assert first["outcome"] == "success"
    state_after_first = {
        r["player_provider_id"]: dict(r) for r in current_state_rows(conn, match_id=MATCH_ID)
    }

    name, observed_at, status = POSTGAME_693
    second = _persist_poll(conn, name, observed_at, status)
    conn.commit()
    assert second["appeared"] == []
    assert second["disappeared"] == []
    assert second["changed"] == []

    state_after_second = {
        r["player_provider_id"]: dict(r) for r in current_state_rows(conn, match_id=MATCH_ID)
    }
    assert set(state_after_first) == set(state_after_second)
    for player_id, row in state_after_second.items():
        assert row["on_bench"] == state_after_first[player_id]["on_bench"]
        assert row["interchange_count"] == state_after_first[player_id]["interchange_count"]
        assert row["time_on_ground"] == state_after_first[player_id]["time_on_ground"]
        assert row["time_on_bench"] == state_after_first[player_id]["time_on_bench"]

    # observed_at (last_observed_at) is the only thing that legitimately
    # advances -- refreshed bookkeeping, not a state change.
    assert state_after_second[next(iter(state_after_second))]["last_observed_at"] == POSTGAME_693[1]
