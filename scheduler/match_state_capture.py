"""Diagnostic-only opt-in capture of live CFS matchItem evidence (Issue #148).

Polls the existing CFS matchItem endpoint for every currently LIVE match at a
configurable interval and retains lightweight/raw evidence about
matchClock.periods, periodCompleted, periodSeconds, match.status and
score.status so a future decision can be evaluated against real evidence.

This module is intentionally simple and separate from the durable
match_stat_windows lease/finality machinery used for production CFS
player-stat polling: it does not claim leases, does not participate in
scrape_runs auditing or interrupted-attempt recovery, and its output is never
read by scheduler decision-making. It is inert unless
AFL_CAPTURE_MATCH_STATE_EVIDENCE is explicitly enabled.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import config
from afl_json.client import AflJsonClient, AflJsonError
from collection.match_state_evidence import (
    MATCH_ITEM_ENDPOINT, detect_transitions, load_previous_observation,
    parse_match_item, persist_observation,
)
from db.connection import get_db_connection
from scheduler.write_lane import write_lane
from utils.log import setup_logger

log = setup_logger("match_state_capture", "match_state_capture.log")


@dataclass(frozen=True)
class MatchStateCaptureSettings:
    enabled: bool = False
    interval_seconds: int = 15

    @classmethod
    def from_config(cls) -> "MatchStateCaptureSettings":
        settings = cls(
            enabled=config.AFL_CAPTURE_MATCH_STATE_EVIDENCE,
            interval_seconds=config.AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS,
        )
        if settings.interval_seconds <= 0:
            raise ValueError("AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS must be positive")
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


def _capture_one(client: AflJsonClient, match_id: int, match_provider_id: str) -> dict[str, Any]:
    response = client.request(MATCH_ITEM_ENDPOINT, path_parameters={"match_provider_id": match_provider_id})
    observed_at = datetime.now(timezone.utc).isoformat()
    current = parse_match_item(
        response.data, match_id=match_id, match_provider_id=match_provider_id, observed_at=observed_at
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


def capture_live_match_state(*, client: AflJsonClient | None = None) -> list[dict[str, Any]]:
    """Poll every currently-LIVE match's matchItem once and persist diagnostic evidence.

    Failures for one match never abort collection for the others, so this is
    safe to leave running unattended across a full round of matches.
    """
    settings = MatchStateCaptureSettings.from_config()
    if not settings.enabled:
        return []
    conn = get_db_connection()
    try:
        matches = _live_matches(conn)
    finally:
        conn.close()
    if not matches:
        return []
    active_client = client or _get_client()
    results: list[dict[str, Any]] = []
    for match_id, match_provider_id in matches:
        try:
            results.append(_capture_one(active_client, match_id, match_provider_id))
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
    log.info("✅ Match-state evidence capture enabled at %ss interval.", settings.interval_seconds)
    return True
