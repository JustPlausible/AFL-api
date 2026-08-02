import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MODULES = {
    "afl_json", "db.connection", "db.import_to_db", "db.scrape_runs",
    "scraper.scrape_afl_clubs", "scraper.scrape_afl_lineups",
    "scraper.scrape_afl_matches", "scraper.scrape_afl_player_stats",
}


def isolated_cli(code):
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, text=True, check=False)


def test_import_cli_is_lightweight_and_parser_and_validators_work():
    result = isolated_cli("""
import json, sys
before = set(sys.modules)
import cli
args = cli.create_parser().parse_args(['--collect-match-rosters', 'CD_R202601421'])
blocked = %r
print(json.dumps({'loaded': sorted(blocked & (set(sys.modules) - before)),
                  'round': args.collect_match_rosters,
                  'match': cli.cfs_match_provider_id('CD_M20260142001'),
                  'has_loader': hasattr(cli, '_load_runtime_components')}))
""" % RUNTIME_MODULES)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output == {"loaded": [], "round": "CD_R202601421",
                      "match": "CD_M20260142001", "has_loader": False}


@pytest.mark.parametrize("arguments", [["--help"], ["--version"]])
def test_help_and_version_do_not_load_runtime_dependencies(arguments):
    result = isolated_cli("""
import json, sys
import cli
try:
    cli.main(%r)
except SystemExit as exc:
    if exc.code != 0: raise
blocked = %r
print('MODULES=' + json.dumps(sorted(blocked & set(sys.modules))))
""" % (arguments, RUNTIME_MODULES))
    assert result.returncode == 0, result.stderr
    assert "MODULES=[]" in result.stdout


def test_json_operation_uses_only_selected_handler_imports(monkeypatch, tmp_path, capsys):
    import cli_runtime

    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    summary = {"status": "successful"}
    orchestrator = Mock()
    orchestrator.run.return_value = summary
    fake_afl_json = ModuleType("afl_json")
    fake_afl_json.AflJsonClient = Mock(return_value=client)
    fake_afl_json.BatchCollectionError = type("BatchCollectionError", (Exception,), {})
    fake_afl_json.CollectionOrchestrator = Mock(return_value=orchestrator)
    fake_afl_json.CollectionRequest = Mock(return_value=object())
    monkeypatch.setitem(sys.modules, "afl_json", fake_afl_json)
    for name in RUNTIME_MODULES - {"afl_json"}:
        monkeypatch.delitem(sys.modules, name, raising=False)
    args = SimpleNamespace(collection_endpoints="metadata", collection_overwrite=False,
                           collection_resume=False, afl_season="2026", collection_output=tmp_path,
                           collection_round=[], collection_match=[], afl_competition_code="AFL",
                           afl_competition_provider_id="CD_C014")

    cli_runtime.handle_collect_afl_data(args)

    output = json.loads(capsys.readouterr().out)
    assert output["operation"] == "collect_afl_data"
    assert output["result_status"] == "success"
    assert output["status"] == "successful"
    assert output["mode"] == "database_free"
    assert output["database_opened"] is False
    assert output["persistence_target"] == "none"
    assert not ({name for name in RUNTIME_MODULES if name.startswith("scraper.")} & set(sys.modules))
    fake_afl_json.CollectionOrchestrator.assert_called_once_with(client)


def test_legacy_operation_uses_only_selected_handler_imports(monkeypatch):
    import cli_runtime

    scraper = ModuleType("scraper")
    player_stats = ModuleType("scraper.scrape_afl_player_stats")
    player_stats.run_scraper = Mock()
    scraper.scrape_afl_player_stats = player_stats
    log_module = ModuleType("utils.log")
    log_module.log = Mock()
    monkeypatch.setitem(sys.modules, "scraper", scraper)
    monkeypatch.setitem(sys.modules, "scraper.scrape_afl_player_stats", player_stats)
    monkeypatch.setitem(sys.modules, "utils.log", log_module)
    monkeypatch.delitem(sys.modules, "afl_json", raising=False)
    monkeypatch.delitem(sys.modules, "db.connection", raising=False)

    cli_runtime.handle_scrape_match(SimpleNamespace(scrape_match=8216))

    player_stats.run_scraper.assert_called_once_with(match_id=8216, once=True)
    message = log_module.log.call_args.args[0]
    assert "source_family=html" in message
    assert "mode=legacy_persistent" in message
    assert "persistence_target=player_stats" in message
    assert "fallback_allowed=False" in message
    assert "fallback_occurred=False" in message
    assert "afl_json" not in sys.modules
    assert "db.connection" not in sys.modules
