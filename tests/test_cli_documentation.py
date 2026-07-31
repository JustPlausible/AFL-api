import re
import shlex
from pathlib import Path

import pytest

import cli


REPOSITORY = Path(__file__).resolve().parents[1]
RELEASE_DOCS = [REPOSITORY / "README.md", *sorted((REPOSITORY / "docs").rglob("*.md"))]


def documented_cli_arguments():
    """Yield argv from release-facing shell fences without executing commands."""
    fence = re.compile(r"```(?:bash|sh)\n(.*?)```", re.DOTALL)
    for path in RELEASE_DOCS:
        for block in fence.findall(path.read_text()):
            logical_lines = block.replace("\\\n", " ").splitlines()
            for line in logical_lines:
                tokens = shlex.split(line, comments=True)
                if not tokens or tokens[0] not in {"python", "python3"}:
                    continue
                if len(tokens) >= 2 and tokens[1] == "cli.py":
                    arguments = tokens[2:]
                elif len(tokens) >= 3 and tokens[1:3] == ["-m", "cli"]:
                    arguments = tokens[3:]
                else:
                    continue
                if ">" in arguments:
                    arguments = arguments[:arguments.index(">")]
                yield path.relative_to(REPOSITORY), line, arguments


@pytest.mark.parametrize(
    "path,line,arguments",
    list(documented_cli_arguments()),
    ids=lambda value: str(value)[:80],
)
def test_release_documented_cli_examples_are_parser_valid(path, line, arguments):
    try:
        cli.create_parser().parse_args(arguments)
    except SystemExit as error:
        if error.code != 0:  # --help is a successful parser exit.
            pytest.fail(f"{path}: documented command is not parser-valid: {line!r} ({error})")


def test_readme_no_longer_advertises_obsolete_cli_flags():
    readme = (REPOSITORY / "README.md").read_text()

    for obsolete in ("--all", "--scrape richmond", "--enrich richmond"):
        assert obsolete not in readme


def test_documented_cfs_examples_use_provider_identifier_families():
    for path, line, arguments in documented_cli_arguments():
        for flag, prefix in (
            ("--collect-match-rosters", "CD_R"),
            ("--collect-match-player-stats", "CD_M"),
        ):
            if flag in arguments:
                identifier = arguments[arguments.index(flag) + 1]
                assert identifier.startswith(prefix), f"{path}: {line}"
                assert not identifier.isdigit(), f"{path}: {line}"


@pytest.mark.parametrize("option", ["--source-status", "--afl-match-id"])
def test_player_stat_diagnostic_options_require_cfs_stat_collection(monkeypatch, option):
    value = "CONCLUDED" if option == "--source-status" else "8216"
    monkeypatch.setattr("sys.argv", ["cli.py", option, value])

    with pytest.raises(SystemExit, match="2"):
        cli.handle_args()
