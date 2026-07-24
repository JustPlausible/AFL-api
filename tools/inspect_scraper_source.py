"""Manual AFL source inspection helper; does not persist captured pages by default."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
from dataclasses import asdict, dataclass
from typing import Iterable

import requests
from bs4 import BeautifulSoup

from scraper.afl_selectors import FIXTURE_SELECTORS, MATCH_CARD_SELECTORS

SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization"}
JSON_PATTERNS = [
    re.compile(r"__NEXT_DATA__", re.I),
    re.compile(r"__APOLLO_STATE__", re.I),
    re.compile(r"application/json", re.I),
    re.compile(r"hydration|hydrate|preloaded|initialState", re.I),
]
HTML_URL_RE = re.compile(r"(?:https?:)?//[^\"'<>\s]+|/[^\"'<>\s]+", re.I)
DATA_CANDIDATE_RE = re.compile(r"(api|graphql|fixture|fixtures|match|matches|round|competition|season|stats|squad|json)", re.I)
STATIC_ASSET_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|ico|css|js|woff2?|ttf|mp4|webm)(?:[?#].*)?$", re.I)
LOW_RELEVANCE_RE = re.compile(r"(doubleclick|googletagmanager|google-analytics|facebook|instagram|twitter|x\.com|youtube|app-store|play\.google|advert|adsystem|scorecardresearch)", re.I)
MISSING_EXECUTABLE_RE = re.compile(r"Executable doesn't exist at (?P<path>[^\n]+)")
MISSING_LIBRARY_RE = re.compile(r"error while loading shared libraries: (?P<library>[^:\s]+)")

FIXTURE_MATCH_PRESET = {
    "description": "Selectors used by scraper.scrape_afl_fixtures and scraper.scrape_afl_matches.",
    "selectors": {
        "fixtures.metadata_root": f"div.{FIXTURE_SELECTORS.METADATA_ROOT_CLASS}",
        "fixtures.round_list_items": FIXTURE_SELECTORS.ROUND_LIST_ITEMS,
        "fixtures.round_label_button": f"{FIXTURE_SELECTORS.ROUND_LIST_ITEMS} {FIXTURE_SELECTORS.ROUND_LABEL_BUTTON}",
        "matches.season_label": MATCH_CARD_SELECTORS.SEASON_LABEL,
        "matches.date_header_or_match_card": MATCH_CARD_SELECTORS.DATE_HEADER_OR_MATCH_CARD,
        "matches.match_card": MATCH_CARD_SELECTORS.MATCH_CARD,
        "matches.home_team_name": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.HOME_TEAM_NAME}",
        "matches.away_team_name": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.AWAY_TEAM_NAME}",
        "matches.venue": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.VENUE}",
        "matches.details_link": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.DETAILS_LINK}",
        "matches.match_time": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.MATCH_TIME}",
        "matches.status_label": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.STATUS_LABEL}",
        "matches.score_total": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.SCORE_TOTAL}",
        "matches.live_clock": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.LIVE_CLOCK}",
    },
    "field_checks": {
        "fixtures.season_pid": {"selector": f"div.{FIXTURE_SELECTORS.METADATA_ROOT_CLASS}", "attribute": "data-season-pid"},
        "fixtures.season_id": {"selector": f"div.{FIXTURE_SELECTORS.METADATA_ROOT_CLASS}", "attribute": "data-season-id"},
        "fixtures.competition_id": {"selector": f"div.{FIXTURE_SELECTORS.METADATA_ROOT_CLASS}", "attribute": "data-competition-id"},
        "fixtures.default_round_id": {"selector": f"div.{FIXTURE_SELECTORS.METADATA_ROOT_CLASS}", "attribute": "data-no-filter-round"},
        "fixtures.special_round": {"selector": f"div.{FIXTURE_SELECTORS.METADATA_ROOT_CLASS}", "attribute": "data-special-round"},
        "fixtures.round_id": {"selector": FIXTURE_SELECTORS.ROUND_LIST_ITEMS, "attribute": "data-round-id"},
        "fixtures.round_label": {"selector": f"{FIXTURE_SELECTORS.ROUND_LIST_ITEMS} {FIXTURE_SELECTORS.ROUND_LABEL_BUTTON}", "text": True},
        "matches.match_id": {"selector": MATCH_CARD_SELECTORS.MATCH_CARD, "attribute": "data-match-id"},
        "matches.match_provider_id": {"selector": MATCH_CARD_SELECTORS.MATCH_CARD, "attribute": "data-match-provider-id"},
        "matches.round_id": {"selector": MATCH_CARD_SELECTORS.MATCH_CARD, "attribute": "data-round-id"},
        "matches.status": {"selector": MATCH_CARD_SELECTORS.MATCH_CARD, "attribute": "data-match-status"},
        "matches.home_team": {"selector": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.HOME_TEAM_NAME}", "text": True},
        "matches.away_team": {"selector": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.AWAY_TEAM_NAME}", "text": True},
        "matches.venue": {"selector": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.VENUE}", "text": True},
        "matches.start_time_utc_candidate": {"selector": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.DETAILS_LINK}", "attribute": "aria-label"},
        "matches.score_home_away_candidate": {"selector": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.SCORE_TOTAL}", "text": True},
        "matches.match_time_label_candidate": {"selector": f"{MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.MATCH_TIME}, {MATCH_CARD_SELECTORS.MATCH_CARD} {MATCH_CARD_SELECTORS.STATUS_LABEL}", "text": True},
    },
}
PRESETS = {"fixture-match": FIXTURE_MATCH_PRESET}
PLAYWRIGHT_INSTALL_CHROMIUM = "python -m playwright install chromium"
PLAYWRIGHT_INSTALL_DEPS = "sudo python -m playwright install-deps chromium"
PLAYWRIGHT_INSTALL_WITH_DEPS = "sudo python -m playwright install --with-deps chromium"
DOCKER_REMEDIATION = "Use the project Docker image if host-level dependency installation is unavailable."

@dataclass
class InspectionError:
    code: str
    summary: str
    remediation: str
    missing_executable: str | None = None
    missing_library: str | None = None
    diagnostic: str | None = None

@dataclass
class ResponseInspection:
    mode: str
    url: str
    status_code: int | None
    final_url: str | None
    headers: dict[str, str]
    byte_length: int
    selector_presence: dict[str, int]
    field_presence: dict[str, bool]
    embedded_json_candidates: list[str]
    html_url_candidates: list[str]
    observed_network_requests: list[str]
    error: InspectionError | None = None
    unfiltered_html_url_candidates: list[str] | None = None
    unfiltered_observed_network_requests: list[str] | None = None


def dependency_context() -> dict[str, str | bool | None]:
    try:
        playwright_version = importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError:
        playwright_version = None
    return {
        "python": "3.11+ recommended; use the project Docker image for the pinned Playwright browser stack",
        "playwright_package_installed": playwright_version is not None,
        "playwright_package_version": playwright_version,
        "playwright_requirement": "playwright==1.61.0 in requirements.txt",
        "playwright_browser_install_hint": PLAYWRIGHT_INSTALL_CHROMIUM,
        "playwright_linux_deps_hint": PLAYWRIGHT_INSTALL_DEPS,
        "playwright_fresh_linux_hint": PLAYWRIGHT_INSTALL_WITH_DEPS,
        "docker_browser_stack": "Dockerfile uses mcr.microsoft.com/playwright/python:v1.61.0-noble",
    }


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("[REDACTED]" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}


def redact_text(value: str) -> str:
    value = re.sub(r"(?i)(authorization|cookie|x-api-key|proxy-authorization)[:=]\s*[^\s,;]+", r"\1=[REDACTED]", value)
    value = re.sub(r"(?i)(token|key|password|secret)=([^&\s]+)", r"\1=[REDACTED]", value)
    return value


def classify_playwright_error(exc: Exception, *, verbose: bool = False) -> InspectionError:
    diagnostic = redact_text(str(exc))
    executable = MISSING_EXECUTABLE_RE.search(diagnostic)
    if executable:
        return InspectionError(
            code="playwright_browser_missing",
            summary="Chromium browser executable is missing",
            missing_executable=executable.group("path").strip(),
            remediation=f"Run `{PLAYWRIGHT_INSTALL_CHROMIUM}` or use the project Docker image.",
            diagnostic=diagnostic if verbose else None,
        )
    library = MISSING_LIBRARY_RE.search(diagnostic)
    if library:
        return InspectionError(
            code="playwright_system_dependency_missing",
            summary="Chromium system dependencies are missing",
            missing_library=library.group("library").strip(),
            remediation=(
                f"Likely host-level command: `{PLAYWRIGHT_INSTALL_DEPS}`. "
                f"For a fresh Linux environment, use `{PLAYWRIGHT_INSTALL_WITH_DEPS}`. {DOCKER_REMEDIATION}"
            ),
            diagnostic=diagnostic if verbose else None,
        )
    return InspectionError(
        code="playwright_launch_failed",
        summary="Playwright browser launch failed",
        remediation=f"Retry with `--verbose` for browser logs, then install missing host dependencies or use the project Docker image. Fresh Linux setup: `{PLAYWRIGHT_INSTALL_WITH_DEPS}`.",
        diagnostic=diagnostic if verbose else None,
    )


def classify_plain_error(exc: Exception, *, verbose: bool = False) -> InspectionError:
    diagnostic = redact_text(str(exc))
    return InspectionError(
        code="plain_http_failed",
        summary="Plain HTTP request failed",
        remediation="Check network access, proxy configuration, DNS, and whether the AFL host is reachable from this environment.",
        diagnostic=diagnostic if verbose else None,
    )


def _field_present(soup: BeautifulSoup, check: dict[str, object]) -> bool:
    for element in soup.select(str(check["selector"])):
        if check.get("text") and element.get_text(strip=True):
            return True
        attr = check.get("attribute")
        if attr and element.has_attr(str(attr)) and element.get(str(attr)) not in (None, ""):
            return True
    return False


def is_relevant_candidate(url: str) -> bool:
    return bool(DATA_CANDIDATE_RE.search(url)) and not STATIC_ASSET_RE.search(url) and not LOW_RELEVANCE_RE.search(url)


def filter_candidates(urls: Iterable[str]) -> list[str]:
    return sorted({url[:250] for url in urls if is_relevant_candidate(url)})[:100]


def extract_html_url_candidates(html: str, *, verbose: bool = False) -> tuple[list[str], list[str] | None]:
    unfiltered = sorted({match.group(0)[:250] for match in HTML_URL_RE.finditer(html)})[:250]
    filtered = filter_candidates(unfiltered)
    return filtered, unfiltered if verbose else None


def inspect_html(mode: str, url: str, html: str, selectors: dict[str, str] | Iterable[str], *, field_checks: dict[str, dict[str, object]] | None = None, status_code: int | None = None, final_url: str | None = None, headers: dict[str, str] | None = None, verbose: bool = False) -> ResponseInspection:
    soup = BeautifulSoup(html, "html.parser")
    selector_map = selectors if isinstance(selectors, dict) else {selector: selector for selector in selectors}
    selector_presence = {name: len(soup.select(selector)) for name, selector in selector_map.items()}
    field_presence = {name: _field_present(soup, check) for name, check in (field_checks or {}).items()}
    json_candidates: list[str] = []
    for script in soup.find_all("script"):
        script_type = script.get("type", "")
        script_id = script.get("id", "")
        body = script.string or script.get_text(" ", strip=True)[:500]
        if any(p.search(script_type) or p.search(script_id) or p.search(body) for p in JSON_PATTERNS):
            label = script_id or script_type or "inline-script"
            json_candidates.append(label[:120])
    html_candidates, unfiltered = extract_html_url_candidates(html, verbose=verbose)
    return ResponseInspection(mode, url, status_code, final_url, redact_headers(headers or {}), len(html.encode("utf-8")), selector_presence, field_presence, json_candidates[:100], html_candidates, [], unfiltered_html_url_candidates=unfiltered)


def _empty_error_result(mode: str, url: str, selectors: dict[str, str] | Iterable[str], field_checks: dict[str, dict[str, object]] | None, error: InspectionError) -> ResponseInspection:
    selector_map = selectors if isinstance(selectors, dict) else {selector: selector for selector in selectors}
    return ResponseInspection(mode, url, None, None, {}, 0, {name: 0 for name in selector_map}, {name: False for name in (field_checks or {})}, [], [], [], error=error)


def fetch_plain(url: str, selectors: dict[str, str] | Iterable[str], field_checks: dict[str, dict[str, object]] | None = None, timeout: float = 20.0, *, verbose: bool = False) -> ResponseInspection:
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "AFL-api-source-inspector/1.0"})
    except requests.RequestException as exc:
        return _empty_error_result("plain-http", url, selectors, field_checks, classify_plain_error(exc, verbose=verbose))
    return inspect_html("plain-http", url, response.text, selectors, field_checks=field_checks, status_code=response.status_code, final_url=response.url, headers=dict(response.headers), verbose=verbose)


def fetch_playwright(url: str, selectors: dict[str, str] | Iterable[str], field_checks: dict[str, dict[str, object]] | None = None, timeout_ms: int = 60000, *, verbose: bool = False) -> ResponseInspection:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
    captured: set[str] = set()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("request", lambda request: captured.add(request.url))
            response = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            html = page.content()
            final_url = page.url
            headers = response.headers if response else {}
            status = response.status if response else None
            browser.close()
    except PlaywrightError as exc:
        return _empty_error_result("playwright-rendered", url, selectors, field_checks, classify_playwright_error(exc, verbose=verbose))
    result = inspect_html("playwright-rendered", url, html, selectors, field_checks=field_checks, status_code=status, final_url=final_url, headers=dict(headers), verbose=verbose)
    unfiltered_observed = sorted(captured)[:250]
    result.observed_network_requests = filter_candidates(unfiltered_observed)[:150]
    if verbose:
        result.unfiltered_observed_network_requests = unfiltered_observed
    return result


def comparison_matrix(results: list[ResponseInspection], key: str) -> dict[str, list[str] | str]:
    raw = next((r for r in results if r.mode == "plain-http"), None)
    rendered = next((r for r in results if r.mode == "playwright-rendered"), None)
    raw_map = getattr(raw, key) if raw else {}
    rendered_map = getattr(rendered, key) if rendered else {}
    names = sorted(set(raw_map) | set(rendered_map))
    if raw is None or rendered is None or raw.error or rendered.error:
        return {
            "raw_http_only": [],
            "rendered_html_only": [],
            "both": [],
            "neither": [],
            "unknown": names,
            "not_compared_reason": "Both plain HTTP and Playwright-rendered inspections must succeed before acquisition-method conclusions can be drawn.",
        }
    matrix: dict[str, list[str] | str] = {"raw_http_only": [], "rendered_html_only": [], "both": [], "neither": [], "unknown": []}
    for name in names:
        in_raw = bool(raw_map.get(name))
        in_rendered = bool(rendered_map.get(name))
        if in_raw and in_rendered:
            matrix["both"].append(name)  # type: ignore[union-attr]
        elif in_raw:
            matrix["raw_http_only"].append(name)  # type: ignore[union-attr]
        elif in_rendered:
            matrix["rendered_html_only"].append(name)  # type: ignore[union-attr]
        else:
            matrix["neither"].append(name)  # type: ignore[union-attr]
    return matrix


def status_summary(result: ResponseInspection) -> str:
    if not result.error:
        return "SUCCESS"
    return f"FAILED — {result.error.summary}"


def suggested_action(results: list[ResponseInspection]) -> str:
    for result in results:
        if result.error:
            return result.error.remediation
    return "Review selector/field comparison and candidate data-source lists."


def render_terminal_summary(results: list[ResponseInspection]) -> str:
    incomplete = any(result.error for result in results)
    lines = ["INSPECTION INCOMPLETE" if incomplete else "INSPECTION COMPLETE"]
    raw = next((r for r in results if r.mode == "plain-http"), None)
    rendered = next((r for r in results if r.mode == "playwright-rendered"), None)
    if raw:
        lines.append(f"Plain HTTP: {status_summary(raw)}")
    if rendered:
        lines.append(f"Playwright: {status_summary(rendered)}")
    lines.append(f"Suggested action: {suggested_action(results)}")
    if incomplete:
        lines.append("Rendered-page and acquisition-method conclusions cannot be drawn until all required modes succeed.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare plain HTTP and Playwright-rendered AFL source responses without saving captures.",
        epilog=(
            "Fixture/match workflow: python -m tools.inspect_scraper_source "
            "https://www.afl.com.au/fixture?Competition=1\\&Season=85\\&Round=1343 --preset fixture-match. "
            f"If Playwright Chromium is missing, run: {PLAYWRIGHT_INSTALL_CHROMIUM}. "
            f"For fresh Linux hosts, likely command: {PLAYWRIGHT_INSTALL_WITH_DEPS}. "
            "Use the Dockerfile Playwright image when host-level dependency installation is unavailable."
        ),
    )
    parser.add_argument("url", help="AFL page URL to inspect; quote URLs containing '&' in your shell")
    parser.add_argument("--selector", action="append", default=[], help="CSS selector to count; repeatable for custom inspections")
    parser.add_argument("--preset", choices=sorted(PRESETS), help="Documented selector/field set to check; use fixture-match for fixture and match-card contracts")
    parser.add_argument("--mode", choices=("plain", "playwright", "both"), default="both", help="Fetch raw HTTP, Playwright-rendered HTML, or both; default compares both")
    parser.add_argument("--verbose", action="store_true", help="Include underlying exception diagnostics and unfiltered URL/request candidates")
    parser.add_argument("--json-only", action="store_true", help="Print only the machine-readable JSON report")
    args = parser.parse_args(argv)

    preset = PRESETS.get(args.preset) if args.preset else None
    selectors = dict(preset["selectors"]) if preset else {selector: selector for selector in args.selector}
    selectors.update({selector: selector for selector in args.selector})
    field_checks = dict(preset.get("field_checks", {})) if preset else {}

    results = []
    if args.mode in ("plain", "both"):
        results.append(fetch_plain(args.url, selectors, field_checks, verbose=args.verbose))
    if args.mode in ("playwright", "both"):
        results.append(fetch_playwright(args.url, selectors, field_checks, verbose=args.verbose))
    output = {
        "note": "No pages, cookies, credentials, or raw network captures were written to disk.",
        "environment": dependency_context(),
        "preset": args.preset,
        "preset_description": preset.get("description") if preset else None,
        "documentation_mapping": {
            "fixtures-rounds": "docs/scraper_source_inventory.md#source-contract-fixtures-rounds",
            "matches-status": "docs/scraper_source_inventory.md#source-contract-matches-status",
        } if args.preset == "fixture-match" else {},
        "comparison": {
            "selectors": comparison_matrix(results, "selector_presence"),
            "fields": comparison_matrix(results, "field_presence"),
        },
        "acquisition_method_conclusion": "Pending verification until both plain HTTP and Playwright-rendered inspections succeed." if any(result.error for result in results) else "Both requested modes completed; human review is still required before changing scraper dependencies.",
        "results": [asdict(result) for result in results],
        "human_judgement_required": [
            "Decide whether candidate embedded JSON or observed browser requests are stable, documented, and appropriate to treat as dependencies.",
            "Review page states not represented by this URL, including live, completed, postponed, bye, hidden or not-yet-announced states.",
            "Confirm whether missing optional fields are expected for this page state or indicate selector drift.",
        ],
    }
    if not args.json_only:
        print(render_terminal_summary(results))
        print()
    print(json.dumps(output, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
