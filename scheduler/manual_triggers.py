"""Narrow internal scheduler entry points for admin-requested manual scrapes."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
import sqlite3
from typing import Any

from apscheduler.triggers.date import DateTrigger
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from db.connection import get_db_connection
from db.scrape_runs import TRIGGER_ADMIN_MANUAL
from scheduler.scheduled_tasks import scheduler
from scheduler.registry import add_registered_job, registry_rows, PENDING, RUNNING

router = APIRouter(prefix="/scheduler/manual", tags=["manual scheduler triggers"])

MIGRATION_GUIDANCE = "Required scheduler registry or scrape-run audit tables are missing. Run database migrations before using manual scheduler triggers."

class ManualTriggerRequest(BaseModel):
    round_id: int | None = Field(default=None, ge=1)
    match_id: int | None = Field(default=None, ge=1)


def _require_tables(conn) -> None:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('scheduler_job_registry','scrape_runs')").fetchall()
    names = {r[0] for r in rows}
    if {"scheduler_job_registry", "scrape_runs"} - names:
        raise HTTPException(status_code=503, detail=MIGRATION_GUIDANCE)


def _round_exists(conn, round_id: int) -> None:
    if not conn.execute("SELECT 1 FROM rounds WHERE round_id=? LIMIT 1", (round_id,)).fetchone():
        raise HTTPException(status_code=422, detail="Unknown round identifier")


def _match_exists(conn, match_id: int) -> None:
    if not conn.execute("SELECT 1 FROM matches WHERE match_id=? LIMIT 1", (match_id,)).fetchone():
        raise HTTPException(status_code=422, detail="Unknown match identifier")


def _duplicate(job_type: str, *, round_id: int | None = None, match_id: int | None = None) -> dict[str, Any] | None:
    for row in registry_rows():
        if row.get("trigger_type") != "admin_manual":
            continue
        if row.get("status") not in {PENDING, RUNNING}:
            continue
        if row.get("job_type") != job_type:
            continue
        if match_id is not None and int(row.get("match_id") or 0) == match_id:
            return row
        if round_id is not None and str(row.get("round_id")) == str(round_id):
            return row
        if round_id is None and match_id is None:
            return row
    return None


def _job_id(kind: str, target: str) -> str:
    return f"admin_manual_{kind}_{target}_{uuid4().hex[:12]}"


def manual_refresh_injuries(correlation_id: str):
    conn = get_db_connection()
    try:
        from scraper.scrape_afl_injuries import scrape_injury_list
        return scrape_injury_list(conn, trigger_source=TRIGGER_ADMIN_MANUAL, correlation_id=correlation_id)
    finally:
        conn.close()


def manual_refresh_fixtures_round(round_id: int, correlation_id: str):
    from scraper.scrape_afl_matches import run as scrape_matches
    return scrape_matches(round_id=round_id, trigger_source=TRIGGER_ADMIN_MANUAL, correlation_id=correlation_id)


def manual_refresh_lineups_round(round_id: int, correlation_id: str):
    from scraper.scrape_afl_lineups import scrape_team_lineups
    return scrape_team_lineups(round_number=round_id, trigger_source=TRIGGER_ADMIN_MANUAL, correlation_id=correlation_id)


def manual_refresh_lineups_match(match_id: int, correlation_id: str):
    from scraper.scrape_afl_lineups import scrape_match_lineup
    return scrape_match_lineup(match_id=match_id, trigger_source=TRIGGER_ADMIN_MANUAL, correlation_id=correlation_id)


def manual_refresh_player_stats_match(match_id: int, correlation_id: str):
    from scraper.scrape_afl_player_stats import run_scraper as scrape_player_stats
    return scrape_player_stats(match_id=match_id, once=True, trigger_source=TRIGGER_ADMIN_MANUAL, correlation_id=correlation_id)


def _enqueue(func, *, job_type: str, target_type: str, target_id: int | None = None, round_id: int | None = None, match_id: int | None = None):
    duplicate = _duplicate(job_type, round_id=round_id, match_id=match_id)
    if duplicate:
        return {"status": "already_running", "job_id": duplicate["job_id"], "message": "An equivalent manual job is already queued or running."}
    target = target_type if target_id is None else f"{target_type}_{target_id}"
    jid = _job_id(job_type, target)
    args = [jid] if target_id is None else [target_id, jid]
    add_registered_job(
        scheduler, func,
        trigger=DateTrigger(run_date=datetime.now(timezone.utc)), run_date=datetime.now(timezone.utc),
        args=args, job_id=jid, job_type=job_type, round_id=round_id, match_id=match_id,
        name=f"Admin manual {job_type} {target}", replace_existing=False, trigger_type="admin_manual",
    )
    return {"status": "queued", "job_id": jid, "correlation_id": jid, "trigger_source": TRIGGER_ADMIN_MANUAL, "target_type": target_type, "target_identifier": target_id}


@router.post("/injuries")
def trigger_injuries(payload: ManualTriggerRequest | None = None):
    conn = get_db_connection()
    try:
        _require_tables(conn)
    finally:
        conn.close()
    return _enqueue(manual_refresh_injuries, job_type="injury", target_type="injury_list")


@router.post("/fixtures/round")
def trigger_fixtures_round(payload: ManualTriggerRequest):
    if payload.match_id is not None or payload.round_id is None:
        raise HTTPException(status_code=422, detail="Submit exactly one round identifier")
    conn = get_db_connection()
    try:
        _require_tables(conn); _round_exists(conn, payload.round_id)
    finally:
        conn.close()
    return _enqueue(manual_refresh_fixtures_round, job_type="fixture", target_type="round", target_id=payload.round_id, round_id=payload.round_id)


@router.post("/lineups/round")
def trigger_lineups_round(payload: ManualTriggerRequest):
    if payload.match_id is not None or payload.round_id is None:
        raise HTTPException(status_code=422, detail="Submit exactly one round identifier")
    conn = get_db_connection()
    try:
        _require_tables(conn); _round_exists(conn, payload.round_id)
    finally:
        conn.close()
    return _enqueue(manual_refresh_lineups_round, job_type="lineup", target_type="round", target_id=payload.round_id, round_id=payload.round_id)


@router.post("/lineups/match")
def trigger_lineups_match(payload: ManualTriggerRequest):
    if payload.round_id is not None or payload.match_id is None:
        raise HTTPException(status_code=422, detail="Submit exactly one match identifier")
    conn = get_db_connection()
    try:
        _require_tables(conn); _match_exists(conn, payload.match_id)
    finally:
        conn.close()
    return _enqueue(manual_refresh_lineups_match, job_type="lineup", target_type="match", target_id=payload.match_id, match_id=payload.match_id)


@router.post("/player-stats/match")
def trigger_player_stats_match(payload: ManualTriggerRequest):
    if payload.round_id is not None or payload.match_id is None:
        raise HTTPException(status_code=422, detail="Submit exactly one match identifier")
    conn = get_db_connection()
    try:
        _require_tables(conn); _match_exists(conn, payload.match_id)
    finally:
        conn.close()
    return _enqueue(manual_refresh_player_stats_match, job_type="player_stats", target_type="match", target_id=payload.match_id, match_id=payload.match_id)
