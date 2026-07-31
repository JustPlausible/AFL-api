from __future__ import annotations

import json
import sqlite3
import sys

import cli
from afl_json import CollectionResult, PlayerCollectionResult
from db.migration_runner import migrate_database


class FakeClient:
    def __enter__(self): return self
    def __exit__(self, *_args): return None


class FakeCollector:
    player_status = "published"

    def __init__(self, _client, **_kwargs): pass

    def collect(self, **_kwargs):
        return CollectionResult(
            {"afl_id": 1, "provider_id": "CD_C014", "code": "AFL", "name": "AFL",
             "metadata": None, "source": {}},
            {"afl_id": 85, "provider_id": "CD_S1", "name": "2026 AFL", "short_name": "2026",
             "year": 2026, "current": True, "current_round_number": 1, "start_time": None,
             "end_time": None, "metadata": None, "source": {}},
            [{"afl_id": 100, "provider_id": "CD_R1", "name": "Round 1", "round_number": 1,
              "abbreviation": "R1", "start_time": None, "end_time": None, "byes": [],
              "metadata": None, "source": {}}],
            [{"afl_id": 10, "provider_id": "CD_T1", "name": "Cats", "abbreviation": "CAT",
              "nickname": None, "displayName": "Cats", "shortName": None, "team_type": None,
              "metadata": None, "club": None, "source": {}}],
            [],
        )

    def collect_players(self, provider_season_id):
        if self.player_status == "unavailable":
            return PlayerCollectionResult([], [], [], "unavailable")
        return PlayerCollectionResult(
            [{"champion_data_player_id": "CD_I1", "afl_player_id": 7001,
              "name": "Ada Example", "given_name": "Ada", "family_name": "Example"}],
            [{"champion_data_player_id": "CD_I1", "provider_season_id": provider_season_id,
              "team_id": "CD_T1", "jumper_number": 7, "listed_position": "MID",
              "photo_url": None, "source": {"playerId": "CD_I1"}}], [], "published",
        )


def run(tmp_path, monkeypatch, capsys, status="published"):
    path = tmp_path / f"cli-{status}.db"
    migrate_database(path)
    FakeCollector.player_status = status
    monkeypatch.setattr(cli, "AflJsonClient", FakeClient)
    monkeypatch.setattr(cli, "PublicAflCollector", FakeCollector)
    monkeypatch.setattr(cli, "get_db_connection", lambda: sqlite3.connect(path))
    monkeypatch.setattr(sys, "argv", ["cli.py", "--bootstrap-afl-season", "2026"])
    cli.main()
    return path, json.loads(capsys.readouterr().out)


def test_supported_bootstrap_dispatch_persists_players_and_prints_counts(tmp_path, monkeypatch, capsys):
    path, output = run(tmp_path, monkeypatch, capsys)
    assert output["player_collection_status"] == "published"
    assert output["canonical_players_inserted"] == 1
    assert output["provider_mappings_inserted"] == 2
    assert output["player_seasons_inserted"] == 1
    assert output["missing_team_links"] == 0
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM competition_season_players").fetchone() == (1,)


def test_supported_bootstrap_distinguishes_unavailable_without_deleting(tmp_path, monkeypatch, capsys):
    path, output = run(tmp_path, monkeypatch, capsys, "unavailable")
    assert output["player_collection_status"] == "unavailable"
    assert output["players_collected"] == output["player_seasons_inserted"] == 0
    assert sqlite3.connect(path).execute(
        "SELECT COUNT(*) FROM competition_season_players"
    ).fetchone() == (0,)
