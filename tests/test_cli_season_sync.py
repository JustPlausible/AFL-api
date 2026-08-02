from __future__ import annotations

import json

import pytest

import afl_json
import cli
import cli_runtime
from afl_json.season_sync import MatchSyncResult, SeasonSyncResult


class FakeClient:
    def __enter__(self): return self
    def __exit__(self, *_args): return None


class FakeConnection:
    def close(self): pass


class FakeSynchronizer:
    result = None

    def __init__(self, *_args, **_kwargs): pass

    def run(self, **_kwargs): return self.result


def invoke(monkeypatch, capsys, result, *extra):
    FakeSynchronizer.result = result
    monkeypatch.setattr(afl_json, "AflJsonClient", FakeClient)
    monkeypatch.setattr("afl_json.season_sync.SeasonSynchronizer", FakeSynchronizer)
    monkeypatch.setattr("db.connection.get_db_connection", FakeConnection)
    args = cli.handle_args(["--sync-afl-season", "2026", *extra])
    cli_runtime.handle_sync_afl_season(args)
    return capsys.readouterr()


def result(outcome="success"):
    value = SeasonSyncResult(2026, competition_id=1, season_id=85,
                             bootstrap_outcome="success", outcome=outcome)
    value.total_matches_discovered = value.eligible_matches = 1
    value.collected_successfully = 1
    value.statistic_rows_inserted = 46
    value.matches = [MatchSyncResult(8001, "CD_M1", 1, "collected", "CONCLUDED",
                                     records=46, rows_inserted=46, audit_id="match-audit")]
    return value


def test_default_output_is_concise_human_summary(monkeypatch, capsys):
    output = invoke(monkeypatch, capsys, result())

    assert output.err == ""
    assert output.out.startswith("AFL season sync 2026: success\n")
    assert "matches: selected=1 eligible=1 collected=1" in output.out
    assert '"matches"' not in output.out


def test_print_json_emits_complete_machine_result(monkeypatch, capsys):
    output = invoke(monkeypatch, capsys, result(), "--print-json")
    payload = json.loads(output.out)

    assert output.err == ""
    assert payload["result_status"] == payload["outcome"] == "success"
    assert payload["records_received"] == 1
    assert payload["rows_written"] == 46
    assert payload["matches"][0]["match_id"] == 8001


def test_partial_print_json_preserves_stdout_before_exit_one(monkeypatch, capsys):
    value = result("partial")
    value.failed = 1
    value.matches[0] = MatchSyncResult(8001, "CD_M1", 1, "failed", error="safe")

    with pytest.raises(SystemExit, match="1"):
        invoke(monkeypatch, capsys, value, "--print-json")
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out)["result_status"] == "partial"


def test_audit_only_partial_json_preserves_committed_match(monkeypatch, capsys):
    value = result("partial")
    value.audit_outcome = "failed"
    value.audit_failures = 1
    value.matches[0] = MatchSyncResult(
        8001, "CD_M1", 1, "collected", "CONCLUDED", records=46,
        rows_inserted=46, rows_written=46, audit_id="match-audit",
        collection_outcome="concluded", persistence_outcome="committed",
        audit_outcome="failed", audit_error_class="OperationalError",
        audit_error_summary="token=<redacted>", processing_continued=True,
    )

    with pytest.raises(SystemExit, match="1"):
        invoke(monkeypatch, capsys, value, "--print-json")
    payload = json.loads(capsys.readouterr().out)
    assert payload["result_status"] == "partial"
    assert payload["matches"][0]["persistence_outcome"] == "committed"
    assert payload["matches"][0]["audit_outcome"] == "failed"
    assert payload["matches"][0]["rows_written"] == 46


@pytest.mark.parametrize("arguments,message", [
    (["--round", "-1"], "round must be zero or greater"),
    (["--round-from", "-1", "--round-to", "2"], "round must be zero or greater"),
    (["--match-id", "0"], "match ID must be a positive integer"),
    (["--match-id", "-1"], "match ID must be a positive integer"),
])
def test_invalid_numeric_domains_exit_two_on_stderr(capsys, arguments, message):
    with pytest.raises(SystemExit, match="2"):
        cli.handle_args(["--sync-afl-season", "2026", *arguments])
    output = capsys.readouterr()
    assert message in output.err
    assert output.out == ""


def test_round_zero_and_deduplicated_match_ids_are_valid():
    args = cli.handle_args([
        "--sync-afl-season", "2026", "--round", "0",
        "--match-id", "8001", "--match-id", "8001", "--match-id", "8002",
    ])
    assert args.round == 0
    assert args.match_id == [8001, 8002]
