"""Production CFS match-commentary polling (Issue #201).

Polls the CFS ``commentaryFeed/{match_provider_id}`` endpoint for in-window
matches and persists normalized, deduplicated, canonically-linked commentary
events for the ``/api/v1/matches/{match_id}/commentary`` consumer route (see
``afl_json/match_commentary.py`` and ``api/routes_v1.py``).

This is a normal production collector, not a diagnostic profile: it runs
unconditionally (subject only to its own ``AFL_COMMENTARY_PRODUCTION_ENABLED``
flag), registered directly in ``scheduler/scheduled_tasks.py`` alongside the
other production jobs, and is never gated by ``AFL_DIAGNOSTICS_ENABLED`` or
``AFL_DIAGNOSTIC_PROFILES``. The still-running ``commentary`` diagnostic
profile (``scheduler/match_commentary_capture.py``) is completely independent
of this module -- neither reads from nor writes to the other's tables.

## Candidate selection and scheduling model

Deliberately reuses the same lightweight, stateless, self-terminating
candidate-window pattern already proven by the commentary/match_clock/
interchange diagnostic profiles, rather than the durable
``match_stat_windows`` lease system used for authoritative CFS player-stat
polling (``scheduler/match_windows.py``, ``scheduler/player_stat_polling.py``).
This is a deliberate, scoped choice, not an oversight:

* commentary is explicitly non-authoritative (Issue #201's constraints) and
  persistence is idempotent by fingerprint, so there is no correctness risk
  from two overlapping polls of the same match -- the lease system's main
  value (preventing duplicate authoritative writes / wasted claims across
  concurrent workers) does not apply here;
* this process runs a single sequential poll loop (like the diagnostic
  profiles), so there is no concurrent-worker scenario to arbitrate between
  in the first place;
* introducing a second consumer of ``match_stat_windows`` (or a parallel
  lease table) purely for a non-authoritative, best-effort stream would be
  the "generic event-bus abstraction... without a strong present-day
  architectural reason" Issue #201 explicitly says to avoid.

Candidates are the union of:

* currently ``LIVE`` matches (``_live_matches``, reused unmodified from
  ``scheduler.match_state_capture`` -- a plain, investigation-agnostic read
  of the ``matches`` table);
* currently ``POSTGAME`` matches (``_postgame_matches`` below -- the same
  plain-read pattern, added because the confirmed ``CD_M20260142406``
  same-slot scoring-outcome change (see ``afl_json.match_commentary`` module
  docstring) demonstrates commentary can still change after a match leaves
  LIVE but before it is finalised);
* a bounded pre-kickoff tolerance window (``_kickoff_tolerance_matches``,
  also reused unmodified from ``scheduler.match_state_capture``);
* a bounded post-active grace window computed from this module's own
  ``match_commentary_polls`` bookkeeping (``recently_active_match_provider_ids``),
  covering the period shortly after a match reaches ``CONCLUDED`` so a
  correction landing right at that boundary is not missed.

Both bounded windows are stateless and recomputed fresh on every poll --
nothing here claims a lease, participates in ``scrape_runs`` auditing, or
needs interrupted-attempt recovery. Restart safety comes for free from
fingerprint-based idempotency (a repeated poll of an unchanged feed is a
no-op) and from ``match_commentary_polls.poll_sequence`` being recomputed
from durable storage on every write, exactly like the diagnostic module.
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
from afl_json.match_commentary import (
    MATCH_COMMENTARY_ENDPOINT, MatchCommentaryError, parse_commentary_feed,
    persist_commentary_feed, persist_poll_outcome, recently_active_match_provider_ids,
)
from scheduler.match_state_capture import _kickoff_tolerance_matches, _live_matches
from scheduler.write_lane import write_lane
from db.connection import get_db_connection
from utils.log import setup_logger
from logging_sources import LOG_SOURCES

_source = LOG_SOURCES["match_commentary_production"]
log = setup_logger(_source.logger_name, _source.filename)


@dataclass(frozen=True)
class MatchCommentaryProductionSettings:
    enabled: bool = True
    interval_seconds: int = 20
    kickoff_tolerance_seconds: int = 600
    postgame_grace_seconds: int = 1800

    @classmethod
    def from_config(cls) -> "MatchCommentaryProductionSettings":
        settings = cls(
            enabled=config.AFL_COMMENTARY_PRODUCTION_ENABLED,
            interval_seconds=config.AFL_COMMENTARY_PRODUCTION_INTERVAL_SECONDS,
            kickoff_tolerance_seconds=config.AFL_COMMENTARY_PRODUCTION_KICKOFF_TOLERANCE_SECONDS,
            postgame_grace_seconds=config.AFL_COMMENTARY_PRODUCTION_POSTGAME_GRACE_SECONDS,
        )
        if settings.interval_seconds <= 0:
            raise ValueError("AFL_COMMENTARY_PRODUCTION_INTERVAL_SECONDS must be positive")
        if settings.kickoff_tolerance_seconds < 0:
            raise ValueError("AFL_COMMENTARY_PRODUCTION_KICKOFF_TOLERANCE_SECONDS must not be negative")
        if settings.postgame_grace_seconds < 0:
            raise ValueError("AFL_COMMENTARY_PRODUCTION_POSTGAME_GRACE_SECONDS must not be negative")
        return settings


_client_lock = threading.Lock()
_client: AflJsonClient | None = None


def _get_client() -> AflJsonClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AflJsonClient()
        return _client


def shutdown_match_commentary_production_client() -> None:
    """Close the pooled production client; safe to call even if never opened."""
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()


def _postgame_matches(conn) -> list[tuple[int, str]]:
    """Plain read of currently-POSTGAME matches. See module docstring."""
    rows = conn.execute(
        "SELECT match_id, match_provider_id FROM matches WHERE status='POSTGAME' AND match_provider_id IS NOT NULL"
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _capture_candidates(conn, *, now: datetime,
                        settings: MatchCommentaryProductionSettings) -> list[tuple[int, str]]:
    seen: set[str] = set()
    ordered: list[tuple[int, str]] = []
    for group in (
        _live_matches(conn),
        _postgame_matches(conn),
        _kickoff_tolerance_matches(conn, now=now, tolerance_seconds=settings.kickoff_tolerance_seconds),
        recently_active_match_provider_ids(conn, now=now, grace_seconds=settings.postgame_grace_seconds),
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
    """Poll and persist one match's production commentary.

    Mirrors ``scheduler.match_commentary_capture._capture_one``'s outcome
    mapping and per-match error isolation (see that module for the detailed
    rationale on each exception -> outcome mapping); this is an independent
    copy against the production persistence functions rather than a shared
    helper, matching this module's "new, narrowly-scoped production path"
    stance (see ``afl_json.match_commentary`` module docstring).
    """
    outcome: str | None
    try:
        response = client.request(MATCH_COMMENTARY_ENDPOINT, path_parameters={"match_provider_id": match_provider_id})
        outcome = None
    except AflJsonResourceUnavailable:
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
            feed = parse_commentary_feed(
                response.data, match_id=match_id, match_provider_id=match_provider_id,
                observed_at=observed_at, match_status_at_poll=local_status,
            )
        except MatchCommentaryError as exc:
            log.warning(
                "commentaryFeed malformed payload match_id=%s match_provider_id=%s error=%s",
                match_id, match_provider_id, exc,
            )
            return persist_poll_outcome(
                conn, match_id=match_id, match_provider_id=match_provider_id,
                observed_at=observed_at, match_status_at_poll=local_status, outcome="malformed_payload",
            )
        return persist_commentary_feed(conn, feed)

    result = write_lane.execute("match_commentary_production.persist", match_provider_id, op)
    if result["outcome"] == "success" and result["new_event_count"]:
        log.info(
            "match_commentary_production new_events match_id=%s match_provider_id=%s poll_sequence=%s "
            "new_event_count=%s possible_edits=%s",
            match_id, match_provider_id, result["poll_sequence"], result["new_event_count"],
            len(result["possible_edits"]),
        )
    return {"match_id": match_id, "match_provider_id": match_provider_id, **result}


def poll_match_commentary(*, client: AflJsonClient | None = None,
                          clock: Callable[[], datetime] | None = None) -> list[dict[str, Any]]:
    """Poll every in-window match's commentaryFeed once and persist production events.

    Failures for one match never abort collection for the others, and this
    module never raises out of a poll cycle -- commentary availability must
    never affect authoritative match finality or any other production
    collection path (Issue #201).
    """
    settings = MatchCommentaryProductionSettings.from_config()
    if not settings.enabled:
        return []
    clock_fn = clock or (lambda: datetime.now(timezone.utc))
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
                "commentaryFeed production capture failed match_id=%s match_provider_id=%s error=%s",
                match_id, match_provider_id, exc,
            )
        except Exception:
            log.exception(
                "Unexpected commentaryFeed production capture failure match_id=%s match_provider_id=%s",
                match_id, match_provider_id,
            )
    return results
