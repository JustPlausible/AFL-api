import subprocess
import sys
from pathlib import Path


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
