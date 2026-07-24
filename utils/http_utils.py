# utils/http_utils.py

from __future__ import annotations

import errno
import os
import random
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from requests import exceptions as requests_exceptions
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

import config
from utils.log import log

SCRAPER_USER_AGENT = "AFL-api/1.0 (+https://github.com/JustPlausible/AFL-api)"
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 20.0
DEFAULT_TIMEOUT = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_BACKOFF_MAX_SECONDS = 8.0
DEFAULT_BACKOFF_JITTER_SECONDS = 0.25
DEFAULT_RETRY_AFTER_MAX_SECONDS = 10.0
DEFAULT_RATE_LIMIT_SECONDS = 1.0
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
SENSITIVE_HEADER_NAMES = frozenset({
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "apikey",
})
SENSITIVE_QUERY_NAMES = frozenset({
    "api_key",
    "apikey",
    "key",
    "token",
    "access_token",
    "auth",
    "authorization",
    "password",
    "secret",
})
TRANSIENT_EXCEPTIONS = (
    requests.ConnectionError,
    requests.Timeout,
    requests_exceptions.ChunkedEncodingError,
    requests_exceptions.ContentDecodingError,
)

Clock = Callable[[], float]
Sleeper = Callable[[float], None]
Jitter = Callable[[float, float], float]


def _default_headers() -> dict[str, str]:
    return {"User-Agent": SCRAPER_USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}


def _host_key(url: str) -> str:
    parsed = urlsplit(url)
    return (parsed.hostname or "").lower()


def sanitize_url(url: str) -> str:
    parsed = urlsplit(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    safe_query = urlencode([
        (k, "[REDACTED]" if k.lower() in SENSITIVE_QUERY_NAMES else v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
    ])
    return urlunsplit((parsed.scheme, netloc, parsed.path, safe_query, ""))


def redact_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    return {
        k: ("[REDACTED]" if k.lower() in SENSITIVE_HEADER_NAMES else v)
        for k, v in (headers or {}).items()
    }


class ScraperHttpError(requests.RequestException):
    def __init__(self, message: str, *, method: str, url: str, status_code: int | None, attempts: int):
        super().__init__(message)
        self.method = method.upper()
        self.url = sanitize_url(url)
        self.status_code = status_code
        self.attempts = attempts


@dataclass
class PerHostRateLimiter:
    min_interval_seconds: float = DEFAULT_RATE_LIMIT_SECONDS
    clock: Clock = time.monotonic
    sleep: Sleeper = time.sleep
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _next_allowed: dict[str, float] = field(default_factory=dict)

    def wait(self, url: str) -> None:
        host = _host_key(url)
        if not host or self.min_interval_seconds <= 0:
            return
        with self._lock:
            now = self.clock()
            allowed = self._next_allowed.get(host, now)
            wait_for = max(0.0, allowed - now)
            self._next_allowed[host] = max(now, allowed) + self.min_interval_seconds
        if wait_for > 0:
            self.sleep(wait_for)

    def reset(self) -> None:
        with self._lock:
            self._next_allowed.clear()


@dataclass(frozen=True)
class ScraperHttpPolicy:
    timeout: tuple[float, float] = DEFAULT_TIMEOUT
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS
    backoff_jitter_seconds: float = DEFAULT_BACKOFF_JITTER_SECONDS
    retry_after_max_seconds: float = DEFAULT_RETRY_AFTER_MAX_SECONDS
    rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS


class ScraperHttpClient:
    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        policy: ScraperHttpPolicy | None = None,
        rate_limiter: PerHostRateLimiter | None = None,
        sleep: Sleeper = time.sleep,
        clock: Clock = time.monotonic,
        jitter: Jitter | None = None,
    ):
        self.session = session or requests.Session()
        self.policy = policy or ScraperHttpPolicy()
        self.sleep = sleep
        self.clock = clock
        self.jitter = jitter or random.uniform
        self.rate_limiter = rate_limiter or PerHostRateLimiter(self.policy.rate_limit_seconds, clock, sleep)

    def close(self) -> None:
        self.session.close()

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None, timeout=None, **kwargs) -> requests.Response:
        merged_headers = _default_headers()
        if headers:
            merged_headers.update(headers)
        timeout = timeout or self.policy.timeout
        last_exc: Exception | None = None
        response: requests.Response | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            self.rate_limiter.wait(url)
            try:
                response = self.session.request(method.upper(), url, headers=merged_headers, timeout=timeout, **kwargs)
            except TRANSIENT_EXCEPTIONS as exc:
                last_exc = exc
                if attempt >= self.policy.max_attempts:
                    raise self._error(method, url, None, attempt, f"transient transport failure: {exc.__class__.__name__}") from exc
                self._sleep_before_retry(attempt, None)
                continue
            except requests.RequestException as exc:
                raise self._error(method, url, None, attempt, f"non-retryable transport failure: {exc.__class__.__name__}") from exc

            if response.status_code not in RETRYABLE_STATUS_CODES:
                if 400 <= response.status_code < 600:
                    raise self._error(method, url, response.status_code, attempt, "non-retryable HTTP status")
                return response
            if attempt >= self.policy.max_attempts:
                raise self._error(method, url, response.status_code, attempt, "retryable HTTP status persisted")
            self._sleep_before_retry(attempt, response)
        raise self._error(method, url, getattr(response, "status_code", None), self.policy.max_attempts, str(last_exc or "request failed"))

    def _sleep_before_retry(self, attempt: int, response: requests.Response | None) -> None:
        delay = self._retry_after_delay(response) if response is not None else None
        if delay is None:
            exponential = min(self.policy.backoff_max_seconds, self.policy.backoff_base_seconds * (2 ** (attempt - 1)))
            delay = min(self.policy.backoff_max_seconds, exponential + self.jitter(0.0, self.policy.backoff_jitter_seconds))
        self.sleep(max(0.0, delay))

    def _retry_after_delay(self, response: requests.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - self.clock()
            except (TypeError, ValueError, OverflowError):
                return None
        return min(max(0.0, delay), self.policy.retry_after_max_seconds)

    def _error(self, method: str, url: str, status_code: int | None, attempts: int, reason: str) -> ScraperHttpError:
        status = f" status={status_code}" if status_code is not None else ""
        return ScraperHttpError(
            f"{method.upper()} {sanitize_url(url)} failed after {attempts} attempt(s){status}: {reason}",
            method=method,
            url=url,
            status_code=status_code,
            attempts=attempts,
        )


_default_client = ScraperHttpClient()


def get_scraper_http_client() -> ScraperHttpClient:
    return _default_client


def reset_scraper_http_client(client: ScraperHttpClient | None = None) -> ScraperHttpClient:
    global _default_client
    _default_client.close()
    _default_client = client or ScraperHttpClient()
    return _default_client


def is_blocked(response: requests.Response) -> bool:
    return response.status_code in [403, 429] or "Access Denied" in response.text or "Are you a robot" in response.text


def respectful_sleep(min_seconds=1.5, max_seconds=3.5):
    sleep_time = random.uniform(min_seconds, max_seconds)
    print(f"⏱ Sleeping for {sleep_time:.2f}s")
    time.sleep(sleep_time)


def scrape_with_backoff(url: str, max_retries: int = 3, min_sleep: float = 1.5, max_sleep: float = 3.5, timeout: int | None = None) -> requests.Response | None:
    policy = ScraperHttpPolicy(
        timeout=(DEFAULT_CONNECT_TIMEOUT, float(timeout or config.SCRAPER_TIMEOUT or DEFAULT_READ_TIMEOUT)),
        max_attempts=max_retries,
        backoff_base_seconds=min_sleep,
        backoff_max_seconds=max_sleep,
        backoff_jitter_seconds=max(0.0, max_sleep - min_sleep),
    )
    client = ScraperHttpClient(policy=policy)
    try:
        return client.get(url)
    except requests.HTTPError as exc:
        print(f"⚠️ Unexpected status: {exc.response.status_code if exc.response else 'unknown'}")
        return None
    except ScraperHttpError as exc:
        print(f"❗ {exc}")
        return None
    finally:
        client.close()


def load_page_with_playwright(url: str, wait_time: float = 3.0, screenshot_on_error: bool = True) -> str | None:
    log(f"🌐 Launching Playwright browser for: {url}", "INFO")
    screenshot_path = os.path.join("logs", "playwright_error.png")
    os.makedirs("logs", exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=SCRAPER_USER_AGENT, viewport={"width": 1280, "height": 800})
            page = context.new_page()
            response = page.goto(url, timeout=int(DEFAULT_READ_TIMEOUT * 1000))
            if not response:
                log(f"❌ No response returned from {url}", "ERROR")
                if screenshot_on_error:
                    page.screenshot(path=screenshot_path); log(f"📸 Screenshot saved to {screenshot_path}", "WARN")
                return None
            if response.status != 200:
                log(f"❌ Received HTTP {response.status} from {url}", "ERROR")
                if screenshot_on_error:
                    page.screenshot(path=screenshot_path); log(f"📸 Screenshot saved to {screenshot_path}", "WARN")
                return None
            page.wait_for_timeout(wait_time * 1000)
            content = page.content()
            log("✅ Page loaded successfully via Playwright", "SUCCESS")
            browser.close()
            return content
    except PlaywrightTimeoutError:
        log(f"⏰ Timeout while trying to load: {url}", "ERROR")
        if screenshot_on_error:
            try: page.screenshot(path=screenshot_path); log(f"📸 Screenshot saved to {screenshot_path}", "WARN")
            except Exception: pass
    except OSError as e:
        if e.errno == errno.EAGAIN:
            log("⚠️ Resource temporarily unavailable — retrying after short wait", "WARN")
            time.sleep(5)
            return load_page_with_playwright(url, wait_time, screenshot_on_error)
        log(f"❌ OSError during page load: {e}", "ERROR")
    except Exception as e:
        log(f"❌ Exception occurred during page load: {e}", "ERROR")
        if screenshot_on_error:
            try: page.screenshot(path=screenshot_path); log(f"📸 Screenshot saved to {screenshot_path}", "WARN")
            except Exception: pass
    return None
