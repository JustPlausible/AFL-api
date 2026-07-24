import threading

import pytest
import requests

from utils import http_utils


class FakeResponse:
    def __init__(self, status_code=200, headers=None, text="ok"):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.ok = 200 <= status_code < 400
        self.url = "https://example.test/path"

    def raise_for_status(self):
        if 400 <= self.status_code:
            err = requests.HTTPError(f"{self.status_code} error")
            err.response = self
            raise err


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


def client(outcomes, sleeps=None, jitter=lambda a, b: 0.0, policy=None):
    sleeps = sleeps if sleeps is not None else []
    return http_utils.ScraperHttpClient(
        session=FakeSession(outcomes),
        policy=policy or http_utils.ScraperHttpPolicy(rate_limit_seconds=0),
        sleep=sleeps.append,
        jitter=jitter,
        rate_limiter=http_utils.PerHostRateLimiter(0, sleep=sleeps.append),
    )


def test_default_connect_and_read_timeout_configuration():
    assert http_utils.DEFAULT_TIMEOUT == (5.0, 20.0)
    c = client([FakeResponse()])
    c.get("https://afl.example/fixture")
    assert c.session.calls[0][2]["timeout"] == (5.0, 20.0)
    assert c.session.calls[0][2]["headers"]["User-Agent"].startswith("AFL-api/")


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retry_eligible_statuses(status):
    sleeps = []
    c = client([FakeResponse(status), FakeResponse(200)], sleeps=sleeps)
    assert c.get("https://afl.example/retry").status_code == 200
    assert len(c.session.calls) == 2
    assert sleeps == [0.5]


@pytest.mark.parametrize("exc", [requests.ConnectionError("down"), requests.Timeout("slow"), requests.exceptions.ChunkedEncodingError("chunk")])
def test_retry_transient_network_failures(exc):
    c = client([exc, FakeResponse(200)])
    assert c.get("https://afl.example/retry").status_code == 200
    assert len(c.session.calls) == 2


def test_no_unnecessary_retry_for_permanent_4xx():
    c = client([FakeResponse(404), FakeResponse(200)])
    with pytest.raises(http_utils.ScraperHttpError) as exc:
        c.get("https://afl.example/missing")
    assert exc.value.status_code == 404
    assert exc.value.attempts == 1
    assert len(c.session.calls) == 1


def test_maximum_retry_attempt_bounds():
    c = client([FakeResponse(503), FakeResponse(503), FakeResponse(503)])
    with pytest.raises(http_utils.ScraperHttpError) as exc:
        c.get("https://afl.example/unavailable")
    assert exc.value.status_code == 503
    assert exc.value.attempts == 3
    assert len(c.session.calls) == 3


def test_exponential_backoff_and_jitter_bounds():
    sleeps = []
    jitter_calls = []

    def jitter(a, b):
        jitter_calls.append((a, b))
        return b

    policy = http_utils.ScraperHttpPolicy(backoff_base_seconds=1, backoff_max_seconds=3, backoff_jitter_seconds=0.2, rate_limit_seconds=0)
    c = client([FakeResponse(503), FakeResponse(503), FakeResponse(200)], sleeps=sleeps, jitter=jitter, policy=policy)
    c.get("https://afl.example/backoff")
    assert sleeps == [1.2, 2.2]
    assert jitter_calls == [(0.0, 0.2), (0.0, 0.2)]


def test_retry_after_is_bounded():
    sleeps = []
    policy = http_utils.ScraperHttpPolicy(retry_after_max_seconds=10, rate_limit_seconds=0)
    c = client([FakeResponse(429, {"Retry-After": "99"}), FakeResponse(200)], sleeps=sleeps, policy=policy)
    c.get("https://afl.example/rate")
    assert sleeps == [10]


def test_per_host_rate_limiting_and_separate_buckets():
    now = [100.0]
    sleeps = []

    def clock():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = http_utils.PerHostRateLimiter(2.0, clock=clock, sleep=sleep)
    limiter.wait("https://one.example/a")
    limiter.wait("https://two.example/a")
    limiter.wait("https://one.example/b")
    assert sleeps == [2.0]


def test_concurrent_same_host_requests_are_spaced():
    now = [0.0]
    sleeps = []
    lock = threading.Lock()

    def clock():
        with lock:
            return now[0]

    def sleep(seconds):
        with lock:
            sleeps.append(seconds)
            now[0] += seconds

    limiter = http_utils.PerHostRateLimiter(1.0, clock=clock, sleep=sleep)
    threads = [threading.Thread(target=limiter.wait, args=("https://same.example/path",)) for _ in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(sleeps) == [1.0, 1.0]


def test_redacts_sensitive_headers_and_credentials_from_exceptions():
    c = client([requests.ConnectionError("Authorization: secret token=abc")], policy=http_utils.ScraperHttpPolicy(max_attempts=1, rate_limit_seconds=0))
    with pytest.raises(http_utils.ScraperHttpError) as exc:
        c.get("https://user:pass@afl.example/path?token=abc&round=1", headers={"Authorization": "secret", "Cookie": "c"})
    message = str(exc.value)
    assert "user:pass" not in message
    assert "abc" not in message
    assert "secret" not in message
    assert "token=%5BREDACTED%5D" in message
    assert http_utils.redact_headers({"Authorization": "secret", "X-Other": "ok"}) == {"Authorization": "[REDACTED]", "X-Other": "ok"}


def test_limiter_reset_and_default_client_injection():
    fake = client([FakeResponse()])
    assert http_utils.reset_scraper_http_client(fake) is fake
    assert http_utils.get_scraper_http_client() is fake
    fake.rate_limiter._next_allowed["afl.example"] = 123
    fake.rate_limiter.reset()
    assert fake.rate_limiter._next_allowed == {}
    http_utils.reset_scraper_http_client()


def test_scrape_with_backoff_uses_shared_client(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, policy):
            calls.append(policy)
        def get(self, url):
            calls.append(url)
            return FakeResponse(200)
        def close(self):
            calls.append("closed")

    monkeypatch.setattr(http_utils, "ScraperHttpClient", FakeClient)
    response = http_utils.scrape_with_backoff("https://afl.example/plain", max_retries=2, timeout=7)
    assert response.status_code == 200
    assert calls[0].max_attempts == 2
    assert calls[0].timeout == (5.0, 7.0)
    assert calls[1:] == ["https://afl.example/plain", "closed"]
