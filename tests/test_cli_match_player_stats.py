from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import cli
from afl_json import AflJsonResourceUnavailable
from afl_json.player_stats import upsert_player_stats as real_upsert_player_stats
from db.migration_runner import migrate_database


FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"


class FakeResponse:
    def __init__(self, payload):
        self.data = payload


class FakeClient:
    payload = None
    unavailable = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, _endpoint, **_kwargs):
        if self.unavailable:
            raise AflJsonResourceUnavailable(
                "not published", endpoint="match_player_statistics"
            )
        return FakeResponse(self.payload)


@pytest.fixture
def cli_database(tmp_path, monkeypatch):
    path = tmp_path / "cli.db"
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO rounds(round_id, round_label, season_id, competition_id) "
        "VALUES (100, 'Round 1', 85, 1)"
    )
    conn.execute(
        "INSERT INTO matches(match_id, match_provider_id, round_id, status) "
        "VALUES (8059, 'CD_M20260140204', 100, 'CONCLUDED')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(cli, "DB_PATH", str(path))
    monkeypatch.setattr(cli, "get_db_connection", lambda: sqlite3.connect(path))
    monkeypatch.setattr(cli, "AflJsonClient", FakeClient)
    return path


def run_cli(monkeypatch, capsys, *extra):
    monkeypatch.setattr(sys, "argv", [
        "cli.py", "--collect-match-player-stats", "CD_M20260140204", *extra,
    ])
    cli.main()
    return json.loads(capsys.readouterr().out)


def concluded_payload():
    return json.loads((FIXTURES / "match_player_stats_concluded.json").read_text())


def test_cli_persists_stats_resolves_concluded_status_and_is_idempotent(
    cli_database, monkeypatch, capsys,
):
    FakeClient.payload = concluded_payload()
    FakeClient.unavailable = False

    first = run_cli(monkeypatch, capsys)
    second = run_cli(monkeypatch, capsys)

    assert first == {
        "match_provider_id": "CD_M20260140204", "status": "concluded",
        "resolved_match_status": "CONCLUDED",
        "records_collected": 2, "rows_written": 2, "rejected_records": 0,
        "diagnostics": 0,
    }
    assert second["rows_written"] == 0
    conn = sqlite3.connect(cli_database)
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 2
    assert conn.execute(
        "SELECT DISTINCT resolved_match_status, snapshot_authority FROM cfs_player_stats"
    ).fetchall() == [("CONCLUDED", 2)]


@pytest.mark.parametrize("unavailable,payload", [
    (True, None),
    (False, {"status": "LIVE", "homeTeamPlayerStats": [], "awayTeamPlayerStats": []}),
])
def test_cli_unavailable_or_empty_response_writes_zero(
    cli_database, monkeypatch, capsys, unavailable, payload,
):
    FakeClient.unavailable = unavailable
    FakeClient.payload = payload

    output = run_cli(monkeypatch, capsys)

    assert output["records_collected"] == 0
    assert output["rows_written"] == 0
    assert output["resolved_match_status"] == "CONCLUDED"
    assert sqlite3.connect(cli_database).execute(
        "SELECT COUNT(*) FROM cfs_player_stats"
    ).fetchone()[0] == 0


def test_cli_database_failure_rolls_back_partial_snapshot(
    cli_database, monkeypatch, capsys,
):
    FakeClient.payload = concluded_payload()
    FakeClient.unavailable = False

    def fail_after_one(conn, result):
        real_upsert_player_stats(conn, replace(result, records=result.records[:1]))
        raise sqlite3.OperationalError("simulated database failure")

    monkeypatch.setattr(cli, "upsert_player_stats", fail_after_one)

    with pytest.raises(sqlite3.OperationalError, match="simulated database failure"):
        run_cli(monkeypatch, capsys)
    conn = sqlite3.connect(cli_database)
    assert conn.execute("SELECT COUNT(*) FROM cfs_player_stats").fetchone()[0] == 0
    assert conn.execute(
        "SELECT status FROM scrape_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone() == ("failed",)


def test_cli_print_json_preserves_records_and_adds_write_summary(
    cli_database, monkeypatch, capsys,
):
    FakeClient.payload = concluded_payload()
    FakeClient.unavailable = False

    output = run_cli(monkeypatch, capsys, "--print-json")

    assert len(output["records"]) == output["records_collected"] == 2
    assert output["rows_written"] == 2
    assert output["resolved_match_status"] == "CONCLUDED"
