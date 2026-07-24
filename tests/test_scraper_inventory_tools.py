from pathlib import Path

from tools import inspect_scraper_source, scraper_inventory


def test_validate_inventory_document_passes():
    assert scraper_inventory.validate_inventory(Path("docs/scraper_source_inventory.md")) == []


def test_validate_inventory_detects_missing_active_section(tmp_path):
    doc = tmp_path / "inventory.md"
    doc.write_text("# empty\n", encoding="utf-8")
    errors = scraper_inventory.validate_inventory(doc)
    assert any("fixtures-rounds" in error for error in errors)


def test_inspect_html_reports_selectors_json_html_urls_and_redacts_headers():
    html = """
    <html><head>
      <script id="__NEXT_DATA__" type="application/json">{"props": {}}</script>
      <script>fetch('/api/matches?round=1')</script>
      <img src="https://static.example.test/logo.png">
    </head><body><div class="fixtures__item" data-match-id="1"></div></body></html>
    """
    result = inspect_scraper_source.inspect_html(
        "plain-http",
        "https://example.test",
        html,
        {"match_card": ".fixtures__item", "missing": ".missing"},
        field_checks={"match_id": {"selector": ".fixtures__item", "attribute": "data-match-id"}},
        status_code=200,
        final_url="https://example.test",
        headers={"Authorization": "secret", "Content-Type": "text/html"},
    )
    assert result.selector_presence == {"match_card": 1, "missing": 0}
    assert result.field_presence == {"match_id": True}
    assert "__NEXT_DATA__" in result.embedded_json_candidates
    assert "/api/matches?round=1" in result.html_url_candidates
    assert result.observed_network_requests == []
    assert "https://static.example.test/logo.png" not in result.html_url_candidates
    assert result.headers["Authorization"] == "[REDACTED]"
    assert result.headers["Content-Type"] == "text/html"


def test_redact_headers_handles_cookie_case_insensitively():
    assert inspect_scraper_source.redact_headers({"Cookie": "a=b", "X-Test": "ok"}) == {"Cookie": "[REDACTED]", "X-Test": "ok"}


def test_fixture_match_preset_compares_selector_and_field_buckets_when_both_modes_succeed():
    raw = inspect_scraper_source.inspect_html(
        "plain-http",
        "https://example.test",
        '<div class="js-react-fixtures" data-season-id="85"></div>',
        {"fixture_root": "div.js-react-fixtures", "match_card": "div.fixtures__item"},
        field_checks={
            "season_id": {"selector": "div.js-react-fixtures", "attribute": "data-season-id"},
            "match_id": {"selector": "div.fixtures__item", "attribute": "data-match-id"},
        },
    )
    rendered = inspect_scraper_source.inspect_html(
        "playwright-rendered",
        "https://example.test",
        '<div class="js-react-fixtures" data-season-id="85"></div><div class="fixtures__item" data-match-id="1"></div>',
        {"fixture_root": "div.js-react-fixtures", "match_card": "div.fixtures__item"},
        field_checks={
            "season_id": {"selector": "div.js-react-fixtures", "attribute": "data-season-id"},
            "match_id": {"selector": "div.fixtures__item", "attribute": "data-match-id"},
        },
    )
    assert inspect_scraper_source.comparison_matrix([raw, rendered], "selector_presence") == {
        "raw_http_only": [],
        "rendered_html_only": ["match_card"],
        "both": ["fixture_root"],
        "neither": [],
        "unknown": [],
    }
    assert inspect_scraper_source.comparison_matrix([raw, rendered], "field_presence") == {
        "raw_http_only": [],
        "rendered_html_only": ["match_id"],
        "both": ["season_id"],
        "neither": [],
        "unknown": [],
    }


def test_failed_mode_comparison_marks_values_unknown_not_neither_or_raw_only():
    raw = inspect_scraper_source.inspect_html(
        "plain-http",
        "https://example.test",
        '<div class="js-react-fixtures" data-season-id="85"></div>',
        {"fixture_root": "div.js-react-fixtures", "match_card": "div.fixtures__item"},
    )
    rendered_error = inspect_scraper_source.ResponseInspection(
        "playwright-rendered",
        "https://example.test",
        None,
        None,
        {},
        0,
        {"fixture_root": 0, "match_card": 0},
        {},
        [],
        [],
        [],
        error=inspect_scraper_source.InspectionError("playwright_browser_missing", "Chromium browser executable is missing", "install"),
    )
    matrix = inspect_scraper_source.comparison_matrix([raw, rendered_error], "selector_presence")
    assert matrix["raw_http_only"] == []
    assert matrix["neither"] == []
    assert matrix["unknown"] == ["fixture_root", "match_card"]
    assert "Both plain HTTP and Playwright-rendered inspections must succeed" in matrix["not_compared_reason"]


def test_dependency_context_documents_playwright_installation():
    context = inspect_scraper_source.dependency_context()
    assert context["playwright_requirement"] == "playwright==1.61.0 in requirements.txt"
    assert context["playwright_browser_install_hint"] == "python -m playwright install chromium"
    assert context["playwright_linux_deps_hint"] == "sudo python -m playwright install-deps chromium"
    assert context["playwright_fresh_linux_hint"] == "sudo python -m playwright install --with-deps chromium"
    assert "mcr.microsoft.com/playwright/python" in context["docker_browser_stack"]


def test_classify_missing_chromium_error_is_structured_and_concise_by_default():
    error = inspect_scraper_source.classify_playwright_error(Exception("BrowserType.launch: Executable doesn't exist at /tmp/chromium\nfull log"))
    assert error.code == "playwright_browser_missing"
    assert error.summary == "Chromium browser executable is missing"
    assert error.missing_executable == "/tmp/chromium"
    assert "python -m playwright install chromium" in error.remediation
    assert error.diagnostic is None


def test_classify_missing_linux_library_error_recommends_install_deps():
    error = inspect_scraper_source.classify_playwright_error(Exception("error while loading shared libraries: libnspr4.so: cannot open shared object file"))
    assert error.code == "playwright_system_dependency_missing"
    assert error.summary == "Chromium system dependencies are missing"
    assert error.missing_library == "libnspr4.so"
    assert "sudo python -m playwright install-deps chromium" in error.remediation
    assert "sudo python -m playwright install --with-deps chromium" in error.remediation


def test_verbose_playwright_error_includes_diagnostic_output():
    error = inspect_scraper_source.classify_playwright_error(Exception("Browser logs here"), verbose=True)
    assert error.code == "playwright_launch_failed"
    assert error.diagnostic == "Browser logs here"


def test_terminal_summary_is_concise_for_failed_rendered_mode():
    raw = inspect_scraper_source.ResponseInspection("plain-http", "u", 200, "u", {}, 10, {}, {}, [], [], [])
    rendered = inspect_scraper_source.ResponseInspection(
        "playwright-rendered",
        "u",
        None,
        None,
        {},
        0,
        {},
        {},
        [],
        [],
        [],
        error=inspect_scraper_source.InspectionError(
            "playwright_system_dependency_missing",
            "Chromium system dependencies are missing",
            "sudo python -m playwright install-deps chromium",
            missing_library="libnspr4.so",
            diagnostic="full browser lifecycle log",
        ),
    )
    summary = inspect_scraper_source.render_terminal_summary([raw, rendered])
    assert "INSPECTION INCOMPLETE" in summary
    assert "Plain HTTP: SUCCESS" in summary
    assert "Playwright: FAILED — Chromium system dependencies are missing" in summary
    assert "sudo python -m playwright install-deps chromium" in summary
    assert "full browser lifecycle log" not in summary


def test_verbose_candidate_lists_include_filtered_static_assets_separately():
    html = '<a href="/api/fixtures?Round=1"></a><img src="https://example.test/image.png"><a href="https://ad.doubleclick.net/ad"></a>'
    result = inspect_scraper_source.inspect_html("plain-http", "u", html, {}, verbose=True)
    assert result.html_url_candidates == ["/api/fixtures?Round=1"]
    assert "https://example.test/image.png" in result.unfiltered_html_url_candidates
    assert "https://ad.doubleclick.net/ad" in result.unfiltered_html_url_candidates
