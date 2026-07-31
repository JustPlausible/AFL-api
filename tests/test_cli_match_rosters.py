import json
import sys
from types import SimpleNamespace

import pytest

import cli


class FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


@pytest.mark.parametrize(("status", "count"), [("empty", 0), ("published", 3)])
def test_roster_cli_reports_source_read_only_boundary_and_publication_state(
    monkeypatch, capsys, status, count,
):
    result = SimpleNamespace(
        round_provider_id="CD_R202601421", status=SimpleNamespace(value=status),
        publication_state=status.upper(), provider_timestamp=None,
        provider_version=None, rosters=[], selections=[{}] * count,
    )
    calls = []

    class Collector:
        def __init__(self, client, *, raw_directory=None):
            calls.append((client, raw_directory))

        def collect(self, provider_id):
            assert provider_id == "CD_R202601421"
            return result

    monkeypatch.setattr(cli, "AflJsonClient", FakeClient)
    monkeypatch.setattr(cli, "MatchRosterCollector", Collector)
    monkeypatch.setattr(sys, "argv", [
        "cli.py", "--collect-match-rosters", "CD_R202601421",
    ])

    cli.main()
    output = json.loads(capsys.readouterr().out)

    assert output == {
        "source_family": "cfs_json",
        "collector": "MatchRosterCollector",
        "persistence_target": None,
        "persistence_performed": False,
        "fallback_occurred": False,
        "fallback_reason": None,
        "round_provider_id": "CD_R202601421",
        "status": status,
        "publication_state": status.upper(),
        "provider_timestamp": None,
        "provider_version": None,
        "selections": count,
    }
    assert len(calls) == 1
