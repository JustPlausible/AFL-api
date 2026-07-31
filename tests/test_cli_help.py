import subprocess
import sys
from pathlib import Path

import pytest

import cli


def test_cli_help_does_not_require_club_data(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(repository / "cli.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--collect-afl-metadata" in result.stdout
    assert "--afl-raw-directory" in result.stdout
    assert "CD_R202601421" in result.stdout
    assert "CD_M20260142001" in result.stdout
    assert "persists to player_stats" in " ".join(result.stdout.split())


@pytest.mark.parametrize(("flag", "value", "expected"), [
    ("--collect-match-rosters", "1365", "CD_R..."),
    ("--collect-match-rosters", "CD_M20260142001", "CD_R..."),
    ("--collect-match-player-stats", "8216", "CD_M..."),
    ("--collect-match-player-stats", "CD_R202601421", "CD_M..."),
])
def test_cfs_cli_rejects_invalid_provider_ids_without_traceback(flag, value, expected):
    repository = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "cli", flag, value], cwd=repository,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert expected in result.stderr
    assert "numeric AFL identifiers are not accepted" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(("flag", "value", "attribute"), [
    ("--collect-match-rosters", "CD_R202601421", "collect_match_rosters"),
    ("--collect-match-player-stats", "CD_M20260142001", "collect_match_player_stats"),
])
def test_cfs_cli_accepts_well_formed_provider_ids(monkeypatch, flag, value, attribute):
    monkeypatch.setattr(sys, "argv", ["cli.py", flag, value])
    assert getattr(cli.handle_args(), attribute) == value
