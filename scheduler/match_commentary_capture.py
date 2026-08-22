"""Diagnostic-only opt-in capture of live CFS commentaryFeed evidence (Issue #196).

Polls the CFS ``commentaryFeed/{match_provider_id}`` endpoint for live
matches at a configurable interval and retains deduplicated evidence about
the accumulated commentary feed -- quarter markers, score events,
player/team-linked commentary, and detected possible edits -- so a future
decision about production commentary/score-event semantics can be evaluated
against real evidence. This is evidence capture only: it does not add
commentary data to the consumer API, does not declare this endpoint
production-authoritative, and does not touch match scheduling, finality, or
either of the other two checked-in diagnostic profiles (``match_clock``,
``interchange``) in any way.

Candidate selection deliberately reuses ``scheduler.match_state_capture``'s
generic, investigation-agnostic ``_live_matches``/``_kickoff_tolerance_matches``
helpers (both are plain reads of the local ``matches`` table, unrelated to
match_clock's own evidence) rather than duplicating that logic. The
post-LIVE grace window is computed from this profile's own evidence table,
mirroring ``interchange`` (the commentaryFeed payload does not appear to
carry a live/score status field the way matchItem does). This keeps
``commentary``, ``match_clock`` and ``interchange`` independently selectable
and independently scheduled.

APScheduler registration, restart-safe re-registration and shutdown are
handled generically by the diagnostics framework via
``diagnostics/profiles/commentary.py``, which adapts the functions in this
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
from collection.match_commentary_evidence import (
    MATCH_COMMENTARY_ENDPOINT, MatchCommentaryEvidenceError, parse_match_commentary,
    persist_observation, persist_poll_outcome, recently_live_match_provider_ids,
)
from diagnostics.framework import is_profile_selected
from scheduler.match_state_capture import _kickoff_tolerance_matches, _live_matches
from scheduler.write_lane import write_lane
from db.connection import get_db_connection
from utils.log import setup_logger
from logging_sources import LOG_SOURCES

_source = LOG_SOURCES["match_commentary_capture"]
log = setup_logger(_source.logger_name, _source.filename)

# This investigation's stable identifier in the diagnostics framework (see
# diagnostics/framework.py and diagnostics/profiles/commentary.py). Defined
# here, next to the settings it gates, so there is one source of truth.
MATCH_COMMENTARY_PROFILE_NAME = "commentary"


@dataclass(frozen=True)
class MatchCommentaryCaptureSettings:
    enabled: bool = False
    interval_seconds: int = 15
    kickoff_tolerance_seconds: int = 600
    post_live_grace_seconds: int = 600

    @classmethod
    def from_config(cls) -> "MatchCommentaryCaptureSettings":
        settings = cls(
            enabled=is_profile_selected(MATCH_COMMENTARY_PROFILE_NAME),
            interval_seconds=config.AFL_DIAGNOSTIC_COMMENTARY_INTERVAL_SECONDS,
            kickoff_tolerance_seconds=config.AFL_DIAGNOSTIC_COMMENTARY_KICKOFF_TOLERANCE_SECONDS,
            post_live_grace_seconds=config.AFL_DIAGNOSTIC_COMMENTARY_POST_LIVE_GRACE_SECONDS,
        )
        if settings.interval_seconds <= 0:
            raise ValueError("AFL_DIAGNOSTIC_COMMENTARY_INTERVAL_SECONDS must be positive")
        if settings.kickoff_tolerance_seconds < 0:
            raise ValueError("AFL_DIAGNOSTIC_COMMENTARY_KICKOFF_TOLERANCE_SECONDS must not be negative")
        if settings.post_live_grace_seconds < 0:
            raise ValueError("AFL_DIAGNOSTIC_COMMENTARY_POST_LIVE_GRACE_SECONDS must not be negative")
        return settings


_client_lock = threading.Lock()
_client: AflJsonClient | None = None


def _get_client() -> AflJsonClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AflJsonClient()
        return _client


def shutdown_match_commentary_capture_client() -> None:
    """Close the pooled diagnostic client; safe to call even if never opened."""
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()


def _capture_candidates(conn, *, now: datetime, settings: MatchCommentaryCaptureSettings) -> list[tuple[int, str]]:
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
    """Poll and persist one match's commentaryFeed evidence.

    Distinguishes endpoint availability/error outcomes explicitly (a normal
    "not yet published" 404 is not a scheduler failure) and never raises for
    an individual candidate's failure, so one bad/unavailable match never
    blocks capture for any other match in the same poll cycle. Unlike
    ``interchange``, every outcome -- success or not -- is persisted (see
    ``collection.match_commentary_evidence.persist_poll_outcome``), so the
    report can show endpoint availability/failure transitions.

    ``observed_at`` is taken from ``clock()`` right after this match's own
    response arrives (or, for a failed request, right before persisting),
    not from a single timestamp shared by the whole poll cycle -- see
    ``scheduler.match_interchange_capture._capture_one`` for why.
    """
    outcome: str | None
    try:
        response = client.request(MATCH_COMMENTARY_ENDPOINT, path_parameters={"match_provider_id": match_provider_id})
        outcome = None
    except AflJsonResourceUnavailable:
        log.info(
            "commentaryFeed not yet published match_id=%s match_provider_id=%s",
            match_id, match_provider_id,
        )
        response, outcome = None, "not_published"
    except AflJsonAuthenticationError as exc:
        log.warning(
            "commentaryFeed authentication failure match_id=%s match_provider_id=%s error=%s",
            match_id, match_provider_id, exc,
        )
        response, outcome = None, "auth_error"
    except AflJsonTransportError as exc:
        log.warning(
            "commentaryFeed transport failure match_id=%s match_provider_id=%s error=%s",
            match_id, match_provider_id, exc,
        )
        response, outcome = None, "transport_error"
    except AflJsonInvalidResponse as exc:
        # response_diagnostics is safe, whitelisted structural metadata only
        # (HTTP status, Content-Type/-Encoding, content length, declared
        # encoding, redirect count, and a JSON/HTML/empty/unknown shape
        # classification) -- never tokens, never any response body content,
        # per docs/architecture/workflows/scheduler_workflow_design.md's
        # "never store tokens, response bodies, or unsafe exception details
        # merely for scheduler diagnosis". See afl_json.client._response_diagnostics.
        # This is what would have let a live "invalid JSON" failure be
        # root-caused (a URL resolving to the wrong CFS base path, returning
        # an HTML error page) without a manual Bruno comparison.
        log.warning(
            "commentaryFeed malformed response match_id=%s match_provider_id=%s error=%s diagnostics=%s",
            match_id, match_provider_id, exc, exc.response_diagnostics,
        )
        response, outcome = None, "invalid_response"
    except AflJsonHttpError as exc:
        log.warning(
            "commentaryFeed HTTP failure match_id=%s match_provider_id=%s error=%s",
            match_id, match_provider_id, exc,
        )
        response, outcome = None, "http_error"

    def op(conn):
        local_status = _current_match_status(conn, match_id)
        observed_at = clock().isoformat()
        if outcome is not None:
            return persist_poll_outcome(
                conn, match_id=match_id, match_provider_id=match_provider_id,
                observed_at=observed_at, match_status_at_poll=local_status, outcome=outcome,
            )
        try:
            current = parse_match_commentary(
                response.data, match_id=match_id, match_provider_id=match_provider_id,
                observed_at=observed_at, match_status_at_poll=local_status,
            )
        except MatchCommentaryEvidenceError as exc:
            log.warning(
                "commentaryFeed malformed payload match_id=%s match_provider_id=%s error=%s",
                match_id, match_provider_id, exc,
            )
            return persist_poll_outcome(
                conn, match_id=match_id, match_provider_id=match_provider_id,
                observed_at=observed_at, match_status_at_poll=local_status, outcome="malformed_payload",
            )
        return persist_observation(conn, current)

    result = write_lane.execute("match_commentary_evidence.persist", match_provider_id, op)
    if result["outcome"] == "success" and result["new_event_count"]:
        log.info(
            "match_commentary_evidence new_events match_id=%s match_provider_id=%s poll_sequence=%s "
            "new_event_count=%s flags=%s",
            match_id, match_provider_id, result["poll_sequence"], result["new_event_count"],
            ",".join(result["transitions"]),
        )
    return {"match_id": match_id, "match_provider_id": match_provider_id, **result}


def capture_live_match_commentary(*, client: AflJsonClient | None = None,
                                  clock: Callable[[], datetime] | None = None) -> list[dict[str, Any]]:
    """Poll every in-window match's commentaryFeed once and persist diagnostic evidence.

    "In-window" is the union of currently-LIVE matches, matches within the
    bounded kickoff tolerance, and matches within this profile's own bounded
    post-LIVE grace (see module docstring). Failures for one match never
    abort collection for the others, so this is safe to leave running
    unattended across a full round of matches.
    """
    settings = MatchCommentaryCaptureSettings.from_config()
    if not settings.enabled:
        return []
    clock_fn = clock or (lambda: datetime.now(timezone.utc))
    # Used only for candidate-window selection; each match's own observed_at
    # is captured separately, per-response, inside _capture_one.
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
                "commentaryFeed evidence capture failed match_id=%s match_provider_id=%s error=%s",
                match_id, match_provider_id, exc,
            )
        except Exception:
            log.exception(
                "Unexpected commentaryFeed evidence capture failure match_id=%s match_provider_id=%s",
                match_id, match_provider_id,
            )
    return results
