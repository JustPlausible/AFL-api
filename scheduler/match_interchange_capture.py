"""Diagnostic-only opt-in capture of live CFS matchInterchange evidence (Issue #193).

Polls the CFS ``matchInterchange/{match_provider_id}`` endpoint for live
matches at a configurable interval and retains evidence about
``homeInterchange[]``/``awayInterchange[]`` entries, per-player
``interchangeCount``/``benchReason``/``timeOnGround``/``timeOnBench``, and the
team-level ``homeInterchangeCounts``/``awayInterchangeCounts`` totals, so a
future decision about production interchange semantics can be evaluated
against real evidence. This is evidence capture only: it does not add
interchange data to the consumer API, does not declare this endpoint
production-authoritative, and does not touch player-stat collection, match
scheduling, or ``match_clock`` in any way.

Candidate selection deliberately reuses ``scheduler.match_state_capture``'s
generic, investigation-agnostic ``_live_matches``/``_kickoff_tolerance_matches``
helpers (both are plain reads of the local ``matches`` table, unrelated to
match_clock's own evidence) rather than duplicating that logic. The
post-LIVE grace window is *not* shared with match_clock, and is computed from
this profile's own evidence table instead -- see
``collection.match_interchange_evidence.recently_live_match_provider_ids``
for why (the matchInterchange payload does not appear to carry a live/score
status field the way matchItem does). This keeps ``interchange`` and
``match_clock`` independently selectable and independently scheduled: neither
profile's candidate selection depends on the other profile being enabled.

APScheduler registration, restart-safe re-registration and shutdown are
handled generically by the diagnostics framework via
``diagnostics/profiles/interchange.py``, which adapts the functions in this
module to the framework's ``DiagnosticProfile`` contract -- this module
itself contains no scheduler-registration code.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import config
from afl_json.client import (
    AflJsonAuthenticationError,
    AflJsonClient,
    AflJsonError,
    AflJsonHttpError,
    AflJsonInvalidResponse,
    AflJsonResourceUnavailable,
    AflJsonTransportError,
)
from collection.match_interchange_evidence import (
    MATCH_INTERCHANGE_ENDPOINT, MatchInterchangeEvidenceError, detect_transitions,
    load_previous_observation, parse_match_interchange, persist_observation,
    recently_live_match_provider_ids,
)
from diagnostics.framework import is_profile_selected
from scheduler.match_state_capture import _kickoff_tolerance_matches, _live_matches
from scheduler.write_lane import write_lane
from db.connection import get_db_connection
from utils.log import setup_logger
from logging_sources import LOG_SOURCES

_source = LOG_SOURCES["match_interchange_capture"]
log = setup_logger(_source.logger_name, _source.filename)

# This investigation's stable identifier in the diagnostics framework (see
# diagnostics/framework.py and diagnostics/profiles/interchange.py). Defined
# here, next to the settings it gates, so there is one source of truth.
MATCH_INTERCHANGE_PROFILE_NAME = "interchange"


@dataclass(frozen=True)
class MatchInterchangeCaptureSettings:
    enabled: bool = False
    interval_seconds: int = 15
    kickoff_tolerance_seconds: int = 600
    post_live_grace_seconds: int = 600

    @classmethod
    def from_config(cls) -> "MatchInterchangeCaptureSettings":
        settings = cls(
            enabled=is_profile_selected(MATCH_INTERCHANGE_PROFILE_NAME),
            interval_seconds=config.AFL_DIAGNOSTIC_INTERCHANGE_INTERVAL_SECONDS,
            kickoff_tolerance_seconds=config.AFL_DIAGNOSTIC_INTERCHANGE_KICKOFF_TOLERANCE_SECONDS,
            post_live_grace_seconds=config.AFL_DIAGNOSTIC_INTERCHANGE_POST_LIVE_GRACE_SECONDS,
        )
        if settings.interval_seconds <= 0:
            raise ValueError("AFL_DIAGNOSTIC_INTERCHANGE_INTERVAL_SECONDS must be positive")
        if settings.kickoff_tolerance_seconds < 0:
            raise ValueError("AFL_DIAGNOSTIC_INTERCHANGE_KICKOFF_TOLERANCE_SECONDS must not be negative")
        if settings.post_live_grace_seconds < 0:
            raise ValueError("AFL_DIAGNOSTIC_INTERCHANGE_POST_LIVE_GRACE_SECONDS must not be negative")
        return settings


_client_lock = threading.Lock()
_client: AflJsonClient | None = None


def _get_client() -> AflJsonClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AflJsonClient()
        return _client


def shutdown_match_interchange_capture_client() -> None:
    """Close the pooled diagnostic client; safe to call even if never opened."""
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()


def _capture_candidates(conn, *, now: datetime, settings: MatchInterchangeCaptureSettings) -> list[tuple[int, str]]:
    seen: set[str] = set()
    ordered: list[tuple[int, str]] = []
    for group in (
        _live_matches(conn),
        _kickoff_tolerance_matches(conn, now=now, tolerance_seconds=settings.kickoff_tolerance_seconds),
        recently_live_match_provider_ids(conn, now=now, grace_seconds=settings.post_live_grace_seconds),
    ):
        for match_id, match_provider_id in group:
            if match_provider_id not in seen:
                seen.add(match_provider_id)
                ordered.append((match_id, match_provider_id))
    return ordered


def _current_match_status(conn, match_id: int) -> str | None:
    row = conn.execute("SELECT status FROM matches WHERE match_id=?", (match_id,)).fetchone()
    return row[0] if row else None


def _capture_one(client: AflJsonClient, match_id: int, match_provider_id: str, *,
                 clock: Callable[[], datetime]) -> dict[str, Any]:
    """Poll and persist one match's matchInterchange evidence.

    Distinguishes endpoint availability/error outcomes explicitly (a normal
    "not yet published" 404 is not a scheduler failure) and never raises for
    an individual candidate's failure, so one bad/unavailable match never
    blocks capture for any other match in the same poll cycle.

    ``observed_at`` is taken from ``clock()`` right after this match's own
    response arrives, not from a single timestamp shared by the whole poll
    cycle -- with several matches polled sequentially (and each request
    subject to its own retries/backoff), reusing one cycle-start timestamp
    for every match would misrepresent how far apart responses actually
    landed, undermining transition-cadence analysis, correlation with
    match_clock's evidence, and this profile's own observed_at-driven
    post-LIVE grace window.
    """
    try:
        response = client.request(MATCH_INTERCHANGE_ENDPOINT, path_parameters={"match_provider_id": match_provider_id})
    except AflJsonResourceUnavailable:
        log.info(
            "matchInterchange not yet published match_id=%s match_provider_id=%s",
            match_id, match_provider_id,
        )
        return {"match_id": match_id, "match_provider_id": match_provider_id, "outcome": "not_published"}
    except AflJsonAuthenticationError as exc:
        log.warning(
            "matchInterchange authentication failure match_id=%s match_provider_id=%s error=%s",
            match_id, match_provider_id, exc,
        )
        return {"match_id": match_id, "match_provider_id": match_provider_id, "outcome": "auth_error"}
    except AflJsonTransportError as exc:
        log.warning(
            "matchInterchange transport failure match_id=%s match_provider_id=%s error=%s",
            match_id, match_provider_id, exc,
        )
        return {"match_id": match_id, "match_provider_id": match_provider_id, "outcome": "transport_error"}
    except AflJsonInvalidResponse as exc:
        log.warning(
            "matchInterchange malformed response match_id=%s match_provider_id=%s error=%s",
            match_id, match_provider_id, exc,
        )
        return {"match_id": match_id, "match_provider_id": match_provider_id, "outcome": "invalid_response"}
    except AflJsonHttpError as exc:
        log.warning(
            "matchInterchange HTTP failure match_id=%s match_provider_id=%s error=%s",
            match_id, match_provider_id, exc,
        )
        return {"match_id": match_id, "match_provider_id": match_provider_id, "outcome": "http_error"}

    def op(conn):
        local_status = _current_match_status(conn, match_id)
        try:
            current = parse_match_interchange(
                response.data, match_id=match_id, match_provider_id=match_provider_id,
                observed_at=clock().isoformat(), match_status_at_poll=local_status,
            )
        except MatchInterchangeEvidenceError as exc:
            log.warning(
                "matchInterchange malformed payload match_id=%s match_provider_id=%s error=%s",
                match_id, match_provider_id, exc,
            )
            return {"outcome": "malformed_payload"}
        previous = load_previous_observation(conn, match_provider_id)
        transitions = detect_transitions(previous, current)
        outcome = persist_observation(conn, current, transitions)
        return {"outcome": "success", **outcome}

    result = write_lane.execute("match_interchange_evidence.persist", match_provider_id, op)
    if result["outcome"] == "success" and result["meaningful_transitions"]:
        log.info(
            "match_interchange_evidence transition match_id=%s match_provider_id=%s poll_sequence=%s flags=%s",
            match_id, match_provider_id, result["poll_sequence"], ",".join(result["meaningful_transitions"]),
        )
    return {"match_id": match_id, "match_provider_id": match_provider_id, **result}


def capture_live_match_interchange(*, client: AflJsonClient | None = None,
                                   clock: Callable[[], datetime] | None = None) -> list[dict[str, Any]]:
    """Poll every in-window match's matchInterchange once and persist diagnostic evidence.

    "In-window" is the union of currently-LIVE matches, matches within the
    bounded kickoff tolerance, and matches within this profile's own bounded
    post-LIVE grace (see module docstring). Failures for one match never
    abort collection for the others, so this is safe to leave running
    unattended across a full round of matches.
    """
    settings = MatchInterchangeCaptureSettings.from_config()
    if not settings.enabled:
        return []
    clock_fn = clock or (lambda: datetime.now(timezone.utc))
    # Used only for candidate-window selection (a single consistent reference
    # point for "is this match in-window right now" is correct there); each
    # match's own observed_at is captured separately, per-response, inside
    # _capture_one -- see its docstring.
    now = clock_fn()
    conn = get_db_connection()
    try:
        matches = _capture_candidates(conn, now=now, settings=settings)
    finally:
        conn.close()
    if not matches:
        return []
    active_client = client or _get_client()
    results: list[dict[str, Any]] = []
    for match_id, match_provider_id in matches:
        try:
            results.append(_capture_one(active_client, match_id, match_provider_id, clock=clock_fn))
        except AflJsonError as exc:
            log.warning(
                "matchInterchange evidence capture failed match_id=%s match_provider_id=%s error=%s",
                match_id, match_provider_id, exc,
            )
        except Exception:
            log.exception(
                "Unexpected matchInterchange evidence capture failure match_id=%s match_provider_id=%s",
                match_id, match_provider_id,
            )
    return results
