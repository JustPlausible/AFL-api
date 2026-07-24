"""Manual AFL source inspection helper; does not persist captured pages by default."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from scraper.afl_selectors import FIXTURE_SELECTORS, MATCH_CARD_SELECTORS, PLAYER_STATS_SELECTORS
from utils.http_utils import ScraperHttpClient, ScraperHttpError, ScraperHttpPolicy

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
AFL_API_HOSTS = {"aflapi.afl.com.au"}
SENSITIVE_REQUEST_HEADER_NAMES = {"cookie", "authorization", "x-api-key", "api-key", "apikey", "x-api-token"}
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
PLAYER_STATS_REQUIRED_HEADERS = ("AF", "G", "B", "D", "K", "H", "M", "T", "HO", "CLR", "MG", "GA", "ToG%")
PLAYER_STATS_IDENTITY_HEADERS = {"", "#", "NO.", "PLAYER", "PLAYERS"}
PLAYER_STATS_PRESET = {
    "description": "Selectors used by scraper.scrape_afl_player_stats.",
    "selectors": {
        "player_stats.match_status_label": PLAYER_STATS_SELECTORS.MATCH_STATUS_LABEL,
        "player_stats.stats_table": PLAYER_STATS_SELECTORS.STATS_TABLE,
        "player_stats.header_cells": f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.HEADER_CELLS}",
        "player_stats.body_rows": f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.BODY_ROWS}",
        "player_stats.player_profile_link": f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.PLAYER_PROFILE_LINK}",
        "player_stats.player_headshot": f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.PLAYER_HEADSHOT}",
        "player_stats.jumper_number": f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.JUMPER_NUMBER}",
    },
    "field_checks": {
        "player_stats.match_status_label": {"selector": PLAYER_STATS_SELECTORS.MATCH_STATUS_LABEL, "text": True},
        "player_stats.stats_table": {"selector": PLAYER_STATS_SELECTORS.STATS_TABLE},
        "player_stats.body_rows": {"selector": f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.BODY_ROWS}"},
        "player_stats.player_profile_link": {"selector": f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.PLAYER_PROFILE_LINK}"},
        "player_stats.player_headshot": {"selector": f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.PLAYER_HEADSHOT}"},
        "player_stats.jumper_number": {"selector": f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.JUMPER_NUMBER}"},
    },
}
PRESETS = {"fixture-match": FIXTURE_MATCH_PRESET, "player-stats": PLAYER_STATS_PRESET}
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
class JsonShapeSummary:
    kind: str
    top_level_keys: list[str]
    item_count: int | None = None
    representative_item_keys: list[str] | None = None

@dataclass
class DirectFetchResult:
    attempted: bool
    status_code: int | None
    content_type: str | None
    byte_size: int | None
    error: str | None = None

@dataclass
class DataSourceResponse:
    url: str
    method: str
    resource_type: str
    status: int | None
    content_type: str | None
    request_had_cookie: bool
    request_had_authorization: bool
    request_had_api_key_header: bool
    response_byte_size: int | None
    json_shape: JsonShapeSummary | None
    direct_fetch: DirectFetchResult
    endpoint_access: str

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
    data_source_responses: list[DataSourceResponse] | None = None
    likely_fixture_data_endpoints: list[DataSourceResponse] | None = None
    player_stats_contract: dict[str, object] | None = None
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
        if not check.get("text") and not check.get("attribute"):
            return True
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
    return ResponseInspection(mode, url, status_code, final_url, redact_headers(headers or {}), len(html.encode("utf-8")), selector_presence, field_presence, json_candidates[:100], html_candidates, [], player_stats_contract=inspect_player_stats_contract(html), unfiltered_html_url_candidates=unfiltered)


def _empty_error_result(mode: str, url: str, selectors: dict[str, str] | Iterable[str], field_checks: dict[str, dict[str, object]] | None, error: InspectionError) -> ResponseInspection:
    selector_map = selectors if isinstance(selectors, dict) else {selector: selector for selector in selectors}
    return ResponseInspection(mode, url, None, None, {}, 0, {name: 0 for name in selector_map}, {name: False for name in (field_checks or {})}, [], [], [], error=error)



def summarize_json_shape(body: bytes) -> JsonShapeSummary | None:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        return JsonShapeSummary(kind="object", top_level_keys=sorted(str(key) for key in data.keys())[:50])
    if isinstance(data, list):
        representative = next((item for item in data if isinstance(item, dict)), None)
        return JsonShapeSummary(
            kind="array",
            top_level_keys=[],
            item_count=len(data),
            representative_item_keys=sorted(str(key) for key in representative.keys())[:50] if representative else [],
        )
    return JsonShapeSummary(kind=type(data).__name__, top_level_keys=[])


def request_header_flags(headers: dict[str, str]) -> tuple[bool, bool, bool]:
    lowered = {name.lower() for name in headers}
    return (
        "cookie" in lowered,
        "authorization" in lowered,
        bool(lowered & {"x-api-key", "api-key", "apikey", "x-api-token"}),
    )


def is_likely_data_source(url: str, resource_type: str | None = None, content_type: str | None = None) -> bool:
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() in AFL_API_HOSTS:
        return True
    if content_type and "json" in content_type.lower() and not LOW_RELEVANCE_RE.search(url):
        return True
    if resource_type in {"xhr", "fetch"} and is_relevant_candidate(url):
        return True
    return is_relevant_candidate(url)


def direct_fetch_without_browser_credentials(url: str, method: str) -> DirectFetchResult:
    if method.upper() != "GET":
        return DirectFetchResult(False, None, None, None, "direct fetch skipped for non-GET request")
    client = ScraperHttpClient(policy=ScraperHttpPolicy(max_attempts=1, rate_limit_seconds=0))
    try:
        response = client.get(url)
        return DirectFetchResult(True, response.status_code, response.headers.get("Content-Type"), len(response.content), None)
    except ScraperHttpError as exc:
        return DirectFetchResult(True, exc.status_code, None, None, redact_text(str(exc)))
    except requests.RequestException as exc:
        return DirectFetchResult(True, None, None, None, redact_text(str(exc)))
    finally:
        client.close()


def endpoint_access_classification(*, direct_fetch: DirectFetchResult, observed_status: int | None, had_sensitive_headers: bool) -> str:
    if had_sensitive_headers:
        return "authenticated"
    if direct_fetch.attempted and direct_fetch.status_code is not None and 200 <= direct_fetch.status_code < 400:
        return "public_directly_callable"
    if observed_status is not None and 200 <= observed_status < 400 and direct_fetch.attempted and direct_fetch.status_code is not None and direct_fetch.status_code >= 400:
        return "browser_context_dependent"
    return "inconclusive"


def build_data_source_response(response, *, verbose: bool = False) -> DataSourceResponse | None:
    request = response.request
    url = request.url
    method = request.method.upper()
    resource_type = request.resource_type
    content_type = response.headers.get("content-type") or response.headers.get("Content-Type")
    if not is_likely_data_source(url, resource_type, content_type):
        return None
    headers = request.headers
    had_cookie, had_authorization, had_api_key = request_header_flags(headers)
    body: bytes | None = None
    byte_size: int | None = None
    json_shape: JsonShapeSummary | None = None
    try:
        body = response.body()
        byte_size = len(body)
    except Exception:
        body = None
    if body is not None and content_type and "json" in content_type.lower():
        json_shape = summarize_json_shape(body)
    direct_fetch = direct_fetch_without_browser_credentials(url, method)
    had_sensitive = had_cookie or had_authorization or had_api_key
    return DataSourceResponse(
        url=url,
        method=method,
        resource_type=resource_type,
        status=response.status,
        content_type=content_type,
        request_had_cookie=had_cookie,
        request_had_authorization=had_authorization,
        request_had_api_key_header=had_api_key,
        response_byte_size=byte_size,
        json_shape=json_shape,
        direct_fetch=direct_fetch,
        endpoint_access=endpoint_access_classification(direct_fetch=direct_fetch, observed_status=response.status, had_sensitive_headers=had_sensitive),
    )


def likely_fixture_endpoint_rows(responses: list[DataSourceResponse]) -> list[DataSourceResponse]:
    return [
        response for response in responses
        if (urlsplit(response.url).hostname or "").lower() in AFL_API_HOSTS
        or DATA_CANDIDATE_RE.search(urlsplit(response.url).path)
    ]

def fetch_plain(url: str, selectors: dict[str, str] | Iterable[str], field_checks: dict[str, dict[str, object]] | None = None, timeout: float = 20.0, *, verbose: bool = False) -> ResponseInspection:
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "AFL-api-source-inspector/1.0"})
    except requests.RequestException as exc:
        return _empty_error_result("plain-http", url, selectors, field_checks, classify_plain_error(exc, verbose=verbose))
    return inspect_html("plain-http", url, response.text, selectors, field_checks=field_checks, status_code=response.status_code, final_url=response.url, headers=dict(response.headers), verbose=verbose)


def fetch_playwright(url: str, selectors: dict[str, str] | Iterable[str], field_checks: dict[str, dict[str, object]] | None = None, timeout_ms: int = 60000, *, verbose: bool = False) -> ResponseInspection:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
    captured: set[str] = set()
    observed_responses = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("request", lambda request: captured.add(request.url))
            page.on("response", lambda response: observed_responses.append(response))
            response = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            html = page.content()
            final_url = page.url
            headers = response.headers if response else {}
            status = response.status if response else None
            data_source_responses = [summary for response in observed_responses if (summary := build_data_source_response(response, verbose=verbose)) is not None]
            browser.close()
    except PlaywrightError as exc:
        return _empty_error_result("playwright-rendered", url, selectors, field_checks, classify_playwright_error(exc, verbose=verbose))
    result = inspect_html("playwright-rendered", url, html, selectors, field_checks=field_checks, status_code=status, final_url=final_url, headers=dict(headers), verbose=verbose)
    unfiltered_observed = sorted(captured)[:250]
    result.observed_network_requests = [response.url for response in data_source_responses][:150] or filter_candidates(unfiltered_observed)[:150]
    result.data_source_responses = data_source_responses[:150]
    result.likely_fixture_data_endpoints = likely_fixture_endpoint_rows(data_source_responses)[:50]
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


TRUTH_YES = "Yes"
TRUTH_NO = "No"
TRUTH_UNKNOWN = "Unknown"


def required_contract_field_names(field_checks: dict[str, dict[str, object]]) -> set[str]:
    return {name for name in field_checks if not name.endswith("_candidate")}


def any_embedded_json(results: list[ResponseInspection]) -> bool:
    return any(result.embedded_json_candidates for result in results)


def any_hydration_data(results: list[ResponseInspection]) -> bool:
    hydration_re = re.compile(r"(__NEXT_DATA__|__APOLLO_STATE__|hydration|hydrate|preloaded|initialState)", re.I)
    return any(any(hydration_re.search(candidate) for candidate in result.embedded_json_candidates) for result in results)


def any_structured_api_endpoints(results: list[ResponseInspection]) -> bool:
    return any(result.data_source_responses for result in results)


def contract_satisfied(result: ResponseInspection | None, required_fields: set[str]) -> bool | None:
    if result is None or result.error:
        return None
    if not required_fields:
        return None
    return all(result.field_presence.get(field) for field in required_fields)


def derive_findings(results: list[ResponseInspection], field_checks: dict[str, dict[str, object]] | None = None) -> dict[str, str]:
    field_checks = field_checks or {}
    required_fields = required_contract_field_names(field_checks)
    raw = next((r for r in results if r.mode == "plain-http"), None)
    rendered = next((r for r in results if r.mode == "playwright-rendered"), None)
    raw_satisfied = contract_satisfied(raw, required_fields)
    rendered_satisfied = contract_satisfied(rendered, required_fields)

    if rendered_satisfied is None:
        current_contract = TRUTH_UNKNOWN
    else:
        current_contract = TRUTH_YES if rendered_satisfied else TRUTH_NO

    if raw is None or rendered is None or raw.error or rendered.error or not required_fields:
        rendered_additional = TRUTH_UNKNOWN
        requires_playwright = TRUTH_UNKNOWN
    else:
        rendered_additional_fields = [
            field for field in required_fields
            if rendered.field_presence.get(field) and not raw.field_presence.get(field)
        ]
        rendered_additional = TRUTH_YES if rendered_additional_fields else TRUTH_NO
        if raw_satisfied:
            requires_playwright = TRUTH_NO
        elif rendered_satisfied:
            requires_playwright = TRUTH_YES
        else:
            requires_playwright = TRUTH_UNKNOWN

    structured_api = any_structured_api_endpoints(results)
    if structured_api:
        recommendation = "Investigate structured API"
    elif current_contract == TRUTH_YES:
        recommendation = "Continue current scraper"
    else:
        recommendation = "Inconclusive"

    if raw is None or rendered is None or raw.error or rendered.error:
        recommendation = "Inconclusive"

    return {
        "embedded_json_found": TRUTH_YES if any_embedded_json(results) else TRUTH_NO,
        "hydration_data_found": TRUTH_YES if any_hydration_data(results) else TRUTH_NO,
        "structured_api_endpoints_observed": TRUTH_YES if structured_api else TRUTH_NO,
        "current_scraper_contract_satisfied": current_contract,
        "rendered_page_exposes_additional_required_fields": rendered_additional,
        "page_still_appears_to_require_playwright": requires_playwright,
        "recommendation": recommendation,
        "recommendation_status": "Pending verification",
    }


def render_findings(findings: dict[str, str]) -> str:
    return "\n".join([
        "Findings:",
        f"Embedded JSON found? {findings['embedded_json_found']}",
        f"Hydration data found? {findings['hydration_data_found']}",
        f"Structured API endpoints observed? {findings['structured_api_endpoints_observed']}",
        f"Current scraper contract satisfied? {findings['current_scraper_contract_satisfied']}",
        f"Does the rendered page expose additional required fields? {findings['rendered_page_exposes_additional_required_fields']}",
        f"Does this page still appear to require Playwright? {findings['page_still_appears_to_require_playwright']}",
        f"Recommendation: {findings['recommendation']}",
        f"Recommendation status: {findings['recommendation_status']}",
    ])


def normalize_player_stats_header(value: str) -> str:
    return value.strip().replace("%", "").replace("ToG", "ToG%")


def interpret_match_state(status_label: str | None) -> str:
    if not status_label:
        return "unknown"
    label = status_label.strip().upper()
    if "FULL TIME" in label:
        return "completed"
    if "Q1" in label or "Q2" in label or "Q3" in label or "Q4" in label or "LIVE" in label:
        return "live"
    if any(token in label for token in ("UPCOMING", "SCHEDULED", "BOUNCE", "START", "PM", "AM")):
        return "pre-match"
    return "unknown"


def inspect_player_stats_contract(html: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    status_el = soup.select_one(PLAYER_STATS_SELECTORS.MATCH_STATUS_LABEL)
    status_label = status_el.get_text(" ", strip=True) if status_el else None
    table = soup.select_one(PLAYER_STATS_SELECTORS.STATS_TABLE)
    headers = [normalize_player_stats_header(cell.get_text(" ", strip=True)) for cell in soup.select(f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.HEADER_CELLS}")]
    rows = soup.select(f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.BODY_ROWS}") if table else []
    profile_links = soup.select(f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.PLAYER_PROFILE_LINK}") if table else []
    headshots = soup.select(f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.PLAYER_HEADSHOT}") if table else []
    jumper_numbers = soup.select(f"{PLAYER_STATS_SELECTORS.STATS_TABLE} {PLAYER_STATS_SELECTORS.JUMPER_NUMBER}") if table else []
    required = set(PLAYER_STATS_REQUIRED_HEADERS)
    header_set = set(headers)
    missing_required = [header for header in PLAYER_STATS_REQUIRED_HEADERS if header not in header_set]
    unexpected = [header for header in headers if header and header not in required and header.upper() not in PLAYER_STATS_IDENTITY_HEADERS]
    team_code_rows = 0
    for jumper in jumper_numbers:
        classes = jumper.get("class", [])
        if len(classes) > 1 and any(str(cls).strip() for cls in classes[1:]):
            team_code_rows += 1
    row_count = len(rows)
    profile_links_present = row_count > 0 and len(profile_links) >= row_count
    headshots_present = row_count > 0 and len(headshots) >= row_count
    jumper_numbers_present = row_count > 0 and len(jumper_numbers) >= row_count
    team_codes_present = row_count > 0 and team_code_rows >= row_count
    required_columns_present = not missing_required
    contract_satisfied = bool(
        table and status_el and row_count > 0 and required_columns_present
        and profile_links_present and headshots_present and jumper_numbers_present and team_codes_present
    )
    return {
        "player_stats_table_present": "Yes" if table else "No",
        "match_status_label_present": "Yes" if status_el else "No",
        "interpreted_match_state": interpret_match_state(status_label),
        "detected_table_headers": headers,
        "player_row_count": row_count,
        "player_profile_links_present": "Yes" if profile_links_present else "No",
        "player_headshots_present": "Yes" if headshots_present else "No",
        "jumper_numbers_present": "Yes" if jumper_numbers_present else "No",
        "team_codes_present": "Yes" if team_codes_present else "No",
        "current_required_stat_columns_present": "Yes" if required_columns_present else "No",
        "missing_required_stat_columns": missing_required,
        "unexpected_additional_stat_columns": unexpected,
        "current_player_stats_scraper_contract_satisfied": "Yes" if contract_satisfied else "No",
    }


def player_stats_summary(results: list[ResponseInspection]) -> dict[str, object]:
    raw = next((r for r in results if r.mode == "plain-http"), None)
    rendered = next((r for r in results if r.mode == "playwright-rendered"), None)
    raw_success = bool(raw and not raw.error)
    rendered_success = bool(rendered and not rendered.error)
    # The ResponseInspection intentionally does not retain full HTML. Recompute from selector/field summaries is not
    # sufficient for headers/identity details, so fetch helpers attach this summary after inspection.
    rendered_stats = getattr(rendered, "player_stats_contract", None) if rendered else None
    raw_stats = getattr(raw, "player_stats_contract", None) if raw else None
    summary = dict(rendered_stats or raw_stats or inspect_player_stats_contract(""))
    summary["plain_http_inspection_success"] = "Yes" if raw_success else "No"
    summary["playwright_inspection_success"] = "Yes" if rendered_success else "No"
    rendered_satisfied = rendered_stats and rendered_stats["current_player_stats_scraper_contract_satisfied"] == "Yes"
    raw_satisfied = raw_stats and raw_stats["current_player_stats_scraper_contract_satisfied"] == "Yes"
    if not rendered_success:
        summary["current_player_stats_scraper_contract_satisfied"] = "Inconclusive"
        summary["playwright_required"] = "Inconclusive"
    else:
        summary["current_player_stats_scraper_contract_satisfied"] = "Yes" if rendered_satisfied else "No"
        if raw_success and raw_satisfied:
            summary["playwright_required"] = "No"
        elif rendered_satisfied:
            summary["playwright_required"] = "Yes"
        else:
            summary["playwright_required"] = "Inconclusive"
    structured = bool(rendered and rendered.data_source_responses)
    summary["structured_player_stat_api_responses_observed"] = "Yes" if structured else "No"
    return summary


def render_player_stats_summary(summary: dict[str, object]) -> str:
    return "\n".join([
        "Player stats summary:",
        f"Plain HTTP inspection success: {summary['plain_http_inspection_success']}",
        f"Playwright inspection success: {summary['playwright_inspection_success']}",
        f"Player stats table present: {summary['player_stats_table_present']}",
        f"Match status label present: {summary['match_status_label_present']}",
        f"Interpreted match state: {summary['interpreted_match_state']}",
        f"Detected table headers: {', '.join(summary['detected_table_headers']) if summary['detected_table_headers'] else '(none)'}",
        f"Player row count: {summary['player_row_count']}",
        f"Player profile links present: {summary['player_profile_links_present']}",
        f"Player headshots present: {summary['player_headshots_present']}",
        f"Jumper numbers present: {summary['jumper_numbers_present']}",
        f"Team codes present: {summary['team_codes_present']}",
        f"Current required stat columns present: {summary['current_required_stat_columns_present']}",
        f"Missing required stat columns: {', '.join(summary['missing_required_stat_columns']) if summary['missing_required_stat_columns'] else '(none)'}",
        f"Unexpected additional stat columns: {', '.join(summary['unexpected_additional_stat_columns']) if summary['unexpected_additional_stat_columns'] else '(none)'}",
        f"Current player-stats scraper contract satisfied: {summary['current_player_stats_scraper_contract_satisfied']}",
        f"Playwright required: {summary['playwright_required']}",
        f"Structured player-stat API responses observed: {summary['structured_player_stat_api_responses_observed']}",
    ])

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
    parser.add_argument("--output", type=Path, help="Write the machine-readable JSON inspection report to this path")
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
    findings = derive_findings(results, field_checks)
    stats_summary = player_stats_summary(results) if args.preset == "player-stats" else None
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
        "findings": findings,
        "player_stats_summary": stats_summary,
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
        print(render_findings(findings))
        if stats_summary:
            print()
            print(render_player_stats_summary(stats_summary))
        print()
    rendered_output = json.dumps(output, indent=2)
    if args.output:
        args.output.write_text(rendered_output + "\n", encoding="utf-8")
    print(rendered_output)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
