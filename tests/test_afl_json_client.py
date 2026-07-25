from __future__ import annotations

from dataclasses import dataclass, field

import pytest
import requests

from afl_json.client import (
    AflJsonAuthenticationError,
    AflJsonClient,
    AflJsonResourceUnavailable,
    AflJsonTransportError,
    HttpPolicy,
)
from afl_json.contracts import CFS_TOKEN_HEADER


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: object = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    def json(self):
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
