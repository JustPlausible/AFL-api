"""Focused checks for the repository version and its runtime surfaces."""

import os
import subprocess
import sys
from pathlib import Path

from version import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_authoritative_version():
    assert __version__ == "0.6.0"


def test_minimal_version_import_does_not_load_runtime_stack():
    command = (
        "import sys; from version import __version__; "
        "assert not ({'main', 'config', 'db.connection', 'playwright'} & set(sys.modules)); "
        "print(__version__)"
    )
    result = subprocess.run(
        [sys.executable, "-c", command], cwd=ROOT, text=True,
        capture_output=True, check=True,
    )
    assert result.stdout == "0.6.0\n"


def test_cli_version_is_script_friendly_and_skips_runtime_imports(tmp_path):
    # An unusable DB path would expose accidental application/database startup.
    environment = {**os.environ, "DB_PATH": str(tmp_path / "missing" / "db.sqlite")}
    result = subprocess.run(
        [sys.executable, "cli.py", "--version"], cwd=ROOT, env=environment,
        text=True, capture_output=True, check=True,
    )
    assert result.stdout == f"{__version__}\n"
    assert result.stderr == ""


def test_fastapi_openapi_metadata_uses_authoritative_version(tmp_path):
    environment = {**os.environ, "DB_PATH": str(tmp_path / "api.sqlite")}
    command = (
        "import json; from main import app; "
        "print(json.dumps({'app': app.version, 'openapi': app.openapi()['info']['version']}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", command], cwd=ROOT, env=environment,
        text=True, capture_output=True, check=True,
    )
    assert result.stderr == ""
    assert result.stdout.strip() == '{"app": "0.6.0", "openapi": "0.6.0"}'
