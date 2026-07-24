from pathlib import Path

from tools import inspect_scraper_source, scraper_inventory


def test_validate_inventory_document_passes():
    assert scraper_inventory.validate_inventory(Path("docs/scraper_source_inventory.md")) == []


def test_validate_inventory_detects_missing_active_section(tmp_path):
    doc = tmp_path / "inventory.md"
    doc.write_text("# empty\n", encoding="utf-8")
    errors = scraper_inventory.validate_inventory(doc)
    assert any("fixtures-rounds" in error for error in errors)


def test_inspect_html_reports_selectors_json_network_and_redacts_headers():
    html = """
    <html><head>
      <script id="__NEXT_DATA__" type="application/json">{"props": {}}</script>
      <script>fetch('/api/matches?round=1')</script>
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
    assert "/api/matches?round=1" in result.network_request_candidates
    assert result.headers["Authorization"] == "[REDACTED]"
    assert result.headers["Content-Type"] == "text/html"


def test_redact_headers_handles_cookie_case_insensitively():
    assert inspect_scraper_source.redact_headers({"Cookie": "a=b", "X-Test": "ok"}) == {"Cookie": "[REDACTED]", "X-Test": "ok"}


def test_fixture_match_preset_compares_selector_and_field_buckets():
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
    }
    assert inspect_scraper_source.comparison_matrix([raw, rendered], "field_presence") == {
        "raw_http_only": [],
        "rendered_html_only": ["match_id"],
        "both": ["season_id"],
        "neither": [],
    }


def test_dependency_context_documents_playwright_installation():
    context = inspect_scraper_source.dependency_context()
    assert context["playwright_requirement"] == "playwright==1.61.0 in requirements.txt"
    assert context["playwright_browser_install_hint"] == "python -m playwright install chromium"
    assert "mcr.microsoft.com/playwright/python" in context["docker_browser_stack"]


def test_format_playwright_error_adds_chromium_install_hint():
    message = inspect_scraper_source.format_playwright_error(Exception("Executable doesn't exist at /tmp/chromium"))
    assert "python -m playwright install chromium" in message
    assert "project Docker image" in message
