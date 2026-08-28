from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from afl_json.match_data_exceptions import (active_stats_exception,
                                            review_stats_not_expected,
                                            revoke_stats_not_expected)
from db.migration_runner import migrate_database


def default_connection(tmp_path):
    path = tmp_path / "exceptions.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    assert conn.row_factory is None
    conn.execute("INSERT INTO matches(match_id,match_provider_id,round_id,status) "
                 "VALUES(847,'CD_M20150141408',1,'CONCLUDED')")
    conn.commit()
    return conn


def test_review_update_replay_and_revoke_support_default_sqlite_rows(tmp_path):
    conn = default_connection(tmp_path)
    created = review_stats_not_expected(
        conn, match_id=847, reason_code="abandoned",
        display_reason="Match abandoned and not played.", actor="operator",
    )
    replayed = review_stats_not_expected(
        conn, match_id=847, reason_code="abandoned",
        display_reason="Match abandoned and not played.", actor="operator",
    )
    updated = review_stats_not_expected(
        conn, match_id=847, reason_code="not_played",
        display_reason="Reviewed correction.", actor="operator",
    )
    assert created.match_id == replayed.match_id == updated.match_id == 847
    assert updated.reason_code == "not_played"
    assert active_stats_exception(conn, 847).display_reason == "Reviewed correction."
    assert revoke_stats_not_expected(conn, match_id=847, actor="operator") is True
    assert active_stats_exception(conn, 847) is None
    assert conn.row_factory is None
    assert conn.execute(
        "SELECT action FROM match_data_exception_audit ORDER BY audit_id"
    ).fetchall() == [("created",), ("updated",), ("revoked",)]


def test_candidate_cli_scopes_year_to_configured_competition(tmp_path, monkeypatch, capsys):
    import cli_runtime
    import db.connection

    path = tmp_path / "competitions.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    now = "2026-01-01T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO afl_competitions(afl_id,provider_id,code,name,updated_at) VALUES(?,?,?,?,?)",
        [(1, "CD_C014", "AFL", "AFL", now),
         (2, "CD_C999", "OTHER", "Other", now)],
    )
    conn.executemany(
        "INSERT INTO afl_seasons(afl_id,provider_id,competition_id,year,updated_at) VALUES(?,?,?,?,?)",
        [(85, "CD_S85", 1, 2015, now), (86, "CD_S86", 2, 2015, now)],
    )
    conn.executemany(
        "INSERT INTO matches(match_id,match_provider_id,round_id,season_id,status) VALUES(?,?,?,?,?)",
        [(847, "CD_M20150141408", 1, 85, "CONCLUDED"),
         (999, "CD_M_OTHER", 2, 86, "CONCLUDED")],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db.connection, "get_read_only_db_connection",
                        lambda: sqlite3.connect(path))
    cli_runtime.handle_report_stats_absence_candidates(SimpleNamespace(
        report_stats_absence_candidates=2015,
        afl_competition_code="AFL", afl_competition_provider_id="CD_C014",
    ))
    output = json.loads(capsys.readouterr().out)
    assert [item["match_id"] for item in output["candidates"]] == [847]
