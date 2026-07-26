"""Reconcile canonical match status with the public direct-detail endpoint."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Callable, Mapping

from .client import AflJsonClient, AflJsonError


class MatchLifecycle(IntEnum):
    SCHEDULED = 1
    LIVE = 2
    POSTGAME = 3
    CONCLUDED = 4


_ALIASES = {"COMPLETED": "CONCLUDED", "FINAL": "CONCLUDED"}


def normalise_match_status(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    status = value.strip().upper()
    status = _ALIASES.get(status, status)
    return status if status in MatchLifecycle.__members__ else None


def later_match_status(left: Any, right: Any) -> str | None:
    """Return the latest recognised lifecycle value without moving backwards."""
    statuses = [status for status in (
        normalise_match_status(left), normalise_match_status(right)
    ) if status]
    return max(statuses, key=lambda status: MatchLifecycle[status]) if statuses else None


@dataclass(frozen=True, slots=True)
class MatchStatusDiagnostic:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class MatchStatusResolution:
    match_provider_id: str
    afl_match_id: int | None
    stored_status: str | None
    direct_status: str | None
    resolved_status: str | None
    resolution_source: str | None
    canonical_refreshed: bool
    diagnostics: tuple[MatchStatusDiagnostic, ...]


def reconcile_match_status(
    conn: sqlite3.Connection,
    client: AflJsonClient,
    *,
    match_provider_id: str,
    afl_match_id: int | None = None,
    clock: Callable[[], datetime] | None = None,
) -> MatchStatusResolution:
    """Resolve a match row, then consult direct detail only when it can advance it."""
    row = conn.execute(
        "SELECT match_id, status FROM matches WHERE match_provider_id = ? LIMIT 1",
        (match_provider_id,),
    ).fetchone()
    stored_raw = row[1] if row else None
    stored_status = normalise_match_status(stored_raw)
    effective_match_id = afl_match_id if afl_match_id is not None else (row[0] if row else None)
    diagnostics: list[MatchStatusDiagnostic] = []
    if stored_raw and stored_status is None:
        diagnostics.append(MatchStatusDiagnostic(
            "unrecognised_stored_status", f"Stored match status {stored_raw!r} is not recognised"
        ))

    if stored_status == "CONCLUDED":
        return MatchStatusResolution(match_provider_id, effective_match_id, stored_status, None,
                                     stored_status, "canonical_match_database", False,
                                     tuple(diagnostics))
    if effective_match_id is None:
        diagnostics.append(MatchStatusDiagnostic(
            "missing_afl_match_id", "No AFL numeric match ID is available for direct reconciliation"
        ))
        return MatchStatusResolution(match_provider_id, None, stored_status, None, stored_status,
                                     "canonical_match_database" if stored_status else None,
                                     False, tuple(diagnostics))

    direct_status = None
    try:
        payload = client.get(
            "match_detail", path_parameters={"afl_match_id": effective_match_id}
        ).data
        direct_status = _direct_status(payload, effective_match_id, match_provider_id)
        if direct_status is None:
            diagnostics.append(MatchStatusDiagnostic(
                "unrecognised_direct_status", "Direct match-detail status is not recognised"
            ))
    except (AflJsonError, ValueError, TypeError, KeyError) as exc:
        diagnostics.append(MatchStatusDiagnostic(
            "direct_match_detail_unavailable",
            f"Direct match-detail reconciliation failed: {type(exc).__name__}",
        ))

    resolved = later_match_status(stored_status, direct_status)
    source = None
    if resolved:
        source = ("direct_match_detail" if direct_status == resolved and direct_status != stored_status
                  else "canonical_match_database")
    if direct_status and stored_status and MatchLifecycle[direct_status] < MatchLifecycle[stored_status]:
        diagnostics.append(MatchStatusDiagnostic(
            "direct_status_regression",
            f"Ignored direct status {direct_status}; stored status {stored_status} is later",
        ))

    refreshed = bool(row and direct_status and resolved == direct_status
                     and direct_status != stored_status)
    if refreshed:
        columns = {column[1] for column in conn.execute("PRAGMA table_info(matches)")}
        if "updated_at" in columns:
            now = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat()
            conn.execute("UPDATE matches SET status = ?, updated_at = ? WHERE match_provider_id = ?",
                         (direct_status, now, match_provider_id))
        else:
            conn.execute("UPDATE matches SET status = ? WHERE match_provider_id = ?",
                         (direct_status, match_provider_id))
    return MatchStatusResolution(match_provider_id, effective_match_id, stored_status, direct_status,
                                 resolved, source, refreshed, tuple(diagnostics))


def _direct_status(payload: Any, afl_match_id: int, match_provider_id: str) -> str | None:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("matches"), list):
        raise ValueError("Direct match-detail response has no matches collection")
    matches = payload["matches"]
    if len(matches) != 1 or not isinstance(matches[0], Mapping):
        raise ValueError("Direct match-detail response must contain exactly one match")
    match = matches[0]
    if match.get("id") != afl_match_id or match.get("providerId") != match_provider_id:
        raise ValueError("Direct match-detail identifiers do not match the requested match")
    return normalise_match_status(match.get("status"))
