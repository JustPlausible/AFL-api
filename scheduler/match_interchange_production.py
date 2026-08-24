"""Production CFS match-interchange polling (Issue #204).

Polls the CFS ``matchInterchange/{match_provider_id}`` endpoint for in-window
matches and persists normalized, canonically-linked per-player interchange
state and meaningful transition history for the
``/api/v1/matches/{match_id}/interchanges`` consumer routes (see
``afl_json/match_interchange.py`` and ``api/routes_v1.py``).

This is a normal production collector, not a diagnostic profile: it runs
unconditionally (subject only to its own ``AFL_INTERCHANGE_PRODUCTION_ENABLED``
flag), registered directly in ``scheduler/scheduled_tasks.py`` alongside the
other production jobs, and is never gated by ``AFL_DIAGNOSTICS_ENABLED`` or
``AFL_DIAGNOSTIC_PROFILES``. The still-running ``interchange`` diagnostic
profile (``scheduler/match_interchange_capture.py``) is completely
independent of this module -- neither reads from nor writes to the other's
tables.

## Candidate selection and scheduling model

Deliberately mirrors ``scheduler.match_commentary_production``'s stateless,
self-terminating candidate-window pattern (Issue #201's precedent, itself
following the interchange/match_clock diagnostic profiles) rather than the
durable ``match_stat_windows`` lease system used for authoritative CFS
player-stat polling. Interchange state is explicitly non-authoritative for
match finality or player statistics, and persistence is idempotent by
durable-state diff, so there is no correctness risk from two overlapping
polls of the same match, and no concurrent-worker scenario exists in this
single sequential poll loop -- see ``afl_json.match_interchange`` module
docstring "Persistence shape and idempotency".

Candidates are the union of:

* currently ``LIVE`` matches (``_live_matches``, reused unmodified from
  ``scheduler.match_state_capture``) -- this also covers QT/HT/3QT breaks,
  since ``matches.status`` stays ``LIVE`` through a regulation-time break;
* currently ``POSTGAME`` matches (``_postgame_matches`` below);
* a bounded pre-kickoff tolerance window (``_kickoff_tolerance_matches``,
  reused unmodified);
* a bounded post-active grace window computed from this module's own
  ``match_interchange_polls`` bookkeeping
  (``afl_json.match_interchange.recently_active_match_provider_ids``),
  covering the period shortly after a match reaches ``CONCLUDED`` so a final
  poll captures the settled end-of-match state -- this is this module's
  CONCLUDED reconciliation: no separate "final" pass is needed because the
  grace window already keeps polling through the LIVE/POSTGAME -> CONCLUDED
  transition.

Interchange availability must never affect authoritative match finality or
any other production collection path -- this module never raises out of a
poll cycle, and a failure/unavailability for one match never blocks capture
for any other match in the same cycle.
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
from afl_json.match_interchange import (
    MATCH_INTERCHANGE_ENDPOINT, MatchInterchangeError, parse_match_interchange,
    persist_match_interchange, persist_poll_outcome, recently_active_match_provider_ids,
)
from scheduler.match_state_capture import _kickoff_tolerance_matches, _live_matches
from scheduler.write_lane import write_lane
from db.connection import get_db_connection
from utils.log import setup_logger
from logging_sources import LOG_SOURCES

_source = LOG_SOURCES["match_interchange_production"]
log = setup_logger(_source.logger_name, _source.filename)


@dataclass(frozen=True)
class MatchInterchangeProductionSettings:
    enabled: bool = True
    interval_seconds: int = 20
    kickoff_tolerance_seconds: int = 600
    postgame_grace_seconds: int = 1800

    @classmethod
    def from_config(cls) -> "MatchInterchangeProductionSettings":
        settings = cls(
            enabled=config.AFL_INTERCHANGE_PRODUCTION_ENABLED,
            interval_seconds=config.AFL_INTERCHANGE_PRODUCTION_INTERVAL_SECONDS,
            kickoff_tolerance_seconds=config.AFL_INTERCHANGE_PRODUCTION_KICKOFF_TOLERANCE_SECONDS,
            postgame_grace_seconds=config.AFL_INTERCHANGE_PRODUCTION_POSTGAME_GRACE_SECONDS,
        )
        if settings.interval_seconds <= 0:
            raise ValueError("AFL_INTERCHANGE_PRODUCTION_INTERVAL_SECONDS must be positive")
        if settings.kickoff_tolerance_seconds < 0:
            raise ValueError("AFL_INTERCHANGE_PRODUCTION_KICKOFF_TOLERANCE_SECONDS must not be negative")
        if settings.postgame_grace_seconds < 0:
            raise ValueError("AFL_INTERCHANGE_PRODUCTION_POSTGAME_GRACE_SECONDS must not be negative")
        return settings


_client_lock = threading.Lock()
_client: AflJsonClient | None = None


def _get_client() -> AflJsonClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AflJsonClient()
        return _client


def shutdown_match_interchange_production_client() -> None:
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
                        settings: MatchInterchangeProductionSettings) -> list[tuple[int, str]]:
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
    """Poll and persist one match's production interchange state.

    Mirrors ``scheduler.match_commentary_production._capture_one``'s outcome
    mapping and per-match error isolation.
    """
    outcome: str | None
    try:
        response = client.request(MATCH_INTERCHANGE_ENDPOINT, path_parameters={"match_provider_id": match_provider_id})
        outcome = None
    except AflJsonResourceUnavailable:
        response, outcome = None, "not_published"
    except AflJsonAuthenticationError as exc:
        log.warning(
            "matchInterchange authentication failure match_id=%s match_provider_id=%s error=%s",
            match_id, match_provider_id, exc,
        )
        response, outcome = None, "auth_error"
    except AflJsonTransportError as exc:
        log.warning(
            "matchInterchange transport failure match_id=%s match_provider_id=%s error=%s",
            match_id, match_provider_id, exc,
        )
        response, outcome = None, "transport_error"
    except AflJsonInvalidResponse as exc:
        log.warning(
            "matchInterchange malformed response match_id=%s match_provider_id=%s error=%s",
            match_id, match_provider_id, exc,
        )
        response, outcome = None, "invalid_response"
    except AflJsonHttpError as exc:
        log.warning(
            "matchInterchange HTTP failure match_id=%s match_provider_id=%s error=%s",
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
            parsed = parse_match_interchange(
                response.data, match_id=match_id, match_provider_id=match_provider_id,
                observed_at=observed_at, match_status_at_poll=local_status,
            )
        except MatchInterchangeError as exc:
            log.warning(
                "matchInterchange malformed payload match_id=%s match_provider_id=%s error=%s",
                match_id, match_provider_id, exc,
            )
            return persist_poll_outcome(
                conn, match_id=match_id, match_provider_id=match_provider_id,
                observed_at=observed_at, match_status_at_poll=local_status, outcome="malformed_payload",
            )
        return persist_match_interchange(conn, parsed)

    result = write_lane.execute("match_interchange_production.persist", match_provider_id, op)
    if result["outcome"] == "success" and (result["appeared"] or result["disappeared"] or result["changed"]):
        log.info(
            "match_interchange_production transitions match_id=%s match_provider_id=%s poll_sequence=%s "
            "appeared=%s disappeared=%s changed=%s",
            match_id, match_provider_id, result["poll_sequence"], len(result["appeared"]),
            len(result["disappeared"]), len(result["changed"]),
        )
    return {"match_id": match_id, "match_provider_id": match_provider_id, **result}


def poll_match_interchange(*, client: AflJsonClient | None = None,
                           clock: Callable[[], datetime] | None = None) -> list[dict[str, Any]]:
    """Poll every in-window match's matchInterchange once and persist production state.

    Failures for one match never abort collection for the others, and this
    module never raises out of a poll cycle -- interchange availability must
    never affect authoritative match finality or any other production
    collection path (Issue #204).
    """
    settings = MatchInterchangeProductionSettings.from_config()
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
                "matchInterchange production capture failed match_id=%s match_provider_id=%s error=%s",
                match_id, match_provider_id, exc,
            )
        except Exception:
            log.exception(
                "Unexpected matchInterchange production capture failure match_id=%s match_provider_id=%s",
                match_id, match_provider_id,
            )
    return results
