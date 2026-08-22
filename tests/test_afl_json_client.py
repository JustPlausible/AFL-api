from __future__ import annotations

from dataclasses import dataclass, field

import pytest
import requests

from afl_json.client import (
    AflJsonAuthenticationError,
    AflJsonClient,
    AflJsonInvalidResponse,
    AflJsonResourceUnavailable,
    AflJsonTransportError,
    HttpPolicy,
)
from afl_json.contracts import CFS_API_BASE, CFS_TOKEN_HEADER


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: object = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    content: bytes = b""
    encoding: str | None = "utf-8"
    history: list = field(default_factory=list)
    json_error: Exception | None = None

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, *outcomes):
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


def client(*outcomes, max_attempts=1):
    return AflJsonClient(
        session=FakeSession(*outcomes),
        policy=HttpPolicy(max_attempts=max_attempts, backoff_seconds=0),
        sleep=lambda _delay: None,
    )


def test_public_request_does_not_acquire_token():
    subject = client(FakeResponse(payload={"competitions": []}))

    result = subject.get("competitions")

    assert result.data == {"competitions": []}
    assert len(subject.session.calls) == 1
    assert CFS_TOKEN_HEADER not in subject.session.calls[0][2]["headers"]


def test_protected_requests_lazily_acquire_and_reuse_token():
    subject = client(
        FakeResponse(payload={"token": "secret-token"}),
        FakeResponse(payload={"players": []}),
        FakeResponse(payload={"players": [{"playerId": "CD_I1"}]}),
    )

    subject.get("season_players", params={"seasonId": "CD_S1"})
    subject.get("season_players", params={"seasonId": "CD_S1"})

    assert [call[1].endswith("/WMCTok") for call in subject.session.calls] == [True, False, False]
    assert subject.session.calls[1][2]["headers"][CFS_TOKEN_HEADER] == "secret-token"
    assert subject.session.calls[2][2]["headers"][CFS_TOKEN_HEADER] == "secret-token"


def test_401_refreshes_token_and_retries_request_once():
    subject = client(
        FakeResponse(payload={"token": "old-token"}),
        FakeResponse(status_code=401, payload={"code": "CFSAPI001"}),
        FakeResponse(payload={"token": "new-token"}),
        FakeResponse(payload={"players": []}),
    )

    result = subject.get("season_players", params={"seasonId": "CD_S1"})

    assert result.status_code == 200
    assert len(subject.session.calls) == 4
    assert subject.session.calls[-1][2]["headers"][CFS_TOKEN_HEADER] == "new-token"


def test_second_401_is_bounded_and_reported_as_authentication_error():
    subject = client(
        FakeResponse(payload={"token": "old"}), FakeResponse(status_code=401, payload={}),
        FakeResponse(payload={"token": "new"}), FakeResponse(status_code=401, payload={}),
    )

    with pytest.raises(AflJsonAuthenticationError) as caught:
        subject.get("season_players", params={"seasonId": "CD_S1"})

    assert caught.value.attempts == 2
    assert len(subject.session.calls) == 4


def test_not_published_error_has_distinct_exception():
    subject = client(
        FakeResponse(payload={"token": "token"}),
        FakeResponse(status_code=404, payload={"error": {"code": "CFSSDS001"}}),
    )

    with pytest.raises(AflJsonResourceUnavailable) as caught:
        subject.get("season_players", params={"seasonId": "CD_S1"})

    assert caught.value.error_code == "CFSSDS001"


def test_transient_failures_are_retried_only_to_configured_limit():
    subject = client(requests.ConnectionError("token=must-not-leak"), requests.Timeout("slow"), max_attempts=2)

    with pytest.raises(AflJsonTransportError) as caught:
        subject.get("competitions")

    assert caught.value.attempts == 2
    assert "must-not-leak" not in str(caught.value)
    assert len(subject.session.calls) == 2


def test_required_parameters_are_validated_before_network_access():
    subject = client()

    with pytest.raises(ValueError, match="Missing path parameters"):
        subject.get("competition_seasons")
    with pytest.raises(ValueError, match="Missing query parameters"):
        subject.get("season_players")

    assert subject.session.calls == []


# --- base_url_override + invalid-JSON diagnostics ---------------------------
# Regression coverage for the live CD_M20260142403 commentary failure: a
# correct parser wired to a URL resolved from the wrong CFS base path
# returned a non-JSON (HTML) error response on every single poll. See
# collection/match_commentary_evidence.py's MATCH_COMMENTARY_ENDPOINT and
# afl_json/contracts.py's EndpointDefinition.base_url_override.

def test_endpoint_with_base_url_override_is_requested_at_the_overridden_root():
    from afl_json.contracts import EndpointDefinition, HttpMethod, SourceSystem

    endpoint = EndpointDefinition(
        name="diagnostic_example", source=SourceSystem.CFS, method=HttpMethod.GET,
        path_template="/exampleFeed/{match_provider_id}", requires_auth=True,
        entity_type="diagnostic_example", collection_paths=(), identifier_type=None,
        required_path_parameters=("match_provider_id",), verified=False,
        base_url_override="https://api.afl.com.au/cfs",
    )
    assert endpoint.base_url == "https://api.afl.com.au/cfs"
    assert endpoint.base_url != CFS_API_BASE  # the standard CFS root every other endpoint uses
    assert endpoint.url_template == "https://api.afl.com.au/cfs/exampleFeed/{match_provider_id}"

    subject = client(
        FakeResponse(payload={"token": "token"}),
        FakeResponse(payload={"ok": True}),
    )
    subject.get(endpoint, path_parameters={"match_provider_id": "CD_M1"})

    requested_url = subject.session.calls[-1][1]
    assert requested_url == "https://api.afl.com.au/cfs/exampleFeed/CD_M1"


def test_endpoint_without_override_still_uses_the_standard_cfs_root():
    """The override is opt-in and additive: an endpoint that doesn't set it
    (i.e. every existing maintained endpoint) is completely unaffected."""
    from afl_json.contracts import get_endpoint

    definition = get_endpoint("season_players")
    assert definition.base_url_override is None
    assert definition.base_url == CFS_API_BASE


def test_invalid_json_response_carries_safe_diagnostics_for_an_html_error_page():
    """Reproduces the live failure shape: a wrong URL returns a 404 HTML
    error page, and response.json() raises. The resulting
    AflJsonInvalidResponse must carry enough safe *structural* metadata to
    diagnose this without a live repro -- status, content-type, body shape
    -- and must never carry request headers (the CFS token) or any response
    body content at all, per docs/architecture/workflows/
    scheduler_workflow_design.md's "never store tokens, response bodies, or
    unsafe exception details merely for scheduler diagnosis"."""
    html_body = "<html><head><title>404 Not Found</title></head><body>Not Found</body></html>"
    subject = client(
        FakeResponse(payload={"token": "token"}),
        FakeResponse(
            status_code=404, text=html_body, content=html_body.encode(),
            headers={"Content-Type": "text/html; charset=utf-8"}, encoding="utf-8",
            json_error=ValueError("Expecting value: line 1 column 1 (char 0)"),
        ),
    )

    with pytest.raises(AflJsonInvalidResponse) as caught:
        subject.get("season_players", params={"seasonId": "CD_S1"})

    diagnostics = caught.value.response_diagnostics
    assert diagnostics is not None
    assert diagnostics["content_type"] == "text/html; charset=utf-8"
    assert diagnostics["body_shape"] == "html-looking"
    assert diagnostics["redirect_count"] == 0
    assert diagnostics["content_length_actual"] == len(html_body.encode())
    # Whitelisted keys only -- structural metadata alone, never any body
    # content, and no request headers (which carry the CFS auth token
    # "token" set up by client()).
    assert set(diagnostics) == {
        "content_type", "content_encoding", "content_length_header", "content_length_actual",
        "declared_encoding", "redirect_count", "body_shape",
    }
    assert "token" not in diagnostics.values()
    assert not any("404 Not Found" in str(value) for value in diagnostics.values())


def test_invalid_json_response_classifies_large_body_without_storing_any_content():
    huge_body = "{" + ("a" * 5000)
    subject = client(
        FakeResponse(
            status_code=200, text=huge_body, content=huge_body.encode(),
            headers={"Content-Type": "application/json"}, encoding="utf-8",
            json_error=ValueError("Expecting property name enclosed in double quotes"),
        ),
    )
    with pytest.raises(AflJsonInvalidResponse) as caught:
        subject.get("competitions")

    diagnostics = caught.value.response_diagnostics
    assert diagnostics["body_shape"] == "json-looking"
    assert not any("aaaa" in str(value) for value in diagnostics.values())


def test_invalid_json_response_never_leaks_body_content_even_with_injected_control_characters():
    """Security regression: a malicious/garbled upstream body containing
    embedded newlines (log-injection payload) or secrets-shaped text must
    never reach response_diagnostics -- not bounded, not sanitised, not at
    all -- since diagnostics is logged verbatim by capture code."""
    hostile_body = "not json\r\nfake_log_line=ADMIN_TOKEN_LEAKED forged_status=200\nsecret=abc123"
    subject = client(
        FakeResponse(
            status_code=200, text=hostile_body, content=hostile_body.encode(),
            headers={"Content-Type": "text/plain"}, encoding="utf-8",
            json_error=ValueError("Expecting value: line 1 column 1 (char 0)"),
        ),
    )
    with pytest.raises(AflJsonInvalidResponse) as caught:
        subject.get("competitions")

    diagnostics = caught.value.response_diagnostics
    serialised = str(diagnostics)
    assert "\r" not in serialised and "\n" not in serialised
    assert "ADMIN_TOKEN_LEAKED" not in serialised
    assert "secret=abc123" not in serialised
    assert diagnostics["body_shape"] == "unknown"


def test_invalid_json_response_empty_body_is_classified_empty():
    subject = client(
        FakeResponse(status_code=200, text="", content=b"", headers={}, encoding=None,
                     json_error=ValueError("Expecting value: line 1 column 1 (char 0)")),
    )
    with pytest.raises(AflJsonInvalidResponse) as caught:
        subject.get("competitions")

    assert caught.value.response_diagnostics["body_shape"] == "empty"
