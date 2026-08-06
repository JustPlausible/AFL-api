"""Persisted identity and shutdown evidence for the single Scheduler process."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from enum import Enum

from scheduler.write_lane import write_lane

INSTANCE_ID = f"scheduler-{os.getpid()}-{uuid.uuid4().hex[:12]}"


class RuntimeOwnership(str, Enum):
    ACTIVE = "active"
    GRACEFULLY_STOPPED = "gracefully_stopped"
    STALE_UNCLEAN = "stale_unclean"
    UNKNOWN = "unknown"


def runtime_ownership(row, *, now: datetime, heartbeat_timeout) -> RuntimeOwnership:
    """Classify persisted ownership; age alone never overrides a live heartbeat."""
    if row is None:
        return RuntimeOwnership.UNKNOWN
    stopped = row["stopped_at"]
    if stopped or row["shutdown_kind"] == "graceful":
        return RuntimeOwnership.GRACEFULLY_STOPPED
    heartbeat = datetime.fromisoformat(row["last_heartbeat_at"])
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    if heartbeat.astimezone(timezone.utc) >= now - heartbeat_timeout:
        return RuntimeOwnership.ACTIVE
    return RuntimeOwnership.STALE_UNCLEAN


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def establish_instance() -> str:
    now = _now()

    def op(conn):
        # A prior row without a stop marker is evidence of unclean disappearance;
        # it is not by itself permission to steal a still-valid lease.
        conn.execute(
            "UPDATE scheduler_runtime_instances SET shutdown_kind='unclean' "
            "WHERE stopped_at IS NULL AND instance_id<>?",
            (INSTANCE_ID,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO scheduler_runtime_instances "
            "(instance_id,started_at,last_heartbeat_at) VALUES(?,?,?)",
            (INSTANCE_ID, now, now),
        )

    write_lane.execute_immediate("scheduler_runtime.start", INSTANCE_ID, op)
    return INSTANCE_ID


def heartbeat() -> None:
    write_lane.execute(
        "scheduler_runtime.heartbeat",
        INSTANCE_ID,
        lambda conn: conn.execute(
            "UPDATE scheduler_runtime_instances SET last_heartbeat_at=? "
            "WHERE instance_id=? AND stopped_at IS NULL",
            (_now(), INSTANCE_ID),
        ),
    )


def mark_graceful_shutdown() -> None:
    now = _now()
    write_lane.execute_immediate(
        "scheduler_runtime.stop",
        INSTANCE_ID,
        lambda conn: conn.execute(
            "UPDATE scheduler_runtime_instances SET stopped_at=?,"
            "last_heartbeat_at=?,shutdown_kind='graceful' WHERE instance_id=?",
            (now, now, INSTANCE_ID),
        ),
    )
