import sqlite3
from contextlib import contextmanager

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


@pytest.fixture(autouse=True)
def no_audit_for_lineup_unit_tests(monkeypatch):
    """Keep legacy lineup CLI tests focused on parsing/resolution, not audit persistence."""

    @contextmanager
    def noop_audit(*args, **kwargs):
        yield {"run_id": "unit-test", "rows_read": None, "rows_written": None}

    monkeypatch.setattr(lineups, "audited_scrape_run", noop_audit)


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


def test_match_mode_errors_when_fixture_database_unavailable(monkeypatch):
    calls = []
    monkeypatch.setattr(lineups, "get_db_connection", lambda: (_ for _ in ()).throw(FileNotFoundError("missing db")))
    monkeypatch.setattr(lineups, "scrape_team_lineups", lambda round_number=0: calls.append(round_number) or [])

    with pytest.raises(lineups.MatchRoundResolutionError) as exc:
        lineups.scrape_match_lineup(7043)

    assert "could not open fixture database" in str(exc.value)
    assert calls == []


def test_match_mode_errors_when_matches_schema_unreadable(monkeypatch):
    conn = sqlite3.connect(":memory:")
    calls = []
    monkeypatch.setattr(lineups, "get_db_connection", lambda: conn)
    monkeypatch.setattr(lineups, "scrape_team_lineups", lambda round_number=0: calls.append(round_number) or [])

    with pytest.raises(lineups.MatchRoundResolutionError) as exc:
        lineups.scrape_match_lineup(7043)

    assert "could not read fixture data" in str(exc.value)
    assert calls == []
    conn.close()


def test_match_mode_errors_when_match_cannot_be_resolved(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE matches (match_id INTEGER, round_id INTEGER)")
    calls = []
    monkeypatch.setattr(lineups, "get_db_connection", lambda: conn)
    monkeypatch.setattr(lineups, "scrape_team_lineups", lambda round_number=0: calls.append(round_number) or [])

    with pytest.raises(lineups.MatchRoundResolutionError) as exc:
        lineups.scrape_match_lineup(7043)

    assert "could not resolve match 7043 to a round" in str(exc.value)
    assert calls == []
    conn.close()


def test_main_returns_non_zero_and_logs_when_match_resolution_fails(monkeypatch):
    errors = []
    monkeypatch.setattr(lineups, "get_db_connection", lambda: (_ for _ in ()).throw(FileNotFoundError("missing db")))
    monkeypatch.setattr(lineups.log, "error", errors.append)

    assert lineups.main(["--match", "7043"]) == 1

    assert "Explicit match scrape failed" in errors[0]
    assert "match 7043" in errors[0]

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


def test_scheduler_round_invocation_uses_persistent_html_policy(monkeypatch):
    calls = []
    from collection import source_policy
    monkeypatch.setattr(source_policy, "collect_operational",
                        lambda domain, **kwargs: calls.append((domain, kwargs)))

    schedule_lineup_scrapes.run_lineup_round_scraper(9)

    assert calls == [(source_policy.OperationalDomain.LINEUPS, {"target_id": 9})]


def test_scheduler_match_invocation_resolves_round_for_persistent_html_policy(monkeypatch):
    calls = []
    from collection import source_policy
    monkeypatch.setattr(source_policy, "round_for_match", lambda match_id: 9)
    monkeypatch.setattr(source_policy, "collect_operational",
                        lambda domain, **kwargs: calls.append((domain, kwargs)))

    schedule_lineup_scrapes.run_lineup_match_scraper(7043)

    assert calls == [(source_policy.OperationalDomain.LINEUPS, {"target_id": 9})]


def test_scheduler_propagates_policy_failure_without_html_fallback(monkeypatch):
    from collection import source_policy
    monkeypatch.setattr(source_policy, "round_for_match", lambda match_id: 9)
    monkeypatch.setattr(source_policy, "collect_operational",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("CFS failed")))
    with pytest.raises(RuntimeError, match="CFS failed"):
        schedule_lineup_scrapes.run_lineup_match_scraper(7043)


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
