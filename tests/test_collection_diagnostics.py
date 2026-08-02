import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from collection.diagnostics import (
    CollectionDiagnostic, DiagnosticStatus, human_summary,
)


def diagnostic(**overrides):
    values = dict(
        operation="collect_match_player_stats", domain="match_player_stats",
        source_family="cfs_json", collector="MatchPlayerStatsCollector",
        mode="persistent", database_opened=True,
        persistence_target="cfs_player_stats", result_status="concluded",
        fallback_allowed=False, fallback_occurred=False,
    )
    values.update(overrides)
    return CollectionDiagnostic(**values)


def test_human_and_json_are_derived_from_the_same_stable_fields():
    result = diagnostic(rows_written=44, records_received=45, records_rejected=1)
    payload = json.loads(json.dumps(result.to_dict()))
    human = dict(line.split("=", 1) for line in human_summary(result).splitlines())

    assert payload["operation"] == human["operation"]
    assert payload["source_family"] == human["source_family"]
    assert payload["persistence_target"] == human["persistence_target"]
    assert payload["fallback_allowed"] is False
    assert human["fallback_allowed"] == "false"
    assert payload["rows_inserted"] is None  # no fabricated insert/update split
    assert "rows_inserted" not in human


@pytest.mark.parametrize("status", [item.value for item in DiagnosticStatus])
def test_all_documented_statuses_remain_distinct(status):
    assert diagnostic(result_status=status).to_dict()["result_status"] == status


def test_invalid_status_and_database_free_persistence_are_rejected():
    with pytest.raises(ValueError, match="unsupported diagnostic status"):
        diagnostic(result_status="published")
    with pytest.raises(ValueError, match="database-free"):
        diagnostic(mode="database_free", database_opened=False,
                   persistence_target="player_stats")


def test_sensitive_detail_is_redacted_and_never_echoed():
    result = diagnostic(
        source_endpoint="https://example.test/stats?token=hunter2",
        result_detail="Authorization: Bearer-secret cookie=session-value",
    )
    encoded = json.dumps(result.to_dict())
    assert "hunter2" not in encoded
    assert "session-value" not in encoded
    assert "<redacted>" in encoded


def test_lineup_cli_reports_supported_persistent_html_mode(monkeypatch, capsys):
    import cli_runtime
    from db import connection, import_to_db, scrape_runs
    from scraper import scrape_afl_lineups
    from utils import log

    class Connection:
        row_factory = None

        def close(self):
            pass

    @contextmanager
    def audit(*_args, **_kwargs):
        yield {"run_id": "audit-1", "rows_read": None, "rows_written": None}

    monkeypatch.setattr(connection, "get_db_connection", Connection)
    monkeypatch.setattr(scrape_runs, "audited_scrape_run", audit)
    monkeypatch.setattr(scrape_afl_lineups, "scrape_team_lineups",
                        lambda **_kwargs: [{"player_id": 1}])
    monkeypatch.setattr(import_to_db, "save_lineups_to_db",
                        lambda records, conn, round_number: len(records))
    monkeypatch.setattr(log, "log", lambda *_args, **_kwargs: None)

    cli_runtime.handle_scrape_lineups(SimpleNamespace(scrape_lineups=9, print_json=False))

    output = json.loads(capsys.readouterr().out)
    assert output["source_family"] == "html"
    assert output["mode"] == "persistent"
    assert output["database_opened"] is True
    assert output["fallback_allowed"] is False
    assert output["fallback_occurred"] is False
