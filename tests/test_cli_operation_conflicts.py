import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import cli


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("arguments", [
    ["--bootstrap-afl-season", "2026", "--scrape-injuries"],
    ["--collect-afl-metadata", "--collect-afl-data"],
    ["--collect-match-player-stats", "CD_M20260142001", "--scrape-match", "8216"],
    ["--scrape-clubs", "--scrape-injuries", "--import-clubs"],
])
def test_conflicting_operations_exit_before_runtime_or_dispatch(monkeypatch, capsys, arguments):
    runtime_loader = Mock()
    handlers = [Mock(), Mock(), Mock()]
    monkeypatch.setattr(cli, "_load_runtime_components", runtime_loader)
    monkeypatch.setattr(cli, "scrape_all_clubs", handlers[0])
    monkeypatch.setattr(cli, "scrape_injuries_to_db", handlers[1])
    monkeypatch.setattr(cli, "import_clubs_to_db", handlers[2])

    with pytest.raises(SystemExit, match="2"):
        cli.main(arguments)

    error = capsys.readouterr().err
    expected = [flag for flag in cli.OPERATION_FLAGS.values() if flag in arguments]
    assert "Only one operation may be selected per invocation." in error
    assert f"Conflicting operations: {', '.join(expected)}" in error
    runtime_loader.assert_not_called()
    for handler in handlers:
        handler.assert_not_called()


def test_conflict_order_is_authoritative_not_argv_order(capsys):
    arguments = ["--import-clubs", "--scrape-injuries", "--scrape-clubs"]

    with pytest.raises(SystemExit, match="2"):
        cli.handle_args(arguments)

    error = capsys.readouterr().err
    assert "Conflicting operations: --scrape-clubs, --scrape-injuries, --import-clubs" in error


def test_conflict_script_does_not_import_runtime_components():
    command = """
import json
import runpy
import sys
sys.argv = ['cli.py', '--bootstrap-afl-season', '2026', '--scrape-injuries']
try:
    runpy.run_path('cli.py', run_name='__main__')
except SystemExit:
    blocked = {'afl_json', 'db.connection', 'scraper.scrape_afl_clubs',
               'scraper.scrape_afl_lineups', 'scraper.scrape_afl_matches'}
    print(json.dumps(sorted(blocked & set(sys.modules))))
    raise
"""
    result = subprocess.run(
        [sys.executable, "-c", command], cwd=ROOT, capture_output=True, text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == "[]\n"
    assert "Conflicting operations: --scrape-injuries, --bootstrap-afl-season" in result.stderr


def test_one_operation_accepts_print_json():
    args = cli.handle_args(["--bootstrap-afl-season", "2026", "--print-json"])
    assert cli.selected_operation_flags(args) == ["--bootstrap-afl-season"]


def test_repeatable_collection_filters_are_not_operations(tmp_path):
    args = cli.handle_args([
        "--collect-afl-data", "--collection-output", str(tmp_path),
        "--collection-round", "1", "--collection-round", "2",
        "--collection-match", "8001", "--collection-match", "CD_M20260142001",
        "--collection-endpoints", "fixtures,rosters", "--no-database",
    ])
    assert cli.selected_operation_flags(args) == ["--collect-afl-data"]
    assert args.collection_round == [1, 2]
    assert args.collection_match == ["8001", "CD_M20260142001"]


def test_no_operation_remains_valid():
    assert cli.selected_operation_flags(cli.handle_args([])) == []


def test_help_remains_a_successful_parser_exit(capsys):
    with pytest.raises(SystemExit, match="0"):
        cli.handle_args(["--help"])
    assert "AFL operator CLI" in capsys.readouterr().out


def test_version_remains_script_friendly(monkeypatch, capsys):
    runtime_loader = Mock()
    monkeypatch.setattr(cli, "_load_runtime_components", runtime_loader)
    cli.main(["--version"])
    assert capsys.readouterr().out == f"{cli.__version__}\n"
    runtime_loader.assert_not_called()


def test_version_conflicts_with_an_operation(capsys):
    with pytest.raises(SystemExit, match="2"):
        cli.handle_args(["--version", "--scrape-injuries"])
    assert "Conflicting operations: --version, --scrape-injuries" in capsys.readouterr().err


def test_every_authoritative_operation_is_registered_by_the_parser():
    actions = {action.dest: action for action in cli.create_parser()._actions}
    registered_operations = {
        destination for destination, action in actions.items()
        if getattr(action, "is_top_level_operation", False)
    }
    assert registered_operations == set(cli.OPERATION_FLAGS)
    for destination, flag in cli.OPERATION_FLAGS.items():
        assert flag in actions[destination].option_strings
