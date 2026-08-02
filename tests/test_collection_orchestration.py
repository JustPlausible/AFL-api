import json
from pathlib import Path

import pytest

import cli
import afl_json
from afl_json import AflJsonResponse
from afl_json.orchestration import CollectionOrchestrator, CollectionRequest


FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FixtureClient:
    def __init__(self, *, fail=None):
        self.calls = []
        self.fail = fail

    def get(self, endpoint, *, path_parameters=None, params=None):
        name = endpoint.name if hasattr(endpoint, "name") else endpoint
        self.calls.append((name, path_parameters or {}, params or {}))
        if self.fail == name:
            raise RuntimeError("secret-token=do-not-record cookie=session-secret")
        values = {
            "competitions": fixture("competitions.json"),
            "competition_seasons": fixture("seasons.json"),
            "rounds": fixture("rounds.json"),
            "teams": fixture("teams.json"),
            "player_id_map": fixture("player_id_map.json"),
            "season_players": fixture("season_players_complete.json"),
            "match_rosters": fixture("match_rosters_completed.json"),
            "match_player_statistics": fixture("match_player_stats_concluded.json"),
        }
        if name == "matches":
            value = fixture(f"matches_round_{params['roundNumber']}.json")
        else:
            value = values[name]
        return AflJsonResponse(name, 200, value, {"set-cookie": "must-not-be-written"})


def request(tmp_path, **values):
    defaults = dict(season=2026, output=tmp_path / "run", rounds=(1,), matches=("8001",),
                    endpoint_families=("metadata", "players", "fixtures", "rosters",
                                       "lineups", "player-stats"))
    defaults.update(values)
    return CollectionRequest(**defaults)


def test_successful_database_free_full_pipeline_writes_raw_and_normalised_json(tmp_path, monkeypatch):
    monkeypatch.setattr("db.connection.get_db_connection",
                        lambda: pytest.fail("database connection must not be opened"))
    client = FixtureClient()
    summary = CollectionOrchestrator(client).run(request(tmp_path))

    assert summary["status"] == "successful"
    assert summary["metadata"]["database_writes"] is False
    assert (tmp_path / "run/raw/matches/matches__compSeasonId-85__competitionId-1__roundNumber-1__page-0001.json").is_file()
    assert (tmp_path / "run/normalised/fixtures/matches.json").is_file()
    assert (tmp_path / "run/normalised/players/players.json").is_file()
    assert (tmp_path / "run/normalised/rosters/CD_R1.json").is_file()
    assert (tmp_path / "run/normalised/lineups/CD_R1.json").is_file()
    assert (tmp_path / "run/normalised/player-stats/CD_M1.json").is_file()


def test_season_round_match_and_endpoint_family_selection(tmp_path):
    client = FixtureClient()
    CollectionOrchestrator(client).run(request(
        tmp_path, endpoint_families=("fixtures", "player-stats")
    ))
    matches = json.loads((tmp_path / "run/normalised/fixtures/matches.json").read_text())["data"]
    assert [item["afl_id"] for item in matches] == [8001]
    assert not (tmp_path / "run/normalised/players").exists()
    assert [call[2].get("roundNumber") for call in client.calls if call[0] == "matches"] == [1]


def test_deterministic_paths_safe_overwrite_and_resume(tmp_path):
    orchestrator = CollectionOrchestrator(FixtureClient())
    orchestrator.run(request(tmp_path))
    paths = sorted(path.relative_to(tmp_path / "run") for path in (tmp_path / "run").rglob("*.json"))
    with pytest.raises(FileExistsError):
        orchestrator.run(request(tmp_path))
    orchestrator.run(request(tmp_path, mode="resume"))
    orchestrator.run(request(tmp_path, mode="overwrite"))
    assert paths == sorted(path.relative_to(tmp_path / "run") for path in (tmp_path / "run").rglob("*.json"))
    assert not list((tmp_path / "run").rglob("*.tmp"))


def test_failures_are_redacted_grouped_and_mark_batch_failed(tmp_path):
    summary = CollectionOrchestrator(FixtureClient(fail="match_player_statistics")).run(request(tmp_path))
    rendered = json.dumps(summary)
    assert summary["status"] == "failed"
    assert summary["counts"]["failed"] == {"player-stats": 1}
    assert "do-not-record" not in rendered and "session-secret" not in rendered
    all_files = "".join(path.read_text() for path in (tmp_path / "run").rglob("*.json"))
    assert "must-not-be-written" not in all_files and "do-not-record" not in all_files


def test_unsupported_endpoint_family_is_clearly_skipped(tmp_path):
    summary = CollectionOrchestrator(FixtureClient()).run(request(
        tmp_path, endpoint_families=("fixtures", "commentary")
    ))
    assert summary["status"] == "successful"
    skipped = [item for item in summary["resources"] if item["status"] == "skipped"]
    assert skipped == [{"resource_type": "commentary", "identifier": "commentary",
                        "status": "skipped", "detail": "unsupported endpoint family", "path": None}]


def test_request_metadata_contains_safe_source_contracts_not_credentials(tmp_path):
    CollectionOrchestrator(FixtureClient()).run(request(tmp_path, endpoint_families=("fixtures",)))
    metadata = json.loads((tmp_path / "run/request.json").read_text())
    assert metadata["request"]["season"] == 2026
    assert metadata["sources"]["season_players"]["authentication"] == "required"
    assert "token" not in json.dumps(metadata).lower()


def test_cli_returns_nonzero_for_batch_level_failure(tmp_path, monkeypatch):
    class ClientContext:
        def __enter__(self): return self
        def __exit__(self, *_args): return None

    class FailedOrchestrator:
        def __init__(self, _client): pass
        def run(self, _request):
            return {"status": "failed", "counts": {"successful": {}, "skipped": {},
                                                     "failed": {"player-stats": 1}}}

    monkeypatch.setattr(afl_json, "AflJsonClient", ClientContext)
    monkeypatch.setattr(afl_json, "CollectionOrchestrator", FailedOrchestrator)
    monkeypatch.setattr("sys.argv", ["cli.py", "--collect-afl-data", "--afl-season", "2026",
                                     "--collection-output", str(tmp_path), "--no-database"])
    with pytest.raises(SystemExit, match="1"):
        cli.main()


def test_existing_cli_metadata_operation_still_parses_without_collection_options(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli.py", "--collect-afl-metadata", "--afl-season", "2026"])
    args = cli.handle_args()
    assert args.collect_afl_metadata is True
    assert args.collect_afl_data is False
