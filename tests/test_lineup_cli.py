import subprocess

import pytest

import scraper.scrape_afl_lineups as lineups
import importlib.util
from pathlib import Path


def _load_scheduler_module():
    spec = importlib.util.spec_from_file_location(
        "schedule_lineup_scrapes_for_test",
        Path(__file__).resolve().parents[1] / "scheduler" / "schedule_lineup_scrapes.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


schedule_lineup_scrapes = _load_scheduler_module()


def test_explicit_round_cli_parsing_and_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(lineups, "scrape_team_lineups", lambda round_number=0: calls.append(round_number) or [])

    assert lineups.main(["--round", "9"]) == 0

    assert calls == [9]


def test_explicit_match_cli_parsing_and_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(lineups, "scrape_match_lineup", lambda match_id: calls.append(match_id) or [])

    assert lineups.main(["--match", "7043"]) == 0

    assert calls == [7043]


def test_positional_round_cli_remains_supported(monkeypatch):
    calls = []
    monkeypatch.setattr(lineups, "scrape_team_lineups", lambda round_number=0: calls.append(round_number) or [])

    assert lineups.main(["9"]) == 0

    assert calls == [9]


def test_default_manual_cli_invocation_is_supported(monkeypatch):
    calls = []
    monkeypatch.setattr(lineups, "scrape_team_lineups", lambda round_number=0: calls.append(round_number) or [])

    assert lineups.main([]) == 0

    assert calls == [0]


def test_cli_rejects_conflicting_selectors(capsys):
    with pytest.raises(SystemExit) as exc:
        lineups.parse_args(["--round", "9", "--match", "7043"])

    assert exc.value.code == 2
    assert "choose only one lineup selector" in capsys.readouterr().err


@pytest.mark.parametrize("argv, message", [(["--round", "abc"], "must be an integer"), (["--match", "0"], "must be a positive integer"), (["--unknown"], "unrecognized arguments")])
def test_cli_rejects_invalid_and_unknown_arguments(argv, message, capsys):
    with pytest.raises(SystemExit) as exc:
        lineups.parse_args(argv)

    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_scheduler_round_invocation_uses_supported_cli(monkeypatch):
    calls = []
    monkeypatch.setattr(schedule_lineup_scrapes.subprocess, "run", lambda command, check: calls.append((command, check)))

    schedule_lineup_scrapes.run_lineup_round_scraper(9)

    assert calls == [(["python3", "-m", "scraper.scrape_afl_lineups", "--round", "9"], True)]


def test_scheduler_match_invocation_uses_supported_cli(monkeypatch):
    calls = []
    monkeypatch.setattr(schedule_lineup_scrapes.subprocess, "run", lambda command, check: calls.append((command, check)))

    schedule_lineup_scrapes.run_lineup_match_scraper(7043)

    assert calls == [(["python3", "-m", "scraper.scrape_afl_lineups", "--match", "7043"], True)]


def test_scheduler_reports_and_propagates_failed_subprocess(monkeypatch):
    def fail(command, check):
        raise subprocess.CalledProcessError(7, command)

    errors = []
    monkeypatch.setattr(schedule_lineup_scrapes.subprocess, "run", fail)
    monkeypatch.setattr(schedule_lineup_scrapes.log, "error", errors.append)

    with pytest.raises(subprocess.CalledProcessError):
        schedule_lineup_scrapes.run_lineup_match_scraper(7043)

    assert "match 7043" in errors[0]
    assert "exit code 7" in errors[0]


def test_match_mode_filters_unrelated_matches(monkeypatch):
    scraped = [
        {"match_id": 7043, "first_name": "Target"},
        {"match_id": 7044, "first_name": "Other"},
        {"match_id": "7043", "first_name": "TargetString"},
    ]
    monkeypatch.setattr(lineups, "get_round_for_match", lambda match_id: 9)
    calls = []
    monkeypatch.setattr(lineups, "scrape_team_lineups", lambda round_number=0: calls.append(round_number) or scraped)

    result = lineups.scrape_match_lineup(7043)

    assert calls == [9]
    assert result == [scraped[0], scraped[2]]
