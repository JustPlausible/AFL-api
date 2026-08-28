"""Admin route/rendering tests for the AFL Data Explorer (Issue #226)."""
from __future__ import annotations

import base64
import importlib
import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import config
from afl_json.match_data_exceptions import review_stats_not_expected, revoke_stats_not_expected
from db.migration_runner import migrate_database

NOW = datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)


def _auth():
    return {"Authorization": "Basic " + base64.b64encode(b"admin:password").decode()}


def _client(tmp_path, monkeypatch, *, seed=None):
    db_path = tmp_path / "afl.db"
    migrate_database(db_path)
    if seed is not None:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        seed(conn)
        conn.close()
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    import admin
    admin = importlib.reload(admin)
    return admin, TestClient(admin.app)


def _seed_base(conn):
    now = NOW.isoformat()
    conn.execute("INSERT INTO afl_competitions VALUES(1,'CD_C014','AFL','AFL','{}','{}',?)", (now,))
    conn.execute(
        "INSERT INTO afl_seasons VALUES(85,'CD_S85',1,'2026','2026',2026,1,1,NULL,NULL,'{}','{}',?)", (now,)
    )
    for team, provider in ((10, "CD_T1"), (11, "CD_T2")):
        conn.execute(
            "INSERT INTO afl_teams VALUES(?,?,?,?,?,?,?,?, '{}','{}','{}',?)",
            (team, provider, f"Team {team}", f"T{team}", provider, provider, provider, "AFL", now),
        )
        conn.execute("INSERT INTO afl_team_seasons VALUES(85,?,?,?)", (team, now, now))
    conn.execute(
        "INSERT INTO rounds(round_id,round_label,season_id,competition_id,provider_id,round_number) "
        "VALUES(101,'Round 1',85,1,'CD_R1',1)"
    )


def _insert_match(conn, match_id, provider_id, status, start_time):
    conn.execute(
        "INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,status,"
        "start_time_utc,season_id,home_team_id,away_team_id) "
        "VALUES(?,?,101,'A','B','MCG',?,?,85,10,11)",
        (match_id, provider_id, status, start_time),
    )


def _seed_player(conn, player_id, name, team_id):
    now = NOW.isoformat()
    conn.execute(
        "INSERT INTO canonical_players VALUES(?,?,?,?,?,?)",
        (player_id, name, name.split()[0], name.split()[-1], now, now),
    )
    conn.execute(
        "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) "
        "VALUES(?,?,?,?,?)",
        (player_id, "champion_data", f"CD_P{player_id}", now, now),
    )
    conn.execute(
        "INSERT INTO competition_season_players(player_id,competition_season_id,team_id,source_provider,"
        "source_json,created_at,updated_at) VALUES(?,85,?,'champion_data','{}',?,?)",
        (player_id, team_id, now, now),
    )


def _seed_stats(conn, provider_id, player_id, side, team_provider, authority=2):
    now = NOW.isoformat()
    conn.execute(
        "INSERT INTO cfs_player_stats(match_provider_id,champion_data_player_id,afl_match_id,"
        "team_provider_id,side,collected_at,source_endpoint,resolved_match_status,snapshot_authority,"
        "extra_stats_json,raw_player_json,canonical_player_id,goals,kicks) "
        "VALUES(?,?,?,?,?,?,'match_player_stats','CONCLUDED',?,'{}','{}',?,3,10)",
        (provider_id, f"CD_P{player_id}", str(player_id), team_provider, side, now, authority, player_id),
    )


def _seed_full_match(conn, match_id=8001, provider_id="CD_M1", status="CONCLUDED",
                     start_time="2026-03-01T00:00:00+00:00"):
    _seed_base(conn)
    _insert_match(conn, match_id, provider_id, status, start_time)
    for player_id in range(1, 21):
        _seed_player(conn, player_id, f"Home Player {player_id}", 10)
        _seed_stats(conn, provider_id, player_id, "home", "CD_T1")
    for player_id in range(21, 41):
        _seed_player(conn, player_id, f"Away Player {player_id}", 11)
        _seed_stats(conn, provider_id, player_id, "away", "CD_T2")

    now = NOW.isoformat()
    for side, team_provider, team_id in (("home", "CD_T1", 10), ("away", "CD_T2", 11)):
        conn.execute(
            "INSERT INTO cfs_match_rosters(match_id,match_provider_id,round_provider_id,team_provider_id,"
            "canonical_team_id,side,team_status,match_status_at_observation,source_last_updated,"
            "first_observed_at,last_observed_at,collector_version) "
            "VALUES(?,?,'CD_R1',?,?,?,'CONFIRMED','CONCLUDED',?,?,?,'test')",
            (match_id, provider_id, team_provider, team_id, side, now, now, now),
        )
    conn.execute(
        "INSERT INTO match_commentary_events(match_id,match_provider_id,event_fingerprint,slot_key,"
        "period_number,period_seconds,comment,score_event,player_provider_id,canonical_player_id,"
        "team_provider_id,canonical_team_id,source_index,first_observed_at,last_observed_at,"
        "raw_event_json,collector_version) "
        "VALUES(?,?,'fp1','slot1',1,30,'Great goal!',1,'CD_P1',1,'CD_T1',10,0,?,?,'{}','test')",
        (match_id, provider_id, now, now),
    )
    conn.execute(
        "INSERT INTO match_interchange_state(match_id,match_provider_id,player_provider_id,"
        "canonical_player_id,team_provider_id,canonical_team_id,side,on_bench,interchange_count,"
        "first_observed_at,last_observed_at,last_transition_at,collector_version) "
        "VALUES(?,?,'CD_P1',1,'CD_T1',10,'home',0,2,?,?,?,'test')",
        (match_id, provider_id, now, now, now),
    )


def _seed_statless_concluded_match(conn, match_id, provider_id, *, score_home=0, score_away=0):
    """A concluded match with no authoritative CFS player-stat rows but full
    rosters/commentary/interchange coverage -- isolates the player-statistics
    dataset for reviewed-exception rendering tests (Issue #233), matching the
    real-world shape of match 847 (Issue #231)."""
    _seed_base(conn)
    conn.execute(
        "INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,status,"
        "start_time_utc,season_id,home_team_id,away_team_id,score_home,score_away) "
        "VALUES(?,?,101,'A','B','MCG','CONCLUDED','2015-07-04T00:00:00+00:00',85,10,11,?,?)",
        (match_id, provider_id, score_home, score_away),
    )
    _seed_player(conn, 1, "Home Player", 10)
    _seed_player(conn, 21, "Away Player", 11)
    now = NOW.isoformat()
    for side, team_provider, team_id in (("home", "CD_T1", 10), ("away", "CD_T2", 11)):
        conn.execute(
            "INSERT INTO cfs_match_rosters(match_id,match_provider_id,round_provider_id,team_provider_id,"
            "canonical_team_id,side,team_status,match_status_at_observation,source_last_updated,"
            "first_observed_at,last_observed_at,collector_version) "
            "VALUES(?,?,'CD_R1',?,?,?,'CONFIRMED','CONCLUDED',?,?,?,'test')",
            (match_id, provider_id, team_provider, team_id, side, now, now, now),
        )
    conn.execute(
        "INSERT INTO match_commentary_events(match_id,match_provider_id,event_fingerprint,slot_key,"
        "period_number,period_seconds,comment,score_event,player_provider_id,canonical_player_id,"
        "team_provider_id,canonical_team_id,source_index,first_observed_at,last_observed_at,"
        "raw_event_json,collector_version) "
        "VALUES(?,?,'fp1','slot1',1,30,'Play stopped.',0,'CD_P1',1,'CD_T1',10,0,?,?,'{}','test')",
        (match_id, provider_id, now, now),
    )
    conn.execute(
        "INSERT INTO match_interchange_state(match_id,match_provider_id,player_provider_id,"
        "canonical_player_id,team_provider_id,canonical_team_id,side,on_bench,interchange_count,"
        "first_observed_at,last_observed_at,last_transition_at,collector_version) "
        "VALUES(?,?,'CD_P1',1,'CD_T1',10,'home',0,0,?,?,?,'test')",
        (match_id, provider_id, now, now, now),
    )


def _no_upstream(monkeypatch, admin):
    def _boom(*args, **kwargs):
        raise AssertionError("Data Explorer routes must never make an upstream HTTP call")
    monkeypatch.setattr(admin.httpx, "get", _boom)
    monkeypatch.setattr(admin.httpx, "post", _boom)


def test_data_explorer_requires_auth(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    response = client.get("/data-explorer")
    assert response.status_code == 401


def test_data_explorer_appears_in_admin_navigation(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    for path in ("/data-explorer", "/tables"):
        response = client.get(path, headers=_auth())
        assert response.status_code == 200
        assert "Data Explorer" in response.text
        assert 'href="/data-explorer"' in response.text


def test_data_explorer_season_list_renders(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch, seed=lambda conn: (_seed_base(conn), conn.commit()))
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer", headers=_auth())

    assert response.status_code == 200
    assert "2026" in response.text
    assert f'href="/data-explorer/seasons/85"' in response.text


def test_data_explorer_season_detail_navigates_to_round(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _insert_match(conn, 8001, "CD_M1", "SCHEDULED", "2026-12-01T00:00:00+00:00")
        conn.commit()

    admin, client = _client(tmp_path, monkeypatch, seed=seed)
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/seasons/85", headers=_auth())

    assert response.status_code == 200
    assert "Round 1" in response.text
    assert 'href="/data-explorer/seasons/85/rounds/101"' in response.text
    assert "Upcoming" in response.text


def test_data_explorer_unknown_season_is_404(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch, seed=lambda conn: (_seed_base(conn), conn.commit()))
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/seasons/999", headers=_auth())

    assert response.status_code == 404


def test_data_explorer_round_detail_lists_matches_and_links_to_match(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch, seed=lambda conn: (_seed_full_match(conn), conn.commit()))
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/seasons/85/rounds/101", headers=_auth())

    assert response.status_code == 200
    assert "Team 10" in response.text
    assert "Team 11" in response.text
    assert 'href="/data-explorer/matches/8001"' in response.text
    assert "Complete" in response.text


def test_data_explorer_unknown_round_is_404(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch, seed=lambda conn: (_seed_base(conn), conn.commit()))
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/seasons/85/rounds/404", headers=_auth())

    assert response.status_code == 404


def test_data_explorer_match_detail_complete_state(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch, seed=lambda conn: (_seed_full_match(conn), conn.commit()))
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/matches/8001", headers=_auth())

    assert response.status_code == 200
    assert "Team 10" in response.text
    assert "Team 11" in response.text
    assert "Overall: Complete" in response.text
    assert 'href="/data-explorer/players/1"' in response.text


def test_data_explorer_match_detail_partial_state(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _insert_match(conn, 8001, "CD_M1", "CONCLUDED", "2026-03-01T00:00:00+00:00")
        _seed_player(conn, 1, "Solo Player", 10)
        _seed_stats(conn, "CD_M1", 1, "home", "CD_T1")
        conn.commit()

    admin, client = _client(tmp_path, monkeypatch, seed=seed)
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/matches/8001", headers=_auth())

    assert response.status_code == 200
    assert "Partial" in response.text


def test_data_explorer_match_detail_missing_state_for_concluded_match(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _insert_match(conn, 8001, "CD_M1", "CONCLUDED", "2026-03-01T00:00:00+00:00")
        conn.commit()

    admin, client = _client(tmp_path, monkeypatch, seed=seed)
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/matches/8001", headers=_auth())

    assert response.status_code == 200
    assert "Missing" in response.text
    assert "no authoritative player-statistics snapshot" in response.text.lower()


def test_data_explorer_match_detail_upcoming_state_not_treated_as_failure(tmp_path, monkeypatch):
    def seed(conn):
        _seed_base(conn)
        _insert_match(conn, 8001, "CD_M1", "SCHEDULED", "2026-12-01T00:00:00+00:00")
        conn.commit()

    admin, client = _client(tmp_path, monkeypatch, seed=seed)
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/matches/8001", headers=_auth())

    assert response.status_code == 200
    assert "Upcoming" in response.text
    assert "not yet expected" in response.text.lower()
    assert "Missing" not in response.text


def test_data_explorer_unknown_match_is_404(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch, seed=lambda conn: (_seed_base(conn), conn.commit()))
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/matches/404", headers=_auth())

    assert response.status_code == 404


def test_data_explorer_player_drilldown_from_match(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch, seed=lambda conn: (_seed_full_match(conn), conn.commit()))
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/players/1", headers=_auth())

    assert response.status_code == 200
    assert "Home Player 1" in response.text
    assert "Team 10" in response.text
    assert 'href="/data-explorer/matches/8001"' in response.text


def test_data_explorer_unknown_player_is_404(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch, seed=lambda conn: (_seed_base(conn), conn.commit()))
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/players/999", headers=_auth())

    assert response.status_code == 404


def test_data_explorer_provider_evidence_rendered_secondary(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch, seed=lambda conn: (_seed_full_match(conn), conn.commit()))
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/matches/8001", headers=_auth())

    assert response.status_code == 200
    assert "Champion Data match ID" in response.text
    assert "CD_M1" in response.text


def test_data_explorer_never_triggers_upstream_requests(tmp_path, monkeypatch):
    """Every explorer route must render purely from persisted data (Issue #226)."""
    admin, client = _client(tmp_path, monkeypatch, seed=lambda conn: (_seed_full_match(conn), conn.commit()))
    _no_upstream(monkeypatch, admin)

    for path in (
        "/data-explorer", "/data-explorer/seasons/85", "/data-explorer/seasons/85/rounds/101",
        "/data-explorer/matches/8001", "/data-explorer/players/1",
    ):
        response = client.get(path, headers=_auth())
        assert response.status_code == 200


# -- reviewed stats_not_expected exceptions (Issue #233, building on #231/#232) --

def test_data_explorer_match_detail_renders_reviewed_stats_exception(tmp_path, monkeypatch):
    """Primary regression case: Issue #233 / real-world match 847 (Issue #231).
    An active stats_not_expected review must render "Not expected (reviewed)"
    with the reason code, display reason and evidence URL, never "Missing",
    while lifecycle and score stay untouched."""
    def seed(conn):
        _seed_statless_concluded_match(conn, 847, "CD_M20150141408")
        conn.commit()
        review_stats_not_expected(
            conn, match_id=847, reason_code="abandoned",
            display_reason="Match abandoned and not played.",
            evidence_url=("https://www.afl.com.au/news/197577/crows-clash-with-geelong-"
                          "abandoned-remainder-of-round-14-to-go-ahead"),
            actor="operator", clock=lambda: NOW,
        )
        conn.commit()

    admin, client = _client(tmp_path, monkeypatch, seed=seed)
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/matches/847", headers=_auth())

    assert response.status_code == 200
    assert "Not expected (reviewed)" in response.text
    assert "abandoned" in response.text
    assert "Match abandoned and not played." in response.text
    assert ("https://www.afl.com.au/news/197577/crows-clash-with-geelong-"
            "abandoned-remainder-of-round-14-to-go-ahead") in response.text
    assert "No authoritative player statistics captured yet" not in response.text
    assert "Missing" not in response.text
    assert "CONCLUDED" in response.text
    assert "0" in response.text  # 0-0 score preserved


def test_data_explorer_round_detail_reviewed_match_is_not_missing(tmp_path, monkeypatch):
    """The round-level match card must also reflect the reviewed disposition,
    not the raw "Missing" state, once reviewed."""
    def seed(conn):
        _seed_statless_concluded_match(conn, 847, "CD_M20150141408")
        conn.commit()
        review_stats_not_expected(
            conn, match_id=847, reason_code="abandoned",
            display_reason="Match abandoned and not played.", actor="operator", clock=lambda: NOW,
        )
        conn.commit()

    admin, client = _client(tmp_path, monkeypatch, seed=seed)
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/seasons/85/rounds/101", headers=_auth())

    assert response.status_code == 200
    assert "Not expected (reviewed)" in response.text
    assert "Missing" not in response.text


def test_data_explorer_match_detail_statless_without_review_still_shows_missing(tmp_path, monkeypatch):
    """Unreviewed concluded/statless matches keep the ordinary "Missing"
    presentation -- only an active review changes it."""
    admin, client = _client(
        tmp_path, monkeypatch,
        seed=lambda conn: (_seed_statless_concluded_match(conn, 847, "CD_M20150141408"), conn.commit()),
    )
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/matches/847", headers=_auth())

    assert response.status_code == 200
    assert "Missing" in response.text
    assert "Not expected (reviewed)" not in response.text


def test_data_explorer_match_detail_revoked_review_restores_missing(tmp_path, monkeypatch):
    """Revoking the review must immediately restore the ordinary missing
    presentation."""
    def seed(conn):
        _seed_statless_concluded_match(conn, 847, "CD_M20150141408")
        conn.commit()
        review_stats_not_expected(
            conn, match_id=847, reason_code="abandoned",
            display_reason="Match abandoned and not played.", actor="operator", clock=lambda: NOW,
        )
        revoke_stats_not_expected(conn, match_id=847, actor="operator", clock=lambda: NOW)
        conn.commit()

    admin, client = _client(tmp_path, monkeypatch, seed=seed)
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/matches/847", headers=_auth())

    assert response.status_code == 200
    assert "Missing" in response.text
    assert "Not expected (reviewed)" not in response.text


def test_data_explorer_match_detail_reviewed_exception_preserves_historical_scrape_evidence(tmp_path, monkeypatch):
    """A previous partial collection run remains visible as historical audit
    evidence even once the absence is reviewed and explained."""
    def seed(conn):
        _seed_statless_concluded_match(conn, 847, "CD_M20150141408")
        conn.execute(
            "INSERT INTO scrape_runs(run_id,scrape_type,target_type,target_identifier,trigger_source,"
            "status,started_at,rows_written) VALUES('r1','season_match_player_stats','match',"
            "'CD_M20150141408','cli','partial',?,0)", (NOW.isoformat(),),
        )
        conn.commit()
        review_stats_not_expected(
            conn, match_id=847, reason_code="abandoned",
            display_reason="Match abandoned and not played.", actor="operator", clock=lambda: NOW,
        )
        conn.commit()

    admin, client = _client(tmp_path, monkeypatch, seed=seed)
    _no_upstream(monkeypatch, admin)

    response = client.get("/data-explorer/matches/847", headers=_auth())

    assert response.status_code == 200
    assert "partial" in response.text
    assert "0 row(s) written" in response.text
    assert "Not expected (reviewed)" in response.text
