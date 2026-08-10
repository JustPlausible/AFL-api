import os
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(argv, cwd, db_path, extra_env=None):
    """Invoke the documented API-key command with PYTHONPATH deliberately absent.

    Regression guard for Issue #143: the documented command must not depend on
    the caller's working directory or an operator-supplied PYTHONPATH. If a
    future change reintroduces that dependency (for example by routing through
    a script located outside the repository root), this call starts failing
    with ModuleNotFoundError exactly as the original bug did.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["DB_PATH"] = str(db_path)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        argv, cwd=cwd, env=env, text=True, capture_output=True, check=False,
    )


def _create_existing_database(path):
    """Create a valid, empty SQLite file at path, matching an already-migrated
    deployment. The API-key command must open an existing configured database
    rather than create one, so tests exercising the supported (non-error) path
    pre-create it here."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(path)).close()
    return path


def test_documented_cli_command_add_list_remove_roundtrip(tmp_path):
    """Covers --add, --list, --remove through the one documented invocation."""
    db_path = _create_existing_database(tmp_path / "afl_players.db")

    add_result = _run(
        [sys.executable, "cli.py", "--add-api-key", "issue-143-smoke-test"],
        cwd=ROOT, db_path=db_path,
    )
    assert add_result.returncode == 0, add_result.stderr
    assert "Copy this API key now. It will not be shown again:" in add_result.stdout
    full_key = add_result.stdout.strip().splitlines()[-1]
    assert len(full_key) > 20

    list_result = _run(
        [sys.executable, "cli.py", "--list-api-keys"], cwd=ROOT, db_path=db_path,
    )
    assert list_result.returncode == 0, list_result.stderr
    assert "issue-143-smoke-test" in list_result.stdout
    assert "(active)" in list_result.stdout
    # Administrative listing exposes only the prefix, never the full key.
    assert full_key not in list_result.stdout
    assert full_key[:8] in list_result.stdout

    remove_result = _run(
        [sys.executable, "cli.py", "--remove-api-key", "issue-143-smoke-test"],
        cwd=ROOT, db_path=db_path,
    )
    assert remove_result.returncode == 0, remove_result.stderr
    assert "Removed API key" in remove_result.stdout

    final_list = _run(
        [sys.executable, "cli.py", "--list-api-keys"], cwd=ROOT, db_path=db_path,
    )
    assert "No API keys found." in final_list.stdout
    assert "issue-143-smoke-test" not in final_list.stdout


def test_documented_command_works_without_pythonpath_from_an_unrelated_cwd(tmp_path):
    """The exact failure mode from Issue #143: ModuleNotFoundError('api_key_security')
    when the process working directory is not the repository root and PYTHONPATH is
    unset. The documented command must succeed regardless."""
    db_path = _create_existing_database(tmp_path / "configured" / "afl_players.db")
    unrelated_cwd = tmp_path / "somewhere-else"
    unrelated_cwd.mkdir()

    result = _run(
        [sys.executable, str(ROOT / "cli.py"), "--add-api-key", "cwd-independent"],
        cwd=unrelated_cwd, db_path=db_path,
    )

    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "Copy this API key now" in result.stdout


def test_module_invocation_form_also_works(tmp_path):
    """`python -m cli` is the documented module-execution alternative; it must
    resolve the same way as `python cli.py` without PYTHONPATH."""
    db_path = _create_existing_database(tmp_path / "afl_players.db")

    result = _run(
        [sys.executable, "-m", "cli", "--list-api-keys"], cwd=ROOT, db_path=db_path,
    )

    assert result.returncode == 0, result.stderr
    assert "No API keys found." in result.stdout


def test_command_uses_configured_db_path_and_never_an_unintended_default(tmp_path):
    configured_db = _create_existing_database(tmp_path / "configured-data" / "afl_players.db")
    other_cwd = tmp_path / "other-cwd"
    other_cwd.mkdir()

    result = _run(
        [sys.executable, str(ROOT / "cli.py"), "--add-api-key", "configured-path-check"],
        cwd=other_cwd, db_path=configured_db,
    )

    assert result.returncode == 0, result.stderr
    assert not (other_cwd / "data" / "afl_players.db").exists()
    conn = sqlite3.connect(configured_db)
    labels = [row[0] for row in conn.execute("SELECT label FROM api_keys")]
    conn.close()
    assert labels == ["configured-path-check"]


def test_full_key_is_never_persisted_only_hash_and_prefix(tmp_path):
    db_path = _create_existing_database(tmp_path / "afl_players.db")

    add_result = _run(
        [sys.executable, "cli.py", "--add-api-key", "hash-only-check"],
        cwd=ROOT, db_path=db_path,
    )
    full_key = add_result.stdout.strip().splitlines()[-1]

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT api_key, key_hash, key_prefix FROM api_keys WHERE label = 'hash-only-check'"
    ).fetchone()
    conn.close()

    assert row[0] is None
    assert row[1].startswith("sha256:")
    assert full_key not in row[1]
    assert row[2] == full_key[:8]


@pytest.mark.parametrize("flag_argv", [
    ["--add-api-key", "should-not-be-created"],
    ["--list-api-keys"],
    ["--remove-api-key", "anything"],
])
def test_missing_configured_database_fails_clearly_without_creating_one(tmp_path, flag_argv):
    """An incorrectly configured DB_PATH must never be silently initialised.
    All three operations must fail with a clean non-zero result and leave no
    file behind, instead of creating and using an unintended database."""
    db_path = tmp_path / "configured" / "afl_players.db"  # deliberately not created

    result = _run([sys.executable, "cli.py", *flag_argv], cwd=ROOT, db_path=db_path)

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "Database file not found" in result.stderr
    assert not db_path.exists()
    assert not db_path.parent.exists() or not any(db_path.parent.iterdir())


def test_get_connection_uses_the_shared_database_connection_policy(monkeypatch):
    """Issue #143 follow-up: API-key persistence must use db.connection's
    shared connection policy instead of opening SQLite directly, so it gets
    the same existence check and connection policy as the rest of the app."""
    import scripts.manage_api_keys as manage_api_keys

    calls = []

    def fake_get_db_connection():
        calls.append(True)
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(manage_api_keys, "get_db_connection", fake_get_db_connection)

    manage_api_keys.get_connection()

    assert calls == [True]


def test_no_documentation_recommends_running_manage_api_keys_directly():
    """Exactly one documented/supported operator interface (Issue #143): no
    doc's example commands may invoke scripts/manage_api_keys.py directly, by
    script path or as a module, with or without a PYTHONPATH workaround. This
    checks documented operator-facing behaviour rather than freezing whether
    the backing module happens to keep an argument parser or __main__ block."""
    fence = re.compile(r"```(?:bash|sh)\n(.*?)```", re.DOTALL)
    docs = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")), ROOT / "scripts" / "README.md"]
    offenders = []
    for path in docs:
        for block in fence.findall(path.read_text()):
            for line in block.replace("\\\n", " ").splitlines():
                if not line.strip() or line.strip().startswith("#"):
                    continue
                tokens = shlex.split(line, comments=True)
                if any("manage_api_keys" in token for token in tokens):
                    offenders.append((path, line))
    assert offenders == []


def test_capability_grant_revoke_listing_and_safe_output(tmp_path):
    db_path = _create_existing_database(tmp_path / "afl_players.db")
    added = _run([sys.executable, "cli.py", "--add-api-key", "cap-client"], ROOT, db_path)
    secret = added.stdout.strip().splitlines()[-1]

    default_listing = _run([sys.executable, "cli.py", "--list-api-keys"], ROOT, db_path)
    assert "capabilities:standard-read" in default_listing.stdout
    assert "advanced-read" not in default_listing.stdout
    assert secret not in default_listing.stdout

    granted = _run([
        sys.executable, "cli.py", "--grant-api-key-capability", "cap-client", "advanced-read"
    ], ROOT, db_path)
    assert granted.returncode == 0
    assert "Granted capability 'advanced-read'" in granted.stdout
    listing = _run([sys.executable, "cli.py", "--list-api-keys"], ROOT, db_path)
    assert "advanced-read" in listing.stdout
    assert secret not in listing.stdout

    duplicate = _run([
        sys.executable, "cli.py", "--grant-api-key-capability", "cap-client", "advanced-read"
    ], ROOT, db_path)
    assert "already granted" in duplicate.stdout
    revoked = _run([
        sys.executable, "cli.py", "--revoke-api-key-capability", "cap-client", "advanced-read"
    ], ROOT, db_path)
    assert "Revoked capability 'advanced-read'" in revoked.stdout
    absent = _run([
        sys.executable, "cli.py", "--revoke-api-key-capability", "cap-client", "advanced-read"
    ], ROOT, db_path)
    assert "not granted" in absent.stdout


def test_capability_management_validates_capability_and_label(tmp_path):
    db_path = _create_existing_database(tmp_path / "afl_players.db")
    invalid = _run([
        sys.executable, "cli.py", "--grant-api-key-capability", "missing", "write"
    ], ROOT, db_path)
    assert "Invalid capability 'write'" in invalid.stdout
    missing = _run([
        sys.executable, "cli.py", "--grant-api-key-capability", "missing", "advanced-read"
    ], ROOT, db_path)
    assert "API key not found" in missing.stdout


def test_capability_management_rejects_ambiguous_duplicate_labels(tmp_path):
    db_path = _create_existing_database(tmp_path / "afl_players.db")
    for _ in range(2):
        added = _run(
            [sys.executable, "cli.py", "--add-api-key", "duplicate-client"],
            ROOT, db_path,
        )
        assert added.returncode == 0

    conn = sqlite3.connect(db_path)
    first_id = conn.execute(
        "SELECT id FROM api_keys WHERE label = ? ORDER BY id LIMIT 1",
        ("duplicate-client",),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO api_key_capabilities(api_key_id, capability) VALUES(?, ?)",
        (first_id, "advanced-read"),
    )
    conn.commit()
    before = conn.execute(
        "SELECT api_key_id, capability FROM api_key_capabilities ORDER BY api_key_id, capability"
    ).fetchall()
    conn.close()

    for operation in ("--grant-api-key-capability", "--revoke-api-key-capability"):
        result = _run(
            [sys.executable, "cli.py", operation, "duplicate-client", "advanced-read"],
            ROOT, db_path,
        )
        assert result.returncode == 0
        assert "label 'duplicate-client' is ambiguous" in result.stdout
        assert "must uniquely identify a credential" in result.stdout

        conn = sqlite3.connect(db_path)
        after = conn.execute(
            "SELECT api_key_id, capability FROM api_key_capabilities "
            "ORDER BY api_key_id, capability"
        ).fetchall()
        conn.close()
        assert after == before
