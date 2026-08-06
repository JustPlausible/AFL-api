"""Shared HTTP transport for AFL's public JSON API and protected CFS API."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import requests

from .contracts import (
    CFS_ERROR_AUTH,
    CFS_ERROR_NOT_PUBLISHED,
    CFS_TOKEN_HEADER,
    RETRYABLE_HTTP_STATUSES,
    EndpointDefinition,
    get_endpoint,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HttpPolicy:
    """Timeout and bounded retry settings for AFL JSON requests."""

    connect_timeout: float = 5.0
    read_timeout: float = 20.0
    max_attempts: int = 3
    backoff_seconds: float = 0.5

    def __post_init__(self) -> None:
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ValueError("HTTP timeouts must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)


@dataclass(frozen=True, slots=True)
class AflJsonResponse:
    endpoint: str
    status_code: int
    data: Any
    headers: Mapping[str, str]


class AflJsonError(RuntimeError):
    """Base error containing safe, structured request context."""

    def __init__(self, message: str, *, endpoint: str, status_code: int | None = None,
                 error_code: str | None = None, attempts: int = 1):
        super().__init__(message)
        self.endpoint = endpoint
        self.status_code = status_code
        self.error_code = error_code
        self.attempts = attempts


class AflJsonTransportError(AflJsonError):
    pass


class AflJsonHttpError(AflJsonError):
    pass


class AflJsonAuthenticationError(AflJsonHttpError):
    pass


class AflJsonResourceUnavailable(AflJsonHttpError):
    """The requested CFS resource has not been published."""


class AflJsonInvalidResponse(AflJsonError):
    pass


class WMCTokenProvider:
    """Lazily acquire and retain the process-local CFS token."""

    def __init__(self, acquire: Callable[[], str]):
        self._acquire = acquire
        self._token: str | None = None

    def get_token(self) -> str:
        if self._token is None:
            token = self._acquire()
            if not isinstance(token, str) or not token.strip():
                raise AflJsonInvalidResponse(
                    "WMCTok response did not contain a token", endpoint="wmc_token"
                )
            self._token = token.strip()
        return self._token

    def invalidate(self) -> None:
        self._token = None


class AflJsonClient:
    """Request endpoint contracts and apply CFS authentication policy."""

    def __init__(self, *, session: requests.Session | None = None,
                 policy: HttpPolicy | None = None,
                 token_provider: WMCTokenProvider | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.session = session or requests.Session()
        self.policy = policy or HttpPolicy()
        self.sleep = sleep
        self.token_provider = token_provider or WMCTokenProvider(self.acquire_wmc_token)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "AflJsonClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def request(self, endpoint: str | EndpointDefinition, *,
                path_parameters: Mapping[str, object] | None = None,
                params: Mapping[str, object] | None = None,
                json: Any = None) -> AflJsonResponse:
        definition = get_endpoint(endpoint) if isinstance(endpoint, str) else endpoint
        url = self._build_url(definition, path_parameters or {})
        self._validate_query(definition, params or {})
        headers = {"Accept": "application/json"}
        if definition.requires_auth:
            headers[CFS_TOKEN_HEADER] = self.token_provider.get_token()

        response, attempts = self._send_with_retries(
            definition.method.value, url, endpoint=definition.name, headers=headers,
            params=params, json=json,
        )
        payload = self._decode_json(response, definition.name, attempts)
        error_code = _error_code(payload)

        # Authentication gets exactly one token refresh, independently of the
        # bounded retry policy for transient transport/server failures.
        if definition.requires_auth and (response.status_code == 401 or error_code == CFS_ERROR_AUTH):
            self.token_provider.invalidate()
            headers[CFS_TOKEN_HEADER] = self.token_provider.get_token()
            response, retry_attempts = self._send_with_retries(
                definition.method.value, url, endpoint=definition.name, headers=headers,
                params=params, json=json,
            )
            attempts += retry_attempts
            payload = self._decode_json(response, definition.name, attempts)
            error_code = _error_code(payload)

        if response.status_code == 404 and error_code == CFS_ERROR_NOT_PUBLISHED:
            raise AflJsonResourceUnavailable(
                "AFL resource is unavailable or not published", endpoint=definition.name,
                status_code=404, error_code=error_code, attempts=attempts,
            )
        if response.status_code == 401 or error_code == CFS_ERROR_AUTH:
            raise AflJsonAuthenticationError(
                "AFL CFS authentication failed after token refresh", endpoint=definition.name,
                status_code=response.status_code, error_code=error_code, attempts=attempts,
            )
        if not 200 <= response.status_code < 300:
            raise AflJsonHttpError(
                f"AFL JSON request failed with HTTP {response.status_code}",
                endpoint=definition.name, status_code=response.status_code,
                error_code=error_code, attempts=attempts,
            )
        return AflJsonResponse(definition.name, response.status_code, payload, dict(response.headers))

    def get(self, endpoint: str | EndpointDefinition, **kwargs: Any) -> AflJsonResponse:
        return self.request(endpoint, **kwargs)

    def acquire_wmc_token(self) -> str:
        definition = get_endpoint("wmc_token")
        response, attempts = self._send_with_retries(
            definition.method.value, definition.url_template, endpoint=definition.name,
            headers={"Accept": "application/json"},
        )
        payload = self._decode_json(response, definition.name, attempts)
        if not 200 <= response.status_code < 300:
            raise AflJsonAuthenticationError(
                f"WMCTok acquisition failed with HTTP {response.status_code}",
                endpoint=definition.name, status_code=response.status_code,
                error_code=_error_code(payload), attempts=attempts,
            )
        token = payload.get("token") if isinstance(payload, dict) else payload
        if not isinstance(token, str) or not token.strip():
            raise AflJsonInvalidResponse(
                "WMCTok response did not contain a token", endpoint=definition.name,
                status_code=response.status_code, attempts=attempts,
            )
        return token

    def _send_with_retries(self, method: str, url: str, *, endpoint: str, **kwargs: Any):
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                response = self.session.request(method, url, timeout=self.policy.timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == self.policy.max_attempts:
                    raise AflJsonTransportError(
                        f"AFL JSON transport failed: {type(exc).__name__}",
                        endpoint=endpoint, attempts=attempt,
                    ) from exc
                self._backoff(attempt)
                continue
            if response.status_code not in RETRYABLE_HTTP_STATUSES or attempt == self.policy.max_attempts:
                logger.debug("AFL JSON request endpoint=%s status=%s attempt=%s", endpoint, response.status_code, attempt)
                return response, attempt
            logger.warning("Retrying AFL JSON request endpoint=%s status=%s attempt=%s", endpoint, response.status_code, attempt)
            self._backoff(attempt)
        raise AssertionError("bounded request loop exhausted unexpectedly")

    def _backoff(self, attempt: int) -> None:
        self.sleep(self.policy.backoff_seconds * (2 ** (attempt - 1)))

    @staticmethod
    def _decode_json(response: requests.Response, endpoint: str, attempts: int) -> Any:
        try:
            return response.json()
        except (requests.JSONDecodeError, ValueError) as exc:
            raise AflJsonInvalidResponse(
                "AFL endpoint returned invalid JSON", endpoint=endpoint,
                status_code=response.status_code, attempts=attempts,
            ) from exc

    @staticmethod
    def _build_url(definition: EndpointDefinition, values: Mapping[str, object]) -> str:
        missing = set(definition.required_path_parameters) - set(values)
        if missing:
            raise ValueError(f"Missing path parameters for {definition.name}: {', '.join(sorted(missing))}")
        unexpected = set(values) - set(definition.required_path_parameters)
        if unexpected:
            raise ValueError(f"Unexpected path parameters for {definition.name}: {', '.join(sorted(unexpected))}")
        return definition.url_template.format(**values)

    @staticmethod
    def _validate_query(definition: EndpointDefinition, params: Mapping[str, object]) -> None:
        missing = set(definition.required_query_parameters) - set(params)
        if missing:
            raise ValueError(f"Missing query parameters for {definition.name}: {', '.join(sorted(missing))}")


def _error_code(payload: Any) -> str | None:
    """Extract known provider error shapes without ever logging their bodies."""
    if not isinstance(payload, dict):
        return None
    for container in (payload, payload.get("error")):
        if isinstance(container, dict):
            value = container.get("code") or container.get("errorCode")
            if isinstance(value, str):
                return value
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        value = errors[0].get("code") or errors[0].get("errorCode")
        return value if isinstance(value, str) else None
    return None
