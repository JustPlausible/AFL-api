"""Consumer /api/v1 request telemetry middleware (Issue #205).

The single instrumentation point for every ``/api/v1`` request: added once
in ``main.py`` via ``app.middleware("http")``, not per-route. Routes never
need to call :func:`analytics.record.record_consumer_request` themselves.

Deliberately privacy-minimal (see ``docs/analytics_framework.md`` "Privacy
rules" and Issue #205's non-goals): records only the stable route template,
status code, duration, the existing internal ``api_keys.id`` (never the key
secret or header), and a small bounded "mode" descriptor built from a fixed
allow-list of known boolean/enum query flags -- never arbitrary query
content, headers, or the request/response body.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from starlette.requests import Request

from analytics.record import record_consumer_request

# Fixed allow-list of bounded "query-mode" flags already used by /api/v1
# routes (api/routes_v1.py): advanced-read gating, and a handful of route-
# specific boolean/enum filters. Extending this list is how a future route's
# mode flag becomes visible in analytics -- never read arbitrary query keys.
# Each entry normalizes the raw query string to one of a small fixed set of
# accepted values (mirroring FastAPI/Pydantic's own bool query-param
# coercion for the boolean flags, and the route's exact Literal[...] values
# for event_type) rather than copying whatever the caller sent -- an
# unrecognized value (e.g. "?advanced=" or an invalid event_type, which
# FastAPI itself would reject with 422) is simply omitted, never persisted
# verbatim.
_TRUE_VALUES = {"1", "true", "on", "yes"}
_FALSE_VALUES = {"0", "false", "off", "no"}


def _normalize_bool(raw: str) -> str | None:
    lowered = raw.strip().lower()
    if lowered in _TRUE_VALUES:
        return "true"
    if lowered in _FALSE_VALUES:
        return "false"
    return None


def _normalize_event_type(raw: str) -> str | None:
    accepted = {"appeared", "disappeared", "interchange_count_changed", "bench_reason_changed"}
    return raw if raw in accepted else None


REQUEST_MODE_NORMALIZERS = {
    "advanced": _normalize_bool,
    "score_events_only": _normalize_bool,
    "on_bench_only": _normalize_bool,
    "event_type": _normalize_event_type,
}
_MAX_REQUEST_MODE_LENGTH = 100


def _request_mode(request: Request) -> str | None:
    parts = []
    for name, normalize in REQUEST_MODE_NORMALIZERS.items():
        raw = request.query_params.get(name)
        if raw is None:
            continue
        normalized = normalize(raw)
        if normalized is not None:
            parts.append(f"{name}={normalized}")
    if not parts:
        return None
    return ",".join(parts)[:_MAX_REQUEST_MODE_LENGTH]


def _route_identifier(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "unmatched"


async def analytics_http_middleware(request: Request, call_next):
    """Record one telemetry observation per /api/v1 request; never affects the response.

    Registered as the single shared instrumentation point for the whole
    consumer API surface (Issue #205's "prefer central middleware... rather
    than manually modifying every route"). Only paths under ``/api/v1`` are
    recorded -- Admin, health, and the legacy unversioned ``/api`` routes are
    out of scope for this framework.
    """
    if not request.url.path.startswith("/api/v1"):
        return await call_next(request)
    started = time.monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        try:
            duration_ms = (time.monotonic() - started) * 1000
            record_consumer_request(
                route=_route_identifier(request), observed_at=datetime.now(timezone.utc),
                duration_ms=duration_ms, status_code=status_code,
                api_key_id=getattr(request.state, "api_key_id", None),
                request_mode=_request_mode(request),
            )
        except Exception:
            pass
