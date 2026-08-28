"""Machine anomaly evidence and explicitly human-reviewed dataset exceptions.

Neither API changes provider payloads or canonical match lifecycle.  Detection
only describes evidence; only an active persisted review changes completeness.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

REASON_CODES = frozenset({
    "abandoned", "cancelled", "forfeit", "not_played",
    "historical_data_unavailable", "provider_data_unavailable", "other",
})
EXCEPTION_TYPE = "stats_not_expected"


@dataclass(frozen=True, slots=True)
class StatsAbsenceCandidate:
    match_id: int
    provider_match_id: str | None
    lifecycle: str | None
    evidence: dict[str, object]

    def to_dict(self):
        return asdict(self)


def active_stats_exception(conn: sqlite3.Connection, match_id: int):
    return conn.execute(
        "SELECT * FROM match_data_exceptions WHERE match_id=? AND exception_type=? "
        "AND revoked_at IS NULL", (match_id, EXCEPTION_TYPE),
    ).fetchone()


def review_stats_not_expected(conn: sqlite3.Connection, *, match_id: int,
                              reason_code: str, display_reason: str,
                              evidence_url: str | None = None,
                              evidence_note: str | None = None,
                              actor: str = "cli", clock=None):
    """Idempotently create/update a review and append audit history on change."""
    if reason_code not in REASON_CODES:
        raise ValueError(f"unsupported reason code: {reason_code}")
    if not display_reason.strip():
        raise ValueError("display reason must not be empty")
    match = conn.execute(
        "SELECT match_provider_id FROM matches WHERE match_id=?", (match_id,)
    ).fetchone()
    if match is None:
        raise ValueError(f"canonical match {match_id} does not exist")
    provider_id = match[0]
    now = (clock or (lambda: datetime.now(timezone.utc)))().isoformat()
    existing = conn.execute(
        "SELECT * FROM match_data_exceptions WHERE match_id=? AND exception_type=?",
        (match_id, EXCEPTION_TYPE),
    ).fetchone()
    values = (provider_id, reason_code, display_reason.strip(), evidence_url,
              evidence_note, actor)
    if existing is not None:
        old = tuple(existing[key] for key in (
            "provider_match_id", "reason_code", "display_reason", "evidence_url",
            "evidence_note", "created_by"))
        if old == values and existing["revoked_at"] is None:
            return existing
        action = "updated"
        conn.execute(
            "UPDATE match_data_exceptions SET provider_match_id=?,reason_code=?,"
            "display_reason=?,evidence_url=?,evidence_note=?,created_by=?,updated_at=?,"
            "revoked_at=NULL WHERE match_id=? AND exception_type=?",
            (*values, now, match_id, EXCEPTION_TYPE),
        )
    else:
        action = "created"
        conn.execute(
            "INSERT INTO match_data_exceptions VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
            (match_id, provider_id, EXCEPTION_TYPE, reason_code, display_reason.strip(),
             evidence_url, evidence_note, actor, now, now),
        )
    conn.execute(
        "INSERT INTO match_data_exception_audit(match_id,exception_type,action,reason_code,"
        "display_reason,evidence_url,evidence_note,actor,occurred_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (match_id, EXCEPTION_TYPE, action, reason_code, display_reason.strip(),
         evidence_url, evidence_note, actor, now),
    )
    return conn.execute(
        "SELECT * FROM match_data_exceptions WHERE match_id=? AND exception_type=?",
        (match_id, EXCEPTION_TYPE),
    ).fetchone()


def revoke_stats_not_expected(conn: sqlite3.Connection, *, match_id: int,
                              actor: str = "cli", clock=None) -> bool:
    row = active_stats_exception(conn, match_id)
    if row is None:
        return False
    now = (clock or (lambda: datetime.now(timezone.utc)))().isoformat()
    conn.execute("UPDATE match_data_exceptions SET revoked_at=?,updated_at=? "
                 "WHERE match_id=? AND exception_type=?", (now, now, match_id, EXCEPTION_TYPE))
    conn.execute(
        "INSERT INTO match_data_exception_audit(match_id,exception_type,action,reason_code,"
        "display_reason,evidence_url,evidence_note,actor,occurred_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (match_id, EXCEPTION_TYPE, "revoked", row["reason_code"], row["display_reason"],
         row["evidence_url"], row["evidence_note"], actor, now),
    )
    return True


def detect_stats_absence_candidates(conn: sqlite3.Connection, *, season_id: int | None = None):
    """Report concluded/no-authoritative-stat candidates without assigning a cause."""
    conn.row_factory = sqlite3.Row
    params: list[object] = []
    where = "WHERE UPPER(m.status)='CONCLUDED'"
    if season_id is not None:
        where += " AND m.season_id=?"
        params.append(season_id)
    rows = conn.execute(
        "SELECT m.match_id,m.match_provider_id,m.status,m.score_home,m.score_away,m.source_json,"
        "(SELECT COUNT(*) FROM cfs_player_stats s WHERE s.match_provider_id=m.match_provider_id "
        " AND s.snapshot_authority=2) stat_rows,"
        "(SELECT COUNT(*) FROM scrape_runs r WHERE r.target_identifier=m.match_provider_id "
        " AND r.scrape_type='season_match_player_stats' AND r.status IN ('completed','partial') "
        " AND COALESCE(r.rows_read,0)=0) empty_attempts "
        f"FROM matches m {where} ORDER BY m.match_id", params,
    ).fetchall()
    candidates = []
    for row in rows:
        if row["stat_rows"]:
            continue
        try:
            source = json.loads(row["source_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            source = {}
        candidates.append(StatsAbsenceCandidate(row["match_id"], row["match_provider_id"],
            row["status"], {
                "authoritative_stat_rows": 0,
                "successful_empty_stat_attempts": row["empty_attempts"],
                "score": {"home": row["score_home"], "away": row["score_away"]},
                "score_is_zero_zero": row["score_home"] == 0 and row["score_away"] == 0,
                "provider_source_present": bool(source),
                "reviewed_disposition": None,
            }))
    return candidates
