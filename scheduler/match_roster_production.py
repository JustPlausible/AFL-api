"""Production CFS match-roster polling (Issue #219).

Polls the round-scoped CFS ``matchRosters/round/{round_provider_id}`` endpoint
for rounds with an upcoming or in-progress match and persists normalized,
canonically-linked current-state roster selection and change/context records
for the ``/api/v1/matches/{match_id}/rosters`` consumer route (see
``afl_json/rosters.py`` and ``api/routes_v1.py``).

This is a normal production collector, not a diagnostic profile: it runs
unconditionally (subject only to its own ``AFL_ROSTER_PRODUCTION_ENABLED``
flag), registered directly in ``scheduler/scheduled_tasks.py`` alongside the
other production jobs.

## Candidate selection and scheduling model

Deliberately mirrors ``scheduler.match_commentary_production``/
``scheduler.match_interchange_production``'s stateless candidate-window
pattern, but at **round** granularity rather than per-match, because the
underlying CFS endpoint is itself round-scoped: one request refreshes every
match in that round at once. A round is a candidate when it has a
``provider_id`` and at least one match that is either:

* currently ``LIVE`` (``matches.status='LIVE'``); or
* within ``AFL_ROSTER_PRODUCTION_PRE_ROUND_WINDOW_SECONDS`` of its scheduled
  ``start_time_utc`` (default 24h, mirroring the legacy HTML lineup
  scheduler's "T-1 day" trigger -- ``scheduler.schedule_lineup_scrapes``);
  or
* past its scheduled ``start_time_utc`` by no more than
  ``AFL_ROSTER_PRODUCTION_KICKOFF_TOLERANCE_SECONDS`` (default 600s) while
  still locally ``SCHEDULED`` -- catches a delayed local status flip to
  ``LIVE``, mirroring the other production collectors' kickoff-tolerance
  windows.

A round with every match ``CONCLUDED`` is never a candidate -- polling stops
entirely once a round is finished, rather than continuing through a bounded
grace window. Unlike interchange, there is deliberately no separate "one
final reconciliation poll" bookkeeping: docs/match_rosters.md records that a
pre-bounce and a LIVE capture of the same match differed only in the roster
timestamp, so there is no evidence a post-conclusion poll would observe
anything a pre-conclusion poll had not already captured, and the round
simply stops being polled once every match in it concludes.

Persistence (``afl_json.rosters.persist_match_rosters``) is idempotent by
durable-state diff (a current-state projection, replaced per valid
``PUBLISHED`` observation -- see its module docstring), so there is no
correctness risk from re-polling a round that has not changed, and no
concurrent-worker scenario exists in this single sequential poll loop.

Roster availability must never affect authoritative match finality or any
other production collection path -- this module never raises out of a poll
cycle, and a failure/unavailability for one round never blocks capture for
any other round in the same cycle.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import config
from analytics.contracts import UpstreamOutcome
from analytics.record import record_upstream_poll
from afl_json.client import (
    AflJsonAuthenticationError,
    AflJsonClient,
    AflJsonError,
    AflJsonHttpError,
    AflJsonInvalidResponse,
    AflJsonTransportError,
)
from afl_json.rosters import MatchRosterCollector, RosterStatus, persist_match_rosters
from scheduler.time_policy import MetadataTimestampError, parse_metadata_timestamp
from scheduler.write_lane import write_lane
from db.connection import get_db_connection
from utils.log import setup_logger
from logging_sources import LOG_SOURCES

_source = LOG_SOURCES["match_roster_production"]
log = setup_logger(_source.logger_name, _source.filename)


@dataclass(frozen=True)
class MatchRosterProductionSettings:
    enabled: bool = True
    interval_seconds: int = 900
    pre_round_window_seconds: int = 86400
    kickoff_tolerance_seconds: int = 600

    @classmethod
    def from_config(cls) -> "MatchRosterProductionSettings":
        settings = cls(
            enabled=config.AFL_ROSTER_PRODUCTION_ENABLED,
            interval_seconds=config.AFL_ROSTER_PRODUCTION_INTERVAL_SECONDS,
            pre_round_window_seconds=config.AFL_ROSTER_PRODUCTION_PRE_ROUND_WINDOW_SECONDS,
            kickoff_tolerance_seconds=config.AFL_ROSTER_PRODUCTION_KICKOFF_TOLERANCE_SECONDS,
        )
        if settings.interval_seconds <= 0:
            raise ValueError("AFL_ROSTER_PRODUCTION_INTERVAL_SECONDS must be positive")
        if settings.pre_round_window_seconds < 0:
            raise ValueError("AFL_ROSTER_PRODUCTION_PRE_ROUND_WINDOW_SECONDS must not be negative")
        if settings.kickoff_tolerance_seconds < 0:
            raise ValueError("AFL_ROSTER_PRODUCTION_KICKOFF_TOLERANCE_SECONDS must not be negative")
        return settings


_client_lock = threading.Lock()
_client: AflJsonClient | None = None


def _get_client() -> AflJsonClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AflJsonClient()
        return _client


def shutdown_match_roster_production_client() -> None:
    """Close the pooled production client; safe to call even if never opened."""
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()


def _round_candidates(conn, *, now: datetime,
                      settings: MatchRosterProductionSettings) -> list[tuple[int, str]]:
    """Rounds with a live or soon/recently-kicked-off match, read-only.

    Never writes ``matches.status``/``rounds`` -- that remains the job of the
    existing public JSON status refresh. A round with every match
    ``CONCLUDED`` is excluded entirely (see module docstring).
    """
    candidates: list[tuple[int, str]] = []
    rounds = conn.execute(
        "SELECT round_id, provider_id FROM rounds WHERE provider_id IS NOT NULL"
    ).fetchall()
    for round_id, provider_id in rounds:
        match_rows = conn.execute(
            "SELECT status, start_time_utc FROM matches WHERE round_id=?", (round_id,)
        ).fetchall()
        if not match_rows:
            continue
        if all(status == "CONCLUDED" for status, _start in match_rows):
            continue
        active = False
        for status, start_time_utc in match_rows:
            if status == "LIVE":
                active = True
                break
            if not start_time_utc:
                continue
            try:
                start = parse_metadata_timestamp(start_time_utc)
            except MetadataTimestampError:
                continue
            elapsed = (now - start).total_seconds()
            if -settings.pre_round_window_seconds <= elapsed <= settings.kickoff_tolerance_seconds:
                active = True
                break
        if active:
            candidates.append((round_id, provider_id))
    return candidates


def _capture_one(client: AflJsonClient, round_id: int, round_provider_id: str, *,
                 clock: Callable[[], datetime],
                 interval_seconds: int = MatchRosterProductionSettings.interval_seconds) -> dict[str, Any]:
    """Poll and persist one round's production roster state.

    Mirrors ``scheduler.match_interchange_production._capture_one``'s
    outcome mapping and per-round error isolation, adapted for
    ``MatchRosterCollector.collect`` (which raises ``AflJsonInvalidResponse``
    for a malformed payload rather than returning a result) and for the
    round-scoped, not per-match, unit of work.
    """
    started = time.monotonic()
    outcome: str
    result = None
    try:
        # MatchRosterCollector.collect already maps a not-yet-published round
        # (CFS 404/not-published) to RosterCollectionResult(..., UNAVAILABLE,
        # ...) internally rather than raising -- see afl_json.rosters.collect
        # -- so AflJsonResourceUnavailable never escapes this call. The
        # granular business outcome is read from result.status below, not
        # from a caught exception.
        result = MatchRosterCollector(client).collect(round_provider_id)
        outcome = "success"
    except AflJsonAuthenticationError as exc:
        log.warning(
            "matchRosters authentication failure round_id=%s round_provider_id=%s error=%s",
            round_id, round_provider_id, exc,
        )
        outcome = "auth_error"
    except AflJsonTransportError as exc:
        log.warning(
            "matchRosters transport failure round_id=%s round_provider_id=%s error=%s",
            round_id, round_provider_id, exc,
        )
        outcome = "transport_error"
    except AflJsonInvalidResponse as exc:
        log.warning(
            "matchRosters malformed response round_id=%s round_provider_id=%s error=%s",
            round_id, round_provider_id, exc,
        )
        outcome = "malformed_payload"
    except AflJsonHttpError as exc:
        log.warning(
            "matchRosters HTTP failure round_id=%s round_provider_id=%s error=%s",
            round_id, round_provider_id, exc,
        )
        outcome = "http_error"

    if result is None or result.status is not RosterStatus.PUBLISHED:
        # UNAVAILABLE (null), EMPTY ([]) and every transport/auth/malformed
        # failure above are deliberately never persisted -- persist_match_rosters
        # would itself no-op for UNAVAILABLE/EMPTY, but a failed collect()
        # never even reaches persistence at all, exactly matching module
        # docstring "A previously valid canonical roster must not be erased".
        summary_status = result.status.value if result is not None else outcome
        return {"round_id": round_id, "round_provider_id": round_provider_id, "outcome": outcome,
                "status": summary_status, "rosters_written": 0, "selections_written": 0,
                "context_written": 0}

    def op(conn):
        return persist_match_rosters(conn, result, observed_at=clock().isoformat())

    summary = write_lane.execute("match_roster_production.persist", round_provider_id, op)
    if summary.rosters_written:
        log.info(
            "match_roster_production persisted round_id=%s round_provider_id=%s rosters=%s "
            "selections=%s context=%s unmatched_matches=%s unmatched_teams=%s",
            round_id, round_provider_id, summary.rosters_written, summary.selections_written,
            summary.context_written, len(summary.unmatched_matches), len(summary.unmatched_teams),
        )
    duration_ms = (time.monotonic() - started) * 1000
    record_upstream_poll(
        resource="match_rosters", observed_at=clock(), configured_interval_seconds=interval_seconds,
        duration_ms=duration_ms, outcome=UpstreamOutcome.SUCCESS,
        changed=summary.state_changed, change_magnitude=summary.change_magnitude,
        note=f"round_provider_id={round_provider_id}",
    )
    return {"round_id": round_id, "round_provider_id": round_provider_id, "outcome": "success",
            "status": result.status.value, "rosters_written": summary.rosters_written,
            "selections_written": summary.selections_written,
            "context_written": summary.context_written}


def poll_match_rosters(*, client: AflJsonClient | None = None,
                       clock: Callable[[], datetime] | None = None) -> list[dict[str, Any]]:
    """Poll every in-window round's matchRosters once and persist production state.

    Failures for one round never abort collection for the others, and this
    module never raises out of a poll cycle -- roster availability must never
    affect authoritative match finality or any other production collection
    path (Issue #219).
    """
    settings = MatchRosterProductionSettings.from_config()
    if not settings.enabled:
        return []
    clock_fn = clock or (lambda: datetime.now(timezone.utc))
    now = clock_fn()
    conn = get_db_connection()
    try:
        rounds = _round_candidates(conn, now=now, settings=settings)
    finally:
        conn.close()
    if not rounds:
        return []
    active_client = client or _get_client()
    results: list[dict[str, Any]] = []
    for round_id, round_provider_id in rounds:
        try:
            results.append(_capture_one(active_client, round_id, round_provider_id, clock=clock_fn,
                                        interval_seconds=settings.interval_seconds))
        except AflJsonError as exc:
            log.warning(
                "matchRosters production capture failed round_id=%s round_provider_id=%s error=%s",
                round_id, round_provider_id, exc,
            )
        except Exception:
            log.exception(
                "Unexpected matchRosters production capture failure round_id=%s round_provider_id=%s",
                round_id, round_provider_id,
            )
    return results
