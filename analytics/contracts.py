"""The common analytics observation contract (Issue #205).

Two small, frozen dataclasses are the entire "contract" between a collector
or API route and the analytics framework:

* :class:`UpstreamPollObservation` -- one logical poll of an upstream
  AFL/CFS resource by the match-based scheduler.
* :class:`ConsumerRequestObservation` -- one ``/api/v1`` request.

A collector builds one of these from facts it already has (or cheaply
computes) and passes it to :mod:`analytics.record`. Nothing about the
contract is scheduler-specific or API-specific -- a future module (e.g. a
non-match-based collector) only needs to build the same dataclass and call
the same recorder; it does not need new scheduler infrastructure.

Resource/route registration
----------------------------

:data:`RESOURCE_REGISTRY` and :data:`ROUTE_REGISTRY` are small, informational
metadata registries -- a stable identifier plus a human-readable label and,
for upstream resources, a short description of what ``change_magnitude``
counts for that resource (Issue #205 explicitly allows different resources
to use different, resource-specific change semantics). They exist purely to
make reports readable and to document "what does a poll of this resource
mean" in one place; they are deliberately *not* a gate. An observation for
an unregistered identifier is still recorded -- forgetting to register a new
resource must never silently drop real observations. Register a resource by
importing this module and calling :func:`register_resource` (or
:func:`register_route`) once, near where the corresponding collector/route
lives; see ``docs/analytics_framework.md`` "Adding a new analytics module".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class UpstreamOutcome(str, Enum):
    """Outcome category for one upstream poll attempt.

    Mirrors the outcome vocabulary the production commentary/interchange
    collectors already classify responses into (see
    ``afl_json/match_commentary.py`` and ``afl_json/match_interchange.py``)
    rather than inventing a parallel taxonomy. ``changed``/``unchanged`` are
    not separate outcomes here -- both are ``SUCCESS``, distinguished by the
    observation's ``changed`` field, so "successful poll rate" and "changed
    vs unchanged" can each be computed independently from the same rows.
    """

    SUCCESS = "success"
    NOT_PUBLISHED = "not_published"
    UNAVAILABLE = "unavailable"
    AUTH_ERROR = "auth_error"
    TRANSPORT_ERROR = "transport_error"
    HTTP_ERROR = "http_error"
    INVALID_RESPONSE = "invalid_response"
    MALFORMED_PAYLOAD = "malformed_payload"
    ERROR = "error"


UPSTREAM_FAILURE_OUTCOMES = frozenset(UpstreamOutcome) - {UpstreamOutcome.SUCCESS}


@dataclass(frozen=True, slots=True)
class UpstreamPollObservation:
    """One factual observation of one logical upstream poll.

    Every field is either already available to a collector or cheap to
    compute (see ``docs/analytics_framework.md`` "Instrumentation points"
    for exactly where each production collector sources these). Nothing
    here is a full response payload -- ``change_magnitude`` and ``note`` are
    small, bounded facts a resource-specific module chooses to attach, never
    raw content.

    Attributes:
        resource: Stable resource identifier, e.g. ``"cfs_player_stats"``.
        match_id: Internal ``matches.match_id``, when applicable.
        match_provider_id: Champion Data match provider ID, when applicable.
        observed_at: UTC timestamp of this poll attempt.
        lifecycle_state: ``matches.status`` at poll time (SCHEDULED/LIVE/
            POSTGAME/CONCLUDED), when cheaply available.
        configured_interval_seconds: The polling interval this poll was
            scheduled under (a collector's configured/derived cadence).
        actual_interval_seconds: Wall-clock time since the previous
            observation of the same resource/match, computed in-process by
            :mod:`analytics.record` (best-effort, resets on restart) --
            never supplied by the caller directly.
        duration_ms: Wall-clock duration of the request (and, where the
            collector cannot cheaply separate the two, its persistence).
        outcome: Coarse result category (see :class:`UpstreamOutcome`).
        http_status: HTTP status code, where meaningful and available.
        changed: Whether this successful poll differed meaningfully from
            the previous successful poll of the same resource/match. Only
            meaningful when ``outcome`` is ``SUCCESS``; ``None`` otherwise.
        change_magnitude: A small, bounded, resource-defined count of what
            changed (e.g. number of players whose stats changed, number of
            newly observed commentary events, number of interchange
            transitions). ``None`` when not computed/applicable.
        note: A short, bounded, non-payload note (e.g. "malformed_payload").
    """

    resource: str
    observed_at: datetime
    duration_ms: float
    outcome: UpstreamOutcome
    match_id: int | None = None
    match_provider_id: str | None = None
    lifecycle_state: str | None = None
    configured_interval_seconds: float | None = None
    actual_interval_seconds: float | None = None
    http_status: int | None = None
    changed: bool | None = None
    change_magnitude: int | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ConsumerRequestObservation:
    """One factual observation of one ``/api/v1`` request.

    Deliberately privacy-minimal (Issue #205): no request path beyond the
    stable route template, no headers, no request/response bodies, no raw
    client identity. ``api_key_id`` is the existing internal
    ``api_keys.id`` primary key already resolved by ``auth.authenticate_api_key``
    -- an internal integer, never the key secret itself.

    Attributes:
        route: Stable route identifier (the FastAPI/Starlette route
            template, e.g. ``"/api/v1/matches/{match_id}/player-stats"}``).
        observed_at: UTC timestamp of the request.
        duration_ms: Wall-clock request duration.
        status_code: HTTP response status code.
        api_key_id: Internal ``api_keys.id`` for the authenticated caller,
            when the route is authenticated. ``None`` for unauthenticated
            requests.
        request_mode: A short, bounded descriptor of an optional query-mode
            flag actually used (e.g. ``"advanced=true"``), drawn from a
            fixed allow-list -- never arbitrary query-string content.
    """

    route: str
    observed_at: datetime
    duration_ms: float
    status_code: int
    api_key_id: int | None = None
    request_mode: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceInfo:
    resource: str
    display_name: str
    change_magnitude_label: str | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class RouteInfo:
    route: str
    display_name: str
    description: str = ""


RESOURCE_REGISTRY: dict[str, ResourceInfo] = {}
ROUTE_REGISTRY: dict[str, RouteInfo] = {}


def register_resource(resource: str, *, display_name: str,
                      change_magnitude_label: str | None = None,
                      description: str = "") -> None:
    """Register upstream-resource metadata for reporting readability.

    Safe to call more than once with identical arguments (module re-import
    under a test runner); raises only if a *different* definition is
    registered under the same identifier, to catch a copy-paste identifier
    collision early.
    """
    info = ResourceInfo(resource=resource, display_name=display_name,
                        change_magnitude_label=change_magnitude_label, description=description)
    existing = RESOURCE_REGISTRY.get(resource)
    if existing is not None and existing != info:
        raise ValueError(f"analytics resource {resource!r} already registered with different metadata")
    RESOURCE_REGISTRY[resource] = info


def register_route(route: str, *, display_name: str, description: str = "") -> None:
    """Register consumer-route metadata for reporting readability. See
    :func:`register_resource` for the re-registration/collision rule."""
    info = RouteInfo(route=route, display_name=display_name, description=description)
    existing = ROUTE_REGISTRY.get(route)
    if existing is not None and existing != info:
        raise ValueError(f"analytics route {route!r} already registered with different metadata")
    ROUTE_REGISTRY[route] = info


# ---------------------------------------------------------------------------
# Stage 2 registrations: the principal production match-scheduler collectors.
# ---------------------------------------------------------------------------

register_resource(
    "cfs_player_stats", display_name="CFS player statistics",
    change_magnitude_label="players with an accepted (changed) stat line",
    description="Authoritative per-match CFS player statistics (afl_json/player_stats.py).",
)
register_resource(
    "match_commentary", display_name="CFS match commentary",
    change_magnitude_label="newly observed commentary events",
    description="Production commentaryFeed collection (afl_json/match_commentary.py, Issue #201).",
)
register_resource(
    "match_interchange", display_name="CFS match interchange",
    change_magnitude_label="interchange appear/disappear/field transitions",
    description="Production matchInterchange collection (afl_json/match_interchange.py, Issue #204).",
)

# ---------------------------------------------------------------------------
# Consumer /api/v1 route registrations, for reporting readability only. The
# telemetry middleware (analytics/middleware.py) records whatever route
# template FastAPI resolves, whether or not it is registered here.
# ---------------------------------------------------------------------------

register_route("/api/v1", display_name="API root/discovery")
register_route("/api/v1/seasons", display_name="Seasons")
register_route("/api/v1/seasons/{season_id}/rounds", display_name="Season rounds")
register_route("/api/v1/rounds/{round_id}", display_name="Round detail")
register_route("/api/v1/rounds/{round_id}/matches", display_name="Round matches")
register_route("/api/v1/matches/{match_id}", display_name="Match detail")
register_route("/api/v1/matches/{match_id}/player-stats", display_name="Match player stats")
register_route("/api/v1/matches/{match_id}/commentary", display_name="Match commentary")
register_route("/api/v1/matches/{match_id}/interchanges", display_name="Match interchange state")
register_route("/api/v1/matches/{match_id}/interchanges/events", display_name="Match interchange events")
register_route("/api/v1/players", display_name="Player search")
register_route("/api/v1/players/{canonical_player_id}", display_name="Player detail")
register_route("/api/v1/players/{canonical_player_id}/seasons", display_name="Player season history")
register_route("/api/v1/seasons/{season_id}/players", display_name="Season player membership")
register_route("/api/v1/injuries", display_name="Current injuries")
