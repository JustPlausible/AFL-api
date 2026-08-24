from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from db.migration_runner import migrate_database
from scraper.injuries.acquisition import INJURY_URL, InjuryAcquirer
from scraper.injuries.models import (
    InjuryParseResult, InjuryResolutionResult, InjurySourceDocument,
    ParsedInjuryRecord, ParsedTeamBlock, ResolvedInjuryRecord, ResolvedTeamCoverage,
)
from scraper.injuries.orchestration import collect_injuries
from scraper.injuries.parser import parse_injuries_html
from scraper.injuries.persistence import InjuryPersistenceAdapter
from scraper.injuries.resolution import InjuryResolver, resolve_source_club

FIXTURE = Path("tests/fixtures/afl_sources/html_rendered/injuries_round_21_populated.html")


class _Response:
    def __init__(self, text): self.text = text


class _HttpClient:
    """Stub for utils.http_utils.ScraperHttpClient's relevant surface."""

    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self.error:
            raise self.error
        return self.response


def test_acquisition_returns_document_over_plain_http_without_a_browser():
    client = _HttpClient(_Response(FIXTURE.read_text()))
    document = InjuryAcquirer(client).acquire()
    assert document.source_url == INJURY_URL
    assert "Jordan Example" in document.html
    assert document.acquired_at and document.elapsed_ms >= 0
    assert client.calls == [(INJURY_URL, None)]


def test_acquisition_propagates_http_failure():
    client = _HttpClient(error=RuntimeError("request failed"))
    with pytest.raises(RuntimeError, match="request failed"):
        InjuryAcquirer(client).acquire()


def test_acquisition_rejects_missing_required_content():
    with pytest.raises(ValueError, match="required article content"):
        InjuryAcquirer(_HttpClient(_Response("<html/>"))).acquire()


def test_pure_parser_reports_counts_raw_markers_and_optional_diagnostic():
    parsed = parse_injuries_html(FIXTURE.read_text())
    assert parsed.team_count == 2 and len(parsed.records) == 1
    assert parsed.records[0].player_name == "Jordan Example"
    assert parsed.records[0].club_image_alt == "Adelaide Crows"
    changed = FIXTURE.read_text().replace("Updated: July 28, 2026", "", 1)
    assert parse_injuries_html(changed).diagnostics[0].code == "missing_optional_updated"


def test_pure_parser_records_team_coverage_including_zero_row_block():
    """Every recognised team block is observed coverage, whether or not it has rows.

    The fixture's second team (Carlton) has zero player rows -- that must still
    surface as an explicit, present team block, not be silently absent from the
    parse result (Issue #213 partial-team persistence semantics).
    """
    parsed = parse_injuries_html(FIXTURE.read_text())
    assert len(parsed.teams) == 2
    adelaide, carlton = parsed.teams
    assert (adelaide.team_index, adelaide.row_count, adelaide.club_image_alt) == (0, 1, "Adelaide Crows")
    assert (carlton.team_index, carlton.row_count, carlton.club_image_alt) == (1, 0, "Carlton")


FINALS_STRUCTURE_FIXTURE = Path(
    "tests/fixtures/afl_sources/html_rendered/injuries_2026_finals_bare_table_and_trailing_widget.html"
)


def test_pure_parser_accepts_a_bare_table_sibling_and_skips_a_trailing_non_team_widget():
    """Derived from the real 2026-08-25 finals-window capture pair (Issue #213):

    the live page's plain-HTTP table sibling is a bare ``<table>`` (no
    wrapping ``div.table``, unlike the earlier golden fixture), and the page
    appends one trailing non-team promotional widget sharing the exact same
    ``articleWidget full-width`` + commented-image markup as a real team
    block, but with no following table at all. The parser must accept the
    bare-table shape and treat the untabled trailing widget as non-team
    content rather than raising.
    """
    parsed = parse_injuries_html(FINALS_STRUCTURE_FIXTURE.read_text())
    assert parsed.team_count == 2
    assert len(parsed.teams) == 2
    assert [team.row_count for team in parsed.teams] == [1, 0]
    assert parsed.records[0].player_name == "Hugh Bond"
    assert [(d.code, d.team_index) for d in parsed.diagnostics] == [
        ("missing_optional_updated", 1), ("non_team_widget_skipped", 2),
    ]


REAL_CAPTURE_DIR = Path("docs/investigation/afl-json/samples/injuries")
REAL_CAPTURE_HTTP = REAL_CAPTURE_DIR / "injury_list_2026_finals_10teams_http_2026-08-25.html"
REAL_CAPTURE_RENDERED = REAL_CAPTURE_DIR / "injury_list_2026_finals_10teams_rendered_2026-08-25.html"
FINALS_OBSERVED_TEAMS = [
    "ADE", "BRI", "CAR", "COL", "FRE", "GEE", "HAW", "MEL", "SYD", "WBD",
]


def test_real_2026_finals_capture_pair_parses_identically_from_http_and_rendered():
    """Direct evidence for the acquisition decision (Issue #213): the parser,
    unchanged between acquisition methods, produces materially identical
    output from the plain-HTTP and browser-rendered captures of the same
    live 10-team finals page -- proving plain HTTP alone satisfies the full
    parser contract with no browser execution required."""
    http_parsed = parse_injuries_html(REAL_CAPTURE_HTTP.read_text(encoding="utf-8"))
    rendered_parsed = parse_injuries_html(REAL_CAPTURE_RENDERED.read_text(encoding="utf-8"))

    for parsed in (http_parsed, rendered_parsed):
        assert parsed.team_count == 10
        assert [d.code for d in parsed.diagnostics] == ["non_team_widget_skipped"]
        resolved_codes = [
            resolve_source_club(team.club_image_src, team.club_image_alt)["code"]
            for team in parsed.teams
        ]
        assert resolved_codes == FINALS_OBSERVED_TEAMS

    def normalised(records):
        return [
            (r.player_name, r.injury, r.estimated_return, r.updated,
             r.club_image_src.rsplit("/", 1)[-1].split("?", 1)[0])
            for r in records
        ]

    assert len(http_parsed.records) == len(rendered_parsed.records) == 80
    assert normalised(http_parsed.records) == normalised(rendered_parsed.records)


def _club_stub(mapping):
    def resolver(_src, alt):
        return mapping.get(alt)
    return resolver


class _StubPlayerResolver:
    def resolve(self, name, club_code):
        from merge.helpers import InjuryPlayerResolution
        return InjuryPlayerResolution("resolved", name, club_code, canonical_player_id=1, afl_id=1)


def test_resolver_marks_team_coverage_resolved_only_when_club_marker_is_canonical():
    parsed = InjuryParseResult((), 2, (), (
        ParsedTeamBlock(0, "adelaide.jpg", "Adelaide Crows", "July 28, 2026", 0),
        ParsedTeamBlock(1, "unknown.jpg", "Unknown Team", "", 0),
    ))
    resolver = InjuryResolver(
        conn=None,
        club_resolver=_club_stub({"Adelaide Crows": {"code": "ADE", "teamId": 1}}),
        player_resolver=_StubPlayerResolver(),
    )
    resolved = resolver.resolve(parsed)
    assert [team.status for team in resolved.observed_teams] == ["resolved", "unresolved"]
    assert resolved.observed_teams[0].canonical_team_id == 1
    assert resolved.observed_teams[1].canonical_team_id is None
    assert any(
        diagnostic["reason"] and "team block" in diagnostic["reason"]
        for diagnostic in resolved.diagnostics
    )


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


def test_persistence_stores_resolved_canonical_identity(tmp_path):
    conn = _database(tmp_path)
    InjuryPersistenceAdapter(conn).persist(_resolved(), _document())
    row = conn.execute(
        "SELECT canonical_player_id, canonical_team_id FROM injuries WHERE afl_id=123"
    ).fetchone()
    assert row == (1, None)
    conn.close()


def _player_row(name, afl_id, club_code, canonical_team_id, canonical_player_id=None):
    source = ParsedInjuryRecord(name, "Knee", "1 week", "Today", "x", club_code)
    return ResolvedInjuryRecord(
        source, "resolved", club_code, canonical_player_id if canonical_player_id is not None else afl_id,
        afl_id, canonical_team_id=canonical_team_id,
    )


def _seed_current(conn, afl_id, name, club, canonical_team_id, canonical_player_id, updated="prior"):
    conn.execute(
        "INSERT INTO injuries (afl_id, club, player_name, injury, return_info, updated, "
        "first_updated, source, scraped_at, current, canonical_player_id, canonical_team_id) "
        "VALUES (?, ?, ?, 'Old', 'Old', ?, ?, 'prior-source', 'prior-time', 1, ?, ?)",
        (afl_id, club, name, updated, updated, canonical_player_id, canonical_team_id),
    )
    conn.commit()


def test_partial_team_coverage_preserves_omitted_team_current_rows(tmp_path):
    """A team absent from the latest page's observed coverage keeps its prior current rows."""
    conn = _database(tmp_path)
    _seed_current(conn, 900, "West Coast Player", "WCE", 7, 900)
    resolved = InjuryResolutionResult(
        (_player_row("Adelaide Player", 1, "ADE", 1),),
        observed_teams=(ResolvedTeamCoverage(0, "resolved", "ADE", 1, 1),),
    )
    InjuryPersistenceAdapter(conn).persist(resolved, _document())
    assert conn.execute("SELECT current FROM injuries WHERE afl_id=900").fetchone() == (1,)
    conn.close()


def test_observed_team_with_zero_rows_is_authoritative_empty(tmp_path):
    """A team block present with zero rows expires that team's previously-current rows."""
    conn = _database(tmp_path)
    _seed_current(conn, 901, "Carlton Player", "CAR", 5, 901)
    resolved = InjuryResolutionResult(
        (), observed_teams=(ResolvedTeamCoverage(0, "resolved", "CAR", 5, 0),),
    )
    InjuryPersistenceAdapter(conn).persist(resolved, _document())
    assert conn.execute("SELECT current FROM injuries WHERE afl_id=901").fetchone() == (0,)
    conn.close()


def test_observed_team_expires_player_no_longer_listed_but_keeps_others_current(tmp_path):
    conn = _database(tmp_path)
    # Same `updated` text as the incoming resolved record below: this test is
    # about roster-membership expiry, not the separate changed-updated-text
    # behaviour covered by test_changed_updated_text_retires_the_stale_row_...
    _seed_current(conn, 902, "Still Injured", "ADE", 1, 902, updated="Today")
    _seed_current(conn, 903, "Now Fit", "ADE", 1, 903, updated="Today")
    resolved = InjuryResolutionResult(
        (_player_row("Still Injured", 902, "ADE", 1),),
        observed_teams=(ResolvedTeamCoverage(0, "resolved", "ADE", 1, 1),),
    )
    InjuryPersistenceAdapter(conn).persist(resolved, _document())
    rows = dict(conn.execute("SELECT afl_id, current FROM injuries WHERE afl_id IN (902,903)").fetchall())
    assert rows == {902: 1, 903: 0}
    conn.close()


def test_full_team_coverage_expires_across_every_observed_team(tmp_path):
    conn = _database(tmp_path)
    _seed_current(conn, 904, "Gone A", "ADE", 1, 904)
    _seed_current(conn, 905, "Gone B", "CAR", 5, 905)
    resolved = InjuryResolutionResult(
        (_player_row("New A", 906, "ADE", 1), _player_row("New B", 907, "CAR", 5)),
        observed_teams=(
            ResolvedTeamCoverage(0, "resolved", "ADE", 1, 1),
            ResolvedTeamCoverage(1, "resolved", "CAR", 5, 1),
        ),
    )
    InjuryPersistenceAdapter(conn).persist(resolved, _document())
    current_ids = {row[0] for row in conn.execute("SELECT afl_id FROM injuries WHERE current=1").fetchall()}
    assert current_ids == {906, 907}
    conn.close()


def test_unresolved_identity_anywhere_blocks_all_expiry_even_for_observed_teams(tmp_path):
    conn = _database(tmp_path)
    _seed_current(conn, 908, "Should Survive", "ADE", 1, 908)
    unresolved_source = ParsedInjuryRecord("Mystery Player", "Knee", "1 week", "Today", "x", "ADE")
    resolved = InjuryResolutionResult(
        (_player_row("New Player", 909, "ADE", 1),
         ResolvedInjuryRecord(unresolved_source, "unresolved", reason="no canonical match")),
        observed_teams=(ResolvedTeamCoverage(0, "resolved", "ADE", 1, 2),),
    )
    result = InjuryPersistenceAdapter(conn).persist(resolved, _document())
    assert result.status == "partial"
    assert conn.execute("SELECT current FROM injuries WHERE afl_id=908").fetchone() == (1,)
    conn.close()


def test_ambiguous_identity_anywhere_blocks_all_expiry_even_for_observed_teams(tmp_path):
    conn = _database(tmp_path)
    _seed_current(conn, 910, "Should Also Survive", "ADE", 1, 910)
    ambiguous_source = ParsedInjuryRecord("Two Matches", "Knee", "1 week", "Today", "x", "ADE")
    resolved = InjuryResolutionResult(
        (_player_row("New Player", 911, "ADE", 1),
         ResolvedInjuryRecord(ambiguous_source, "ambiguous", "ADE", reason="two canonical matches")),
        observed_teams=(ResolvedTeamCoverage(0, "resolved", "ADE", 1, 2),),
    )
    result = InjuryPersistenceAdapter(conn).persist(resolved, _document())
    assert result.status == "partial"
    assert conn.execute("SELECT current FROM injuries WHERE afl_id=910").fetchone() == (1,)
    conn.close()


def test_changed_updated_text_retires_the_stale_row_for_the_same_player(tmp_path):
    """A player still listed but whose source `Updated:` text changed must not
    leave two current=1 rows for the same afl_id (PR #214 review finding)."""
    conn = _database(tmp_path)
    _seed_current(conn, 913, "Still Injured", "ADE", 1, 913, updated="July 28, 2026")
    resolved = InjuryResolutionResult(
        (ResolvedInjuryRecord(
            ParsedInjuryRecord("Still Injured", "Knee", "1 week", "August 18, 2026", "x", "ADE"),
            "resolved", "ADE", 913, 913, canonical_team_id=1,
        ),),
        observed_teams=(ResolvedTeamCoverage(0, "resolved", "ADE", 1, 1),),
    )
    InjuryPersistenceAdapter(conn).persist(resolved, _document())
    rows = conn.execute(
        "SELECT updated, current FROM injuries WHERE afl_id=913 ORDER BY updated"
    ).fetchall()
    assert rows == [("August 18, 2026", 1), ("July 28, 2026", 0)]
    conn.close()


def test_team_block_with_unresolved_club_marker_does_not_scope_expiry(tmp_path):
    """A team block that itself failed club resolution must not expire anything --
    persistence does not safely know which team's coverage it represents."""
    conn = _database(tmp_path)
    _seed_current(conn, 912, "Unknown Club Player", "XXX", None, 912)
    resolved = InjuryResolutionResult(
        (), observed_teams=(ResolvedTeamCoverage(0, "unresolved", reason="club marker unresolved"),),
    )
    InjuryPersistenceAdapter(conn).persist(resolved, _document())
    assert conn.execute("SELECT current FROM injuries WHERE afl_id=912").fetchone() == (1,)
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


class _CoverageResolver:
    """Stub resolver whose result also carries observed-team coverage."""

    def __init__(self, _conn): pass

    def resolve(self, _parsed):
        source = _resolved().records[0].source
        return InjuryResolutionResult(
            (ResolvedInjuryRecord(source, "resolved", "ADE", 1, 123, canonical_team_id=1),),
            observed_teams=(ResolvedTeamCoverage(0, "resolved", "ADE", 1, 1),),
        )


def test_orchestration_records_source_coverage_provenance_on_the_scrape_run(tmp_path):
    """Issue #213: reuse the existing scrape-run audit for source-coverage provenance."""
    conn = _database(tmp_path)
    parser = lambda _html: InjuryParseResult((_resolved().records[0].source,), 1)
    result = collect_injuries(conn, acquirer=_Acquirer(), parser=parser,
                              resolver_factory=_CoverageResolver)
    assert result.teams_observed == 1
    summary = conn.execute("SELECT diagnostic_summary FROM scrape_runs").fetchone()[0]
    payload = json.loads(summary)
    assert payload["observed_team_count"] == 1
    assert payload["observed_resolved_teams"] == ["ADE"]
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
    output = json.loads(capsys.readouterr().out)
    assert output["rows_persisted"] == 1
    assert output["source_family"] == "html"
    assert output["mode"] == "persistent"
    assert output["database_opened"] is True
    assert output["fallback_allowed"] is False
    assert output["fallback_occurred"] is False
