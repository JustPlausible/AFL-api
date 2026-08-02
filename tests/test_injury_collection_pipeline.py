from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from db.migration_runner import migrate_database
from scraper.injuries.acquisition import INJURY_URL, InjuryAcquirer
from scraper.injuries.models import (
    InjuryParseResult, InjuryResolutionResult, InjurySourceDocument,
    ParsedInjuryRecord, ResolvedInjuryRecord,
)
from scraper.injuries.orchestration import collect_injuries
from scraper.injuries.parser import parse_injuries_html
from scraper.injuries.persistence import InjuryPersistenceAdapter

FIXTURE = Path("tests/fixtures/afl_sources/html_rendered/injuries_round_21_populated.html")


class _Page:
    def __init__(self, html, error=None): self.html, self.error = html, error
    def goto(self, url, timeout):
        assert (url, timeout) == (INJURY_URL, 60_000)
        if self.error: raise self.error
    def wait_for_selector(self, selector, timeout): assert timeout == 15_000
    def content(self): return self.html


class _Browser:
    def __init__(self, page): self.page, self.closed = page, False
    def new_page(self): return self.page
    def close(self): self.closed = True


class _Playwright:
    def __init__(self, browser):
        self.chromium = type("Chromium", (), {"launch": lambda _, **kwargs: browser})()
    def __enter__(self): return self
    def __exit__(self, *_): pass


def test_acquisition_returns_document_and_closes_browser_without_downstream_work():
    browser = _Browser(_Page(FIXTURE.read_text()))
    document = InjuryAcquirer(lambda: _Playwright(browser)).acquire()
    assert document.source_url == INJURY_URL
    assert "Jordan Example" in document.html
    assert document.acquired_at and document.elapsed_ms >= 0
    assert browser.closed


def test_acquisition_navigation_failure_closes_browser():
    browser = _Browser(_Page("", RuntimeError("navigation failed")))
    with pytest.raises(RuntimeError, match="navigation failed"):
        InjuryAcquirer(lambda: _Playwright(browser)).acquire()
    assert browser.closed


def test_acquisition_rejects_missing_required_content():
    with pytest.raises(ValueError, match="required article content"):
        InjuryAcquirer(lambda: _Playwright(_Browser(_Page("<html/>")))).acquire()


def test_pure_parser_reports_counts_raw_markers_and_optional_diagnostic():
    parsed = parse_injuries_html(FIXTURE.read_text())
    assert parsed.team_count == 2 and len(parsed.records) == 1
    assert parsed.records[0].player_name == "Jordan Example"
    assert parsed.records[0].club_image_alt == "Adelaide Crows"
    changed = FIXTURE.read_text().replace("Updated: July 28, 2026", "", 1)
    assert parse_injuries_html(changed).diagnostics[0].code == "missing_optional_updated"


def _database(tmp_path):
    path = tmp_path / "pipeline.db"
    migrate_database(path)
    return sqlite3.connect(path)


def _resolved():
    source = ParsedInjuryRecord("Safe Player", "Knee", "1 week", "Today", "x", "Club")
    return InjuryResolutionResult((ResolvedInjuryRecord(source, "resolved", "ADE", 1, 123),))


def _document():
    return InjurySourceDocument("<html/>", "offline-fixture", "now", 0)


def test_persistence_accepts_resolved_records_and_honours_database(tmp_path):
    conn = _database(tmp_path)
    result = InjuryPersistenceAdapter(conn).persist(_resolved(), _document())
    assert (result.rows_parsed, result.rows_resolved, result.rows_persisted) == (1, 1, 1)
    assert conn.execute("SELECT afl_id,player_name,current FROM injuries").fetchall() == [
        (123, "Safe Player", 1)
    ]
    conn.close()


def test_persistence_rolls_back_the_whole_stage_on_failure(tmp_path):
    conn = _database(tmp_path)
    conn.execute("CREATE TRIGGER reject_injury BEFORE INSERT ON injuries BEGIN SELECT RAISE(FAIL, 'no'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        InjuryPersistenceAdapter(conn).persist(_resolved(), _document())
    assert conn.execute("SELECT COUNT(*) FROM injuries").fetchone() == (0,)
    conn.close()


class _Acquirer:
    def acquire(self): return _document()


class _Resolver:
    def __init__(self, _conn): pass
    def resolve(self, _parsed): return _resolved()


def test_orchestration_completes_one_audit_and_returns_structured_outcome(tmp_path):
    conn = _database(tmp_path)
    parser = lambda _html: InjuryParseResult((_resolved().records[0].source,), 1)
    result = collect_injuries(conn, acquirer=_Acquirer(), parser=parser,
                              resolver_factory=_Resolver)
    assert result.status == "success" and result.rows_persisted == 1
    assert conn.execute("SELECT status,rows_read,rows_written FROM scrape_runs").fetchone() == (
        "completed", 1, 1
    )
    conn.close()


@pytest.mark.parametrize("stage", ["acquisition", "parser", "persistence"])
def test_orchestration_marks_stage_exceptions_failed(tmp_path, stage):
    conn = _database(tmp_path)
    class BoomAcquirer:
        def acquire(self): raise RuntimeError("stage failed")
    class BoomPersistence:
        def __init__(self, _conn): pass
        def persist(self, *_): raise RuntimeError("stage failed")
    kwargs = {"acquirer": _Acquirer(), "parser": lambda _html: InjuryParseResult((), 1),
              "resolver_factory": _Resolver}
    if stage == "acquisition": kwargs["acquirer"] = BoomAcquirer()
    elif stage == "parser": kwargs["parser"] = lambda _html: (_ for _ in ()).throw(RuntimeError("stage failed"))
    else: kwargs["persistence_factory"] = BoomPersistence
    with pytest.raises(RuntimeError, match="stage failed"):
        collect_injuries(conn, **kwargs)
    assert conn.execute("SELECT status,error_class FROM scrape_runs").fetchone() == (
        "failed", "RuntimeError"
    )
    conn.close()


def test_cli_injury_command_uses_operational_policy(monkeypatch, capsys):
    import cli_runtime
    from collection import source_policy
    expected = source_policy.CollectionOutcome(
        "injuries", "html", "collector", True, False, None, "success", 1, 1,
        details={"rows_parsed": 1, "rows_persisted": 1, "status": "success"},
    )
    calls = []
    monkeypatch.setattr(source_policy, "collect_operational",
                        lambda domain, **kwargs: calls.append((domain, kwargs)) or expected)
    args = type("Args", (), {"print_json": False})()
    cli_runtime.handle_scrape_injuries(args)
    assert calls == [(source_policy.OperationalDomain.INJURIES,
                      {"trigger_source": "cli"})]
    assert '"rows_persisted": 1' in capsys.readouterr().out
