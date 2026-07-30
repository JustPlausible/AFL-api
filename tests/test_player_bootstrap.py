import json
import os
from types import SimpleNamespace

import pytest

import config
from db.import_to_db import import_players
from db.migration_runner import migrate_database
from merge import helpers
from utils import stats_cache


def _raw_player(**overrides):
    player = {
        "full_name": "Harley Reid", "first_name": "Harley", "last_name": "Reid",
        "club": "West Coast Eagles", "champion_data_id": "101001",
        "afl_id": None, "afl_url": None,
    }
    player.update(overrides)
    return player


def _write_leaderboard(path, records):
    path.write_text(json.dumps(records), encoding="utf-8")


def test_champion_data_id_enriches_to_afl_identity_across_identifier_types(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    raw = tmp_path / "data/players-westcoast-raw.json"
    raw.write_text(json.dumps([_raw_player(champion_data_id=101001)]))
    leaderboard = tmp_path / "data/afl_stats_leaderboard.json"
    _write_leaderboard(leaderboard, [{
        "champion_data_id": "101001", "afl_id": 9001,
        "afl_url": "https://www.afl.com.au/players/9001/harley-reid",
    }])
    monkeypatch.setattr(helpers, "LEADERBOARD_PATH", leaderboard)
    monkeypatch.setattr(helpers, "ensure_leaderboard_fresh", lambda max_age_hours: True)

    helpers.resolve_players_for_club("westcoast")

    player = json.loads((tmp_path / "data/players-westcoast.json").read_text())[0]
    assert player["afl_id"] == 9001
    assert player["afl_url"].endswith("/9001/harley-reid")
    assert player["source"] == "afl-leaderboard"


def test_missing_leaderboard_entry_does_not_guess_an_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/players-westcoast-raw.json").write_text(json.dumps([_raw_player()]))
    leaderboard = tmp_path / "data/afl_stats_leaderboard.json"
    _write_leaderboard(leaderboard, [{
        "champion_data_id": "different", "afl_id": 42,
        "afl_url": "https://www.afl.com.au/players/42/player",
    }])
    monkeypatch.setattr(helpers, "LEADERBOARD_PATH", leaderboard)
    monkeypatch.setattr(helpers, "ensure_leaderboard_fresh", lambda max_age_hours: True)

    helpers.resolve_players_for_club("westcoast")

    player = json.loads((tmp_path / "data/players-westcoast.json").read_text())[0]
    assert player["afl_id"] is None
    assert player["source"] == "fallback"


def test_missing_leaderboard_uses_direct_squad_identity_without_guessing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    direct_url = "https://www.afl.com.au/players/9001/harley-reid"
    (tmp_path / "data/players-westcoast-raw.json").write_text(json.dumps([
        _raw_player(afl_id=9001, afl_url=direct_url),
    ]))
    monkeypatch.setattr(helpers, "load_leaderboard_index",
                        lambda: (_ for _ in ()).throw(RuntimeError("refresh failed")))

    helpers.resolve_players_for_club("westcoast")

    player = json.loads((tmp_path / "data/players-westcoast.json").read_text())[0]
    assert player["afl_id"] == 9001
    assert player["afl_url"] == direct_url
    assert player["source"] == "fallback"


@pytest.mark.parametrize("initial", [None, []])
def test_missing_or_stale_empty_leaderboard_is_not_silently_accepted(tmp_path, monkeypatch, initial):
    path = tmp_path / "afl_stats_leaderboard.json"
    if initial is not None:
        _write_leaderboard(path, initial)
        os.utime(path, (0, 0))
    monkeypatch.setattr(stats_cache, "LEADERBOARD_PATH", path)
    monkeypatch.setattr(stats_cache, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="", stderr=""))

    with pytest.raises(RuntimeError, match="no player records|without creating"):
        stats_cache.ensure_leaderboard_fresh(max_age_hours=1)


def _prepare_import(tmp_path, monkeypatch, players):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "players.db"
    migrate_database(db)
    (data / "players-westcoast.json").write_text(json.dumps(players))
    monkeypatch.setattr(config, "DB_PATH", str(db))
    monkeypatch.setattr("db.import_to_db.DATA_DIR", data)
    return db


def test_import_counts_every_player_skipped(tmp_path, monkeypatch):
    _prepare_import(tmp_path, monkeypatch, [_raw_player(), _raw_player(full_name="Reuben Ginbey")])

    counts = import_players()

    assert counts == {"processed": 2, "inserted": 0, "updated": 0,
                      "skipped_missing_afl_id": 2, "failed": 0}


def test_import_counts_mixed_inserted_and_skipped_records(tmp_path, monkeypatch):
    db = _prepare_import(tmp_path, monkeypatch, [
        _raw_player(afl_id=9001, afl_url="https://www.afl.com.au/players/9001"),
        _raw_player(full_name="Unresolved Player"),
    ])

    counts = import_players()

    assert counts == {"processed": 2, "inserted": 1, "updated": 0,
                      "skipped_missing_afl_id": 1, "failed": 0}
    import sqlite3
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT full_name FROM players").fetchall() == [("Harley Reid",)]
