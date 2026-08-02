from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import cli
import afl_json
from afl_json import AflJsonResourceUnavailable
from afl_json.player_stats import upsert_player_stats as real_upsert_player_stats
from db.migration_runner import migrate_database


FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"


class FakeResponse:
    def __init__(self, payload):
        self.data = payload


class FakeClient:
    payload = None
    detail_payload = None
    unavailable = False
    calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        if endpoint == "match_detail":
            return FakeResponse(self.detail_payload)
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
    monkeypatch.setattr("db.connection.get_db_connection", lambda: sqlite3.connect(path))
    monkeypatch.setattr(afl_json, "AflJsonClient", FakeClient)
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

    assert {key: first[key] for key in (
        "match_provider_id", "status", "resolved_match_status", "records_collected",
        "rows_written", "rejected_records", "diagnostics",
    )} == {
        "match_provider_id": "CD_M20260140204", "status": "concluded",
        "resolved_match_status": "CONCLUDED", "records_collected": 2,
        "rows_written": 2, "rejected_records": 0, "diagnostics": [],
    }
    assert first["direct_match_detail_status"] is None
    assert first["canonical_match_refreshed"] is False
    assert second["rows_written"] == 0
    assert first["source_family"] == "cfs_json"
    assert first["collector"] == "MatchPlayerStatsCollector"
    assert first["mode"] == "persistent"
    assert first["persistence_target"] == "cfs_player_stats"
    assert first["fallback_allowed"] is False
    assert first["fallback_occurred"] is False
    assert first["fallback_reason"] is None
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

    monkeypatch.setattr(afl_json, "upsert_player_stats", fail_after_one)

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


def test_cli_reconciles_postgame_via_automatic_afl_id_and_persists_46_final_rows(
    cli_database, monkeypatch, capsys,
):
    conn = sqlite3.connect(cli_database)
    conn.execute("UPDATE matches SET match_id=8207, match_provider_id=?, status='POSTGAME'",
                 ("CD_M20260142007",))
    conn.commit()
    conn.close()
    monkeypatch.setattr(sys, "argv", [
        "cli.py", "--collect-match-player-stats", "CD_M20260142007",
    ])
    detail = json.loads((FIXTURES / "match_detail_concluded.json").read_text())
    payload = concluded_payload()
    payload.pop("status")
    templates = payload["homeTeamPlayerStats"] + payload["awayTeamPlayerStats"]
    rows = []
    for index in range(46):
        row = json.loads(json.dumps(templates[index % len(templates)]))
        player = row["player"]["player"]["player"]
        player["playerId"] = f"CD_I{index + 1}"
        rows.append(row)
    payload["homeTeamPlayerStats"], payload["awayTeamPlayerStats"] = rows[:23], rows[23:]
    FakeClient.detail_payload = detail
    FakeClient.payload = payload
    FakeClient.unavailable = False
    FakeClient.calls = []

    cli.main()
    output = json.loads(capsys.readouterr().out)

    assert output["afl_match_id"] == 8207
    assert output["stored_canonical_status"] == "POSTGAME"
    assert output["direct_match_detail_status"] == "CONCLUDED"
    assert output["resolved_match_status"] == "CONCLUDED"
    assert output["status_resolution"] == "direct_match_detail"
    assert output["canonical_match_refreshed"] is True
    assert output["records_collected"] == output["rows_written"] == 46
    assert FakeClient.calls[0] == (
        "match_detail", {"path_parameters": {"afl_match_id": 8207}},
    )
    conn = sqlite3.connect(cli_database)
    assert conn.execute("SELECT status FROM matches").fetchone() == ("CONCLUDED",)
    assert conn.execute(
        "SELECT COUNT(*), MIN(snapshot_authority), MAX(snapshot_authority) FROM cfs_player_stats"
    ).fetchone() == (46, 2, 2)
