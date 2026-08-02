import json
import sys
from types import SimpleNamespace

import pytest

import cli
import afl_json


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

    monkeypatch.setattr(afl_json, "AflJsonClient", FakeClient)
    monkeypatch.setattr(afl_json, "MatchRosterCollector", Collector)
    monkeypatch.setattr(sys, "argv", [
        "cli.py", "--collect-match-rosters", "CD_R202601421",
    ])

    cli.main()
    output = json.loads(capsys.readouterr().out)

    assert {key: output[key] for key in (
        "operation", "source_family", "collector", "mode", "database_opened",
        "persistence_target", "fallback_allowed", "fallback_occurred",
        "round_provider_id", "status", "publication_state", "selections",
    )} == {
        "operation": "collect_match_rosters", "source_family": "cfs_json",
        "collector": "MatchRosterCollector", "mode": "read_only",
        "database_opened": False, "persistence_target": "none",
        "fallback_allowed": False, "fallback_occurred": False,
        "round_provider_id": "CD_R202601421", "status": status,
        "publication_state": status.upper(), "selections": count,
    }
    assert output["result_status"] == ("success" if status == "published" else "empty")
    assert len(calls) == 1
