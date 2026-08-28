"""Service-layer tests for the Admin AFL Data Explorer reporter (Issue #226).

Exercises ``operations.data_explorer.DataExplorerReporter`` directly (no
HTTP/templating), covering the season/round/match/player hierarchy and the
completeness-state distinctions the issue calls out: complete, partial,
missing (should exist but doesn't), and upcoming (not yet expected).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from afl_json.match_data_exceptions import review_stats_not_expected, revoke_stats_not_expected
from afl_json.season_report import ReportStatus
from db.migration_runner import migrate_database
from operations.dashboard import HealthState
from operations.data_explorer import DataExplorerReporter

NOW = datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)


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
        "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) "
        "VALUES(?,?,?,?,?)",
        (player_id, "afl", str(player_id), now, now),
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


def _seed_roster(conn, match_id, provider_id, side, team_provider, player_id):
    now = NOW.isoformat()
    conn.execute(
        "INSERT INTO cfs_match_rosters(match_id,match_provider_id,round_provider_id,team_provider_id,"
        "canonical_team_id,side,team_status,match_status_at_observation,source_last_updated,"
        "first_observed_at,last_observed_at,collector_version) "
        "VALUES(?,?,'CD_R1',?,?,?,'CONFIRMED','CONCLUDED',?,?,?,'test')",
        (match_id, provider_id, team_provider, 10 if side == "home" else 11, side, now, now, now),
    )
    conn.execute(
        "INSERT INTO cfs_match_roster_selections(match_id,match_provider_id,team_provider_id,"
        "canonical_team_id,side,player_provider_id,canonical_player_id,position,jumper_number,captain,"
        "first_observed_at,last_observed_at,collector_version) "
        "VALUES(?,?,?,?,?,?,?,'FORWARDS',7,1,?,?,'test')",
        (match_id, provider_id, team_provider, 10 if side == "home" else 11, side,
         f"CD_P{player_id}", player_id, now, now),
    )


def _seed_commentary(conn, match_id, provider_id, player_id, team_provider):
    now = NOW.isoformat()
    conn.execute(
        "INSERT INTO match_commentary_events(match_id,match_provider_id,event_fingerprint,slot_key,"
        "period_number,period_seconds,comment,score_event,player_provider_id,canonical_player_id,"
        "team_provider_id,canonical_team_id,source_index,first_observed_at,last_observed_at,"
        "raw_event_json,collector_version) "
        "VALUES(?,?,'fp1','slot1',1,30,'Great goal!',1,?,?,?,10,0,?,?,'{}','test')",
        (match_id, provider_id, f"CD_P{player_id}", player_id, team_provider, now, now),
    )


def _seed_interchange(conn, match_id, provider_id, player_id, team_provider, side):
    now = NOW.isoformat()
    conn.execute(
        "INSERT INTO match_interchange_state(match_id,match_provider_id,player_provider_id,"
        "canonical_player_id,team_provider_id,canonical_team_id,side,on_bench,interchange_count,"
        "first_observed_at,last_observed_at,last_transition_at,collector_version) "
        "VALUES(?,?,?,?,?,?,?,0,2,?,?,?,'test')",
        (match_id, provider_id, f"CD_P{player_id}", player_id, team_provider,
         10 if side == "home" else 11, side, now, now, now),
    )


def _seed_full_stats_coverage(conn, provider_id):
    """20 players per side -- clears MIN_CONCLUDED_AUTHORITATIVE_PLAYER_ROWS (season_report.py)."""
    for player_id in range(1, 21):
        _seed_player(conn, player_id, f"Home Player {player_id}", 10)
        _seed_stats(conn, provider_id, player_id, "home", "CD_T1")
    for player_id in range(21, 41):
        _seed_player(conn, player_id, f"Away Player {player_id}", 11)
        _seed_stats(conn, provider_id, player_id, "away", "CD_T2")


def _connect(tmp_path):
    path = tmp_path / "explorer.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _reporter(conn):
    return DataExplorerReporter(conn, clock=lambda: NOW, database="explorer.db")


def test_list_seasons_reuses_season_completeness_status(tmp_path):
    conn = _connect(tmp_path)
    _seed_base(conn)
    _insert_match(conn, 8001, "CD_M1", "CONCLUDED", "2026-03-01T00:00:00+00:00")
    conn.commit()

    seasons = _reporter(conn).list_seasons()

    assert len(seasons) == 1
    assert seasons[0].year == 2026
    assert seasons[0].round_count == 1
    assert seasons[0].match_count == 1
    assert seasons[0].team_count == 2
    # Reused directly from SeasonCompletenessReporter -- concluded match with
    # no stats is a warning-level finding, not a fabricated second state.
    assert seasons[0].status == ReportStatus.INCOMPLETE.value


def test_season_detail_unknown_season_returns_none(tmp_path):
    conn = _connect(tmp_path)
    _seed_base(conn)
    conn.commit()

    assert _reporter(conn).season_detail(999) is None


def test_season_detail_distinguishes_upcoming_round_from_findings(tmp_path):
    conn = _connect(tmp_path)
    _seed_base(conn)
    _insert_match(conn, 8001, "CD_M1", "SCHEDULED", "2026-12-01T00:00:00+00:00")
    conn.commit()

    detail = _reporter(conn).season_detail(85)

    assert detail.year == 2026
    assert len(detail.rounds) == 1
    round_item = detail.rounds[0]
    assert round_item.scheduled_count == 1
    assert round_item.state == HealthState.UPCOMING
    assert "not yet expected" in round_item.state_summary.lower()


def test_season_detail_unknown_lifecycle_round_is_not_reported_complete(tmp_path):
    """A round whose only match has an unrecognised (e.g. placeholder) status must
    never read as "Complete" just because no findings exist yet -- there is simply
    no lifecycle evidence to judge it by (regression: PR #228 review)."""
    conn = _connect(tmp_path)
    _seed_base(conn)
    _insert_match(conn, 8001, "CD_M1", "PLACEHOLDER", "2026-03-01T00:00:00+00:00")
    conn.commit()

    detail = _reporter(conn).season_detail(85)

    round_item = detail.rounds[0]
    assert round_item.concluded_count == 0
    assert round_item.live_count == 0
    assert round_item.scheduled_count == 0
    assert round_item.state == HealthState.UNKNOWN
    assert "could not be recognised" in round_item.state_summary.lower()


def test_fixture_dataset_state_flags_unresolved_team_identity(tmp_path):
    """A non-null home_team_id/away_team_id that fails to resolve to a canonical
    team must not be reported as a complete fixture (regression: PR #228 review)."""
    conn = _connect(tmp_path)
    _seed_base(conn)
    conn.execute(
        "INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,status,"
        "start_time_utc,season_id,home_team_id,away_team_id) "
        "VALUES(8001,'CD_M1',101,'A','B','MCG','CONCLUDED','2026-03-01T00:00:00+00:00',85,999,11)"
    )
    conn.commit()

    detail = _reporter(conn).match_detail(8001)

    fixture = next(d for d in detail.datasets if d.key == "fixture")
    assert fixture.state == HealthState.ATTENTION
    assert "unresolved" in fixture.summary.lower()
    assert detail.home.name is None


def test_match_detail_stats_distinguishes_suspicious_coverage_from_one_sided(tmp_path):
    """A two-sided, uniformly-final snapshot with too few rows is suspiciously low
    coverage, distinct from a genuinely mixed-authority/one-sided snapshot
    (regression: PR #228 review)."""
    conn = _connect(tmp_path)
    _seed_base(conn)

    _insert_match(conn, 8001, "CD_M1", "CONCLUDED", "2026-03-01T00:00:00+00:00")
    _seed_player(conn, 1, "Home Player", 10)
    _seed_player(conn, 2, "Away Player", 11)
    _seed_stats(conn, "CD_M1", 1, "home", "CD_T1")
    _seed_stats(conn, "CD_M1", 2, "away", "CD_T2")

    _insert_match(conn, 8002, "CD_M2", "CONCLUDED", "2026-03-01T00:00:00+00:00")
    for player_id in range(3, 23):
        _seed_player(conn, player_id, f"Home Only {player_id}", 10)
        _seed_stats(conn, "CD_M2", player_id, "home", "CD_T1")
    conn.commit()

    low_coverage = _reporter(conn).match_detail(8001)
    low_coverage_stats = next(d for d in low_coverage.datasets if d.key == "player_statistics")
    assert low_coverage_stats.state == HealthState.PARTIAL
    assert "suspiciously low coverage" in low_coverage_stats.summary.lower()
    assert "mixed-authority" not in low_coverage_stats.summary.lower()

    one_sided = _reporter(conn).match_detail(8002)
    one_sided_stats = next(d for d in one_sided.datasets if d.key == "player_statistics")
    assert one_sided_stats.state == HealthState.PARTIAL
    assert "mixed-authority or one-sided" in one_sided_stats.summary.lower()


def test_match_detail_commentary_preview_is_bounded_ordered_and_totalled(tmp_path):
    """The commentary preview must stay bounded to the most recent
    COMMENTARY_PREVIEW_LIMIT events -- never hydrate the whole match history
    (regression: PR #228 review)."""
    conn = _connect(tmp_path)
    _seed_base(conn)
    _insert_match(conn, 8001, "CD_M1", "LIVE", "2026-03-01T00:00:00+00:00")
    now = NOW.isoformat()
    for period_number in range(1, 26):
        conn.execute(
            "INSERT INTO match_commentary_events(match_id,match_provider_id,event_fingerprint,slot_key,"
            "period_number,period_seconds,comment,score_event,source_index,first_observed_at,"
            "last_observed_at,raw_event_json,collector_version) "
            "VALUES(8001,'CD_M1',?,?,?,0,?,0,0,?,?,'{}','test')",
            (f"fp{period_number}", f"slot{period_number}", period_number,
             f"Event {period_number}", now, now),
        )
    conn.commit()

    detail = _reporter(conn).match_detail(8001)

    assert detail.commentary_total_count == 25
    assert len(detail.commentary_events) == 20
    # Newest-first: the highest period_number (most recently persisted) comes first.
    assert detail.commentary_events[0].period_number == 25
    assert detail.commentary_events[-1].period_number == 6


def test_round_detail_full_match_is_healthy(tmp_path):
    conn = _connect(tmp_path)
    _seed_base(conn)
    _insert_match(conn, 8001, "CD_M1", "CONCLUDED", "2026-03-01T00:00:00+00:00")
    _seed_full_stats_coverage(conn, "CD_M1")
    _seed_roster(conn, 8001, "CD_M1", "home", "CD_T1", 1)
    _seed_roster(conn, 8001, "CD_M1", "away", "CD_T2", 21)
    _seed_commentary(conn, 8001, "CD_M1", 1, "CD_T1")
    _seed_interchange(conn, 8001, "CD_M1", 1, "CD_T1", "home")
    conn.commit()

    detail = _reporter(conn).round_detail(85, 101)

    assert detail is not None
    assert detail.label == "Round 1"
    assert len(detail.matches) == 1
    match_item = detail.matches[0]
    assert match_item.home.name == "Team 10"
    assert match_item.away.name == "Team 11"
    assert match_item.state == HealthState.HEALTHY


def test_round_detail_unknown_round_returns_none(tmp_path):
    conn = _connect(tmp_path)
    _seed_base(conn)
    conn.commit()

    assert _reporter(conn).round_detail(85, 404) is None


def test_match_detail_missing_stats_state_for_concluded_match(tmp_path):
    """A concluded match with no player-statistics snapshot is MISSING, not UPCOMING."""
    conn = _connect(tmp_path)
    _seed_base(conn)
    _insert_match(conn, 8001, "CD_M1", "CONCLUDED", "2026-03-01T00:00:00+00:00")
    conn.commit()

    detail = _reporter(conn).match_detail(8001)

    stats = next(d for d in detail.datasets if d.key == "player_statistics")
    assert stats.state == HealthState.MISSING
    assert "no authoritative player-statistics snapshot" in stats.summary.lower()


def test_match_detail_upcoming_match_is_not_yet_expected(tmp_path):
    conn = _connect(tmp_path)
    _seed_base(conn)
    _insert_match(conn, 8001, "CD_M1", "SCHEDULED", "2026-12-01T00:00:00+00:00")
    conn.commit()

    detail = _reporter(conn).match_detail(8001)

    stats = next(d for d in detail.datasets if d.key == "player_statistics")
    assert stats.state == HealthState.UPCOMING
    assert "not yet expected" in stats.summary.lower()
    commentary = next(d for d in detail.datasets if d.key == "commentary")
    assert commentary.state == HealthState.UPCOMING
    assert detail.overall_state == HealthState.UPCOMING


def test_match_detail_partial_stats_state(tmp_path):
    conn = _connect(tmp_path)
    _seed_base(conn)
    _insert_match(conn, 8001, "CD_M1", "CONCLUDED", "2026-03-01T00:00:00+00:00")
    _seed_player(conn, 1, "Home Player", 10)
    _seed_stats(conn, "CD_M1", 1, "home", "CD_T1")
    conn.commit()

    detail = _reporter(conn).match_detail(8001)

    stats = next(d for d in detail.datasets if d.key == "player_statistics")
    assert stats.state == HealthState.PARTIAL
    assert stats.count == 1


def test_match_detail_full_dataset_is_complete_and_populates_views(tmp_path):
    conn = _connect(tmp_path)
    _seed_base(conn)
    _insert_match(conn, 8001, "CD_M1", "CONCLUDED", "2026-03-01T00:00:00+00:00")
    _seed_full_stats_coverage(conn, "CD_M1")
    _seed_roster(conn, 8001, "CD_M1", "home", "CD_T1", 1)
    _seed_roster(conn, 8001, "CD_M1", "away", "CD_T2", 21)
    _seed_commentary(conn, 8001, "CD_M1", 1, "CD_T1")
    _seed_interchange(conn, 8001, "CD_M1", 1, "CD_T1", "home")
    conn.commit()

    detail = _reporter(conn).match_detail(8001)

    assert detail.home.name == "Team 10"
    assert detail.away.name == "Team 11"
    assert len(detail.player_stats) == 40
    by_side = {row.side for row in detail.player_stats}
    assert by_side == {"home", "away"}
    assert detail.rosters["home"] is not None
    assert detail.rosters["home"].selections[0].player.display_name == "Home Player 1"
    assert detail.commentary_total_count == 1
    assert detail.commentary_events[0].comment == "Great goal!"
    assert len(detail.interchanges) == 1
    stats_state = next(d for d in detail.datasets if d.key == "player_statistics")
    assert stats_state.state == HealthState.HEALTHY
    assert detail.overall_state == HealthState.HEALTHY
    assert detail.provider_evidence.match_provider_id == "CD_M1"


def test_match_detail_unknown_match_returns_none(tmp_path):
    conn = _connect(tmp_path)
    _seed_base(conn)
    conn.commit()

    assert _reporter(conn).match_detail(404) is None


def test_match_detail_never_calls_upstream(tmp_path, monkeypatch):
    """Explorer routes are read-only: building a match detail must never touch the network."""
    import httpx

    def _boom(*args, **kwargs):
        raise AssertionError("Data Explorer must never make an upstream HTTP call")

    monkeypatch.setattr(httpx, "get", _boom)
    monkeypatch.setattr(httpx, "post", _boom)

    conn = _connect(tmp_path)
    _seed_base(conn)
    _insert_match(conn, 8001, "CD_M1", "CONCLUDED", "2026-03-01T00:00:00+00:00")
    conn.commit()

    detail = _reporter(conn).match_detail(8001)
    assert detail is not None


def test_player_detail(tmp_path):
    conn = _connect(tmp_path)
    _seed_base(conn)
    _insert_match(conn, 8001, "CD_M1", "CONCLUDED", "2026-03-01T00:00:00+00:00")
    _seed_player(conn, 1, "Home Player", 10)
    _seed_stats(conn, "CD_M1", 1, "home", "CD_T1")
    conn.commit()

    detail = _reporter(conn).player_detail(1)

    assert detail is not None
    assert detail.display_name == "Home Player"
    assert detail.champion_data_player_id == "CD_P1"
    assert detail.afl_player_id == 1
    assert detail.current_team.name == "Team 10"
    assert len(detail.seasons) == 1
    assert detail.seasons[0].year == 2026
    assert len(detail.recent_matches) == 1
    assert detail.recent_matches[0].match_id == 8001
    assert detail.recent_matches[0].opponent.name == "Team 11"


def test_player_detail_unknown_player_returns_none(tmp_path):
    conn = _connect(tmp_path)
    _seed_base(conn)
    conn.commit()

    assert _reporter(conn).player_detail(999) is None


# -- reviewed stats_not_expected exceptions (Issue #233, building on #231/#232) --

def _seed_statless_concluded_match(conn, match_id, provider_id, *, score_home=0, score_away=0,
                                   with_rosters=True, with_commentary=True, with_interchange=True):
    """A concluded match with no authoritative CFS player-stat rows -- the
    real-world shape of the reviewed match 847 (Issue #231): CONCLUDED,
    0-0, no player statistics ever collected. Rosters/commentary/interchange
    are seeded by default so tests can isolate the player-statistics dataset
    from unrelated completeness rules."""
    _seed_base(conn)
    conn.execute(
        "INSERT INTO matches(match_id,match_provider_id,round_id,home_team,away_team,venue,status,"
        "start_time_utc,season_id,home_team_id,away_team_id,score_home,score_away) "
        "VALUES(?,?,101,'A','B','MCG','CONCLUDED','2015-07-04T00:00:00+00:00',85,10,11,?,?)",
        (match_id, provider_id, score_home, score_away),
    )
    _seed_player(conn, 1, "Home Player", 10)
    _seed_player(conn, 21, "Away Player", 11)
    if with_rosters:
        _seed_roster(conn, match_id, provider_id, "home", "CD_T1", 1)
        _seed_roster(conn, match_id, provider_id, "away", "CD_T2", 21)
    if with_commentary:
        _seed_commentary(conn, match_id, provider_id, 1, "CD_T1")
    if with_interchange:
        _seed_interchange(conn, match_id, provider_id, 1, "CD_T1", "home")


def test_match_detail_active_stats_not_expected_exception_is_reviewed_not_missing(tmp_path):
    """Regression: Issue #233 / real-world match 847 (Issue #231). An active
    stats_not_expected review must present player statistics as REVIEWED --
    never MISSING -- expose the reason code, display reason and evidence, and
    never fabricate player-stat rows. Score, lifecycle and unrelated datasets
    are untouched."""
    conn = _connect(tmp_path)
    _seed_statless_concluded_match(conn, 847, "CD_M20150141408")
    conn.commit()
    review_stats_not_expected(
        conn, match_id=847, reason_code="abandoned",
        display_reason="Match abandoned and not played.",
        evidence_url="https://www.afl.com.au/news/197577/crows-clash-with-geelong-abandoned-remainder-of-round-14-to-go-ahead",
        evidence_note="Bruno confirms play was suspended and never resumed.",
        actor="operator", clock=lambda: NOW,
    )
    conn.commit()

    detail = _reporter(conn).match_detail(847)

    stats = next(d for d in detail.datasets if d.key == "player_statistics")
    assert stats.state == HealthState.REVIEWED
    assert stats.reviewed_exception is not None
    assert stats.reviewed_exception.reason_code == "abandoned"
    assert stats.reviewed_exception.display_reason == "Match abandoned and not played."
    assert stats.reviewed_exception.evidence_url.startswith("https://www.afl.com.au/")
    assert stats.reviewed_exception.evidence_note == "Bruno confirms play was suspended and never resumed."
    assert detail.player_stats == []
    assert detail.lifecycle == "CONCLUDED"
    assert detail.score_home == 0 and detail.score_away == 0
    assert detail.overall_state != HealthState.MISSING


def test_match_detail_statless_concluded_match_without_review_is_missing(tmp_path):
    """The ordinary (unreviewed) case must be unaffected: ``847`` without a
    recorded review stays actionable, exactly as before Issue #233."""
    conn = _connect(tmp_path)
    _seed_statless_concluded_match(conn, 847, "CD_M20150141408")
    conn.commit()

    detail = _reporter(conn).match_detail(847)

    stats = next(d for d in detail.datasets if d.key == "player_statistics")
    assert stats.state == HealthState.MISSING
    assert stats.reviewed_exception is None


def test_match_detail_revoked_stats_exception_restores_missing_interpretation(tmp_path):
    """Revoking the review must immediately restore the ordinary missing/
    actionable interpretation -- the reviewed disposition never permanently
    reclassifies the match."""
    conn = _connect(tmp_path)
    _seed_statless_concluded_match(conn, 847, "CD_M20150141408")
    conn.commit()
    review_stats_not_expected(
        conn, match_id=847, reason_code="abandoned",
        display_reason="Match abandoned and not played.", actor="operator", clock=lambda: NOW,
    )
    conn.commit()
    pre_revoke = _reporter(conn).match_detail(847)
    assert next(d for d in pre_revoke.datasets if d.key == "player_statistics").state == HealthState.REVIEWED

    revoke_stats_not_expected(conn, match_id=847, actor="operator", clock=lambda: NOW)
    conn.commit()

    detail = _reporter(conn).match_detail(847)
    stats = next(d for d in detail.datasets if d.key == "player_statistics")
    assert stats.state == HealthState.MISSING
    assert stats.reviewed_exception is None


def test_match_detail_reviewed_stats_exception_does_not_suppress_unrelated_warnings(tmp_path):
    """The reviewed disposition must not generalise into suppressing every
    missing-dataset warning: rosters/commentary/interchange keep their
    ordinary completeness rule and still surface as attention-needed."""
    conn = _connect(tmp_path)
    _seed_statless_concluded_match(conn, 847, "CD_M20150141408",
                                   with_rosters=False, with_commentary=False, with_interchange=False)
    conn.commit()
    review_stats_not_expected(
        conn, match_id=847, reason_code="abandoned",
        display_reason="Match abandoned and not played.", actor="operator", clock=lambda: NOW,
    )
    conn.commit()

    detail = _reporter(conn).match_detail(847)

    stats = next(d for d in detail.datasets if d.key == "player_statistics")
    assert stats.state == HealthState.REVIEWED
    rosters = next(d for d in detail.datasets if d.key == "rosters")
    commentary = next(d for d in detail.datasets if d.key == "commentary")
    interchange = next(d for d in detail.datasets if d.key == "interchange")
    assert rosters.state == HealthState.ATTENTION
    assert commentary.state == HealthState.ATTENTION
    assert interchange.state == HealthState.ATTENTION
    # The worst unrelated dataset still drives the overall badge -- reviewing
    # the stats exception never hides an otherwise-genuine gap.
    assert detail.overall_state == HealthState.ATTENTION


def test_match_detail_reviewed_stats_exception_preserves_historical_scrape_evidence(tmp_path):
    """A previous partial/failed collection attempt remains visible historical
    audit evidence -- the reviewed disposition only changes how the current
    absence is interpreted, it never invalidates prior evidence."""
    conn = _connect(tmp_path)
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

    detail = _reporter(conn).match_detail(847)

    assert detail.provider_evidence.latest_scrape_status == "partial"
    assert detail.provider_evidence.latest_scrape_rows_written == 0
    stats = next(d for d in detail.datasets if d.key == "player_statistics")
    assert stats.state == HealthState.REVIEWED


def test_match_detail_authoritative_stats_present_ignore_stale_exception(tmp_path):
    """If authoritative stats are actually present, a (now stale) active
    review must not be presented as REVIEWED -- real data takes precedence
    and the ordinary finality rule applies, exactly as for any other match."""
    conn = _connect(tmp_path)
    _seed_base(conn)
    _insert_match(conn, 8001, "CD_M1", "CONCLUDED", "2026-03-01T00:00:00+00:00")
    _seed_full_stats_coverage(conn, "CD_M1")
    conn.commit()
    review_stats_not_expected(
        conn, match_id=8001, reason_code="other",
        display_reason="Stale review.", actor="operator", clock=lambda: NOW,
    )
    conn.commit()

    detail = _reporter(conn).match_detail(8001)

    stats = next(d for d in detail.datasets if d.key == "player_statistics")
    assert stats.state == HealthState.HEALTHY
    assert len(detail.player_stats) == 40
