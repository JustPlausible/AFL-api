"""Diagnostic-only opt-in capture of live CFS matchItem evidence (Issue #148).

Polls the existing CFS matchItem endpoint for live matches at a configurable
interval and retains lightweight/raw evidence about score.matchClock.periods,
periodCompleted, periodSeconds, match.status and score.status so a future
decision can be evaluated against real evidence.

Candidate selection is intentionally slightly wider than a strict
``matches.status='LIVE'`` snapshot, to cover two known boundary conditions
around the local status-refresh cadence (``schedule_match_scrapes``, which
only refreshes ``matches.status`` roughly every 5 minutes):

* a bounded *kickoff tolerance* window so capture can start shortly after a
  match's scheduled start even if the local status row has not yet been
  refreshed from SCHEDULED to LIVE; and
* a bounded *post-LIVE grace* window, driven entirely by this diagnostic
  module's own captured evidence (never by production tables), so capture
  keeps polling for a little while after the local status row moves away
  from LIVE -- covering the case where the Q4 ``periodCompleted``/full-time
  transition happens close to, or just after, that local refresh.

Both windows are bounded, stateless (recomputed from durable timestamps on
every poll) and self-terminating; neither reads nor writes any production
scheduling state. This module is intentionally simple and separate from the
durable match_stat_windows lease/finality machinery used for production CFS
player-stat polling: it does not claim leases, does not participate in
scrape_runs auditing or interrupted-attempt recovery, and its output is never
read by scheduler decision-making. It is inert unless
AFL_CAPTURE_MATCH_STATE_EVIDENCE is explicitly enabled.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import config
from afl_json.client import AflJsonClient, AflJsonError
from collection.match_state_evidence import (
    MATCH_ITEM_ENDPOINT, detect_transitions, load_previous_observation,
    parse_match_item, persist_observation, recently_live_match_provider_ids,
)
from db.connection import get_db_connection
from scheduler.time_policy import MetadataTimestampError, parse_metadata_timestamp
from scheduler.write_lane import write_lane
from utils.log import setup_logger

log = setup_logger("match_state_capture", "match_state_capture.log")


@dataclass(frozen=True)
class MatchStateCaptureSettings:
    enabled: bool = False
    interval_seconds: int = 15
    # Defaults are ~2x the ~5 minute (+ up to 30s jitter) local match-status
    # refresh cadence in schedule_match_scrapes.py, giving comfortable margin
    # against a single missed/delayed refresh cycle at either boundary.
    kickoff_tolerance_seconds: int = 600
    post_live_grace_seconds: int = 600

    @classmethod
    def from_config(cls) -> "MatchStateCaptureSettings":
        settings = cls(
            enabled=config.AFL_CAPTURE_MATCH_STATE_EVIDENCE,
            interval_seconds=config.AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS,
            kickoff_tolerance_seconds=config.AFL_MATCH_STATE_CAPTURE_KICKOFF_TOLERANCE_SECONDS,
            post_live_grace_seconds=config.AFL_MATCH_STATE_CAPTURE_POST_LIVE_GRACE_SECONDS,
        )
        if settings.interval_seconds <= 0:
            raise ValueError("AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS must be positive")
        if settings.kickoff_tolerance_seconds < 0:
            raise ValueError("AFL_MATCH_STATE_CAPTURE_KICKOFF_TOLERANCE_SECONDS must not be negative")
        if settings.post_live_grace_seconds < 0:
            raise ValueError("AFL_MATCH_STATE_CAPTURE_POST_LIVE_GRACE_SECONDS must not be negative")
        return settings


_client_lock = threading.Lock()
_client: AflJsonClient | None = None


def _get_client() -> AflJsonClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AflJsonClient()
        return _client


def shutdown_match_state_capture_client() -> None:
    """Close the pooled diagnostic client; safe to call even if never opened."""
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()


def _live_matches(conn) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT match_id, match_provider_id FROM matches WHERE status='LIVE' AND match_provider_id IS NOT NULL"
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _kickoff_tolerance_matches(conn, *, now: datetime, tolerance_seconds: int) -> list[tuple[int, str]]:
    """Matches not yet locally LIVE whose scheduled kickoff has already passed
    within a bounded tolerance. Read-only against ``matches``; never writes
    ``matches.status`` itself -- that remains the job of the existing public
    JSON status refresh."""
    if tolerance_seconds <= 0:
        return []
    candidates: list[tuple[int, str]] = []
    rows = conn.execute(
        "SELECT match_id, match_provider_id, start_time_utc FROM matches "
        "WHERE status='SCHEDULED' AND match_provider_id IS NOT NULL"
    ).fetchall()
    for match_id, match_provider_id, raw_start in rows:
        try:
            start = parse_metadata_timestamp(raw_start)
        except MetadataTimestampError:
            continue
        elapsed = (now - start).total_seconds()
        if 0 <= elapsed <= tolerance_seconds:
            candidates.append((match_id, match_provider_id))
    return candidates


def _capture_candidates(conn, *, now: datetime, settings: MatchStateCaptureSettings) -> list[tuple[int, str]]:
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


def _capture_one(client: AflJsonClient, match_id: int, match_provider_id: str, *, now: datetime) -> dict[str, Any]:
    response = client.request(MATCH_ITEM_ENDPOINT, path_parameters={"match_provider_id": match_provider_id})
    current = parse_match_item(
        response.data, match_id=match_id, match_provider_id=match_provider_id, observed_at=now.isoformat()
    )

    def op(conn):
        previous, previous_flags = load_previous_observation(conn, match_provider_id)
        transitions = detect_transitions(previous, previous_flags, current)
        outcome = persist_observation(conn, current, transitions)
        return outcome

    outcome = write_lane.execute("match_state_evidence.persist", match_provider_id, op)
    if outcome["transitions"]:
        log.info(
            "match_state_evidence transition match_id=%s match_provider_id=%s poll_sequence=%s "
            "flags=%s match_status=%s score_status=%s latest_period_number=%s "
            "latest_period_seconds=%s latest_period_completed=%s",
            match_id, match_provider_id, outcome["poll_sequence"], ",".join(outcome["transitions"]),
            current.match_status, current.score_status, current.latest_period_number,
            current.latest_period_seconds, current.latest_period_completed,
        )
    return {"match_id": match_id, "match_provider_id": match_provider_id, **outcome}


def capture_live_match_state(*, client: AflJsonClient | None = None,
                             clock: Callable[[], datetime] | None = None) -> list[dict[str, Any]]:
    """Poll every in-window match's matchItem once and persist diagnostic evidence.

    "In-window" is the union of currently-LIVE matches, matches within the
    bounded kickoff tolerance, and matches within the bounded post-LIVE grace
    (see module docstring). Failures for one match never abort collection for
    the others, so this is safe to leave running unattended across a full
    round of matches.
    """
    settings = MatchStateCaptureSettings.from_config()
    if not settings.enabled:
        return []
    now = (clock or (lambda: datetime.now(timezone.utc)))()
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
            results.append(_capture_one(active_client, match_id, match_provider_id, now=now))
        except AflJsonError as exc:
            log.warning(
                "matchItem evidence capture failed match_id=%s match_provider_id=%s error=%s",
                match_id, match_provider_id, exc,
            )
        except Exception:
            log.exception(
                "Unexpected matchItem evidence capture failure match_id=%s match_provider_id=%s",
                match_id, match_provider_id,
            )
    return results


def register_match_state_capture_job(scheduler) -> bool:
    """Register the opt-in diagnostic capture job; a no-op unless explicitly enabled."""
    settings = MatchStateCaptureSettings.from_config()
    if not settings.enabled:
        log.info(
            "Match-state evidence capture disabled (AFL_CAPTURE_MATCH_STATE_EVIDENCE=false); skipping registration."
        )
        return False
    from apscheduler.triggers.interval import IntervalTrigger

    from scheduler.registry import add_registered_job, match_state_capture_job_id
    add_registered_job(
        scheduler, capture_live_match_state,
        trigger=IntervalTrigger(seconds=settings.interval_seconds), args=[],
        job_id=match_state_capture_job_id(), job_type="match_state_evidence_capture",
        name="Diagnostic-only live matchItem evidence capture (Issue #148)",
        replace_existing=True, trigger_type="interval",
    )
    log.info(
        "✅ Match-state evidence capture enabled at %ss interval (kickoff_tolerance=%ss, post_live_grace=%ss).",
        settings.interval_seconds, settings.kickoff_tolerance_seconds, settings.post_live_grace_seconds,
    )
    return True
