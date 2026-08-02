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
    sys.modules.pop("cli_runtime", None)

    with pytest.raises(SystemExit, match="2"):
        cli.main(arguments)

    error = capsys.readouterr().err
    expected = [flag for flag in cli.OPERATION_FLAGS.values() if flag in arguments]
    assert "Only one operation may be selected per invocation." in error
    assert f"Conflicting operations: {', '.join(expected)}" in error
    assert "cli_runtime" not in sys.modules


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


def test_zero_legacy_match_id_preserves_no_operation_behavior():
    result = subprocess.run(
        [sys.executable, "cli.py", "--scrape-match", "0"], cwd=ROOT,
        capture_output=True, text=True, check=False,
    )
    probe = subprocess.run(
        [sys.executable, "-c", (
            "import json, sys, cli; cli.main(['--scrape-match', '0']); "
            "print(json.dumps(sorted({'cli_runtime', "
            "'scraper.scrape_afl_player_stats'} & set(sys.modules))))"
        )], cwd=ROOT, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert "No valid argument supplied. Use --help for options." in result.stderr
    assert probe.returncode == 0
    assert probe.stdout == "[]\n"


def test_nonzero_legacy_match_id_dispatches_selected_handler(monkeypatch):
    import cli_runtime

    handler = Mock()
    monkeypatch.setitem(cli_runtime.HANDLERS, "scrape_match", handler)

    cli.main(["--scrape-match", "8216"])

    handler.assert_called_once()
    assert handler.call_args.args[0].scrape_match == 8216


def test_help_remains_a_successful_parser_exit(capsys):
    with pytest.raises(SystemExit, match="0"):
        cli.handle_args(["--help"])
    assert "AFL operator CLI" in capsys.readouterr().out


def test_version_remains_script_friendly(capsys):
    sys.modules.pop("cli_runtime", None)
    cli.main(["--version"])
    assert capsys.readouterr().out == f"{cli.__version__}\n"
    assert "cli_runtime" not in sys.modules


def test_version_conflicts_with_an_operation(capsys):
    with pytest.raises(SystemExit, match="2"):
        cli.handle_args(["--version", "--scrape-injuries"])
    assert "Conflicting operations: --version, --scrape-injuries" in capsys.readouterr().err


def test_every_authoritative_operation_is_registered_by_the_parser():
    import cli_runtime

    actions = {action.dest: action for action in cli.create_parser()._actions}
    registered_operations = {
        destination for destination, action in actions.items()
        if getattr(action, "is_top_level_operation", False)
    }
    assert registered_operations == set(cli.OPERATION_FLAGS)
    for destination, flag in cli.OPERATION_FLAGS.items():
        assert flag in actions[destination].option_strings
    assert set(cli_runtime.HANDLERS) == set(cli.OPERATION_FLAGS) - {"version"}
