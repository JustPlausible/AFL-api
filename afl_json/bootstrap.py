"""Idempotent persistence for a collected public AFL season hierarchy."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from utils.time_format import normalize_utc_iso

from .collectors import CollectionResult


@dataclass(frozen=True, slots=True)
class BootstrapSummary:
    records_read: int
    inserted: int
    updated: int
    unchanged: int
    failed: int = 0


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _team_id(value: Any) -> int | None:
    if not isinstance(value, Mapping):
        return None
    team = value.get("team") if isinstance(value.get("team"), Mapping) else value
    candidate = team.get("id") if isinstance(team, Mapping) else None
    return candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else None


def _display(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    item = value.get("team") if isinstance(value.get("team"), Mapping) else value
    if not isinstance(item, Mapping):
        return None
    return next((item.get(key) for key in ("abbreviation", "name", "displayName")
                 if isinstance(item.get(key), str)), None)


def _score(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        for key in ("totalScore", "score", "points"):
            candidate = value.get(key)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return candidate
    return None


def _upsert(conn: sqlite3.Connection, table: str, key: str, row: dict[str, Any]) -> str:
    existing = conn.execute(
        f"SELECT {', '.join(row)} FROM {table} WHERE {key}=?", (row[key],)
    ).fetchone()
    values = tuple(row.values())
    if existing is None:
        conn.execute(
            f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", values
        )
        return "inserted"
    # updated_at/scraped_at are observation timestamps, not upstream changes.
    ignored = {"updated_at", "scraped_at"}
    comparable = [column for column in row if column not in ignored]
    positions = {column: index for index, column in enumerate(row)}
    if all(existing[positions[column]] == row[column] for column in comparable):
        return "unchanged"
    assignments = ", ".join(f"{column}=?" for column in row if column != key)
    conn.execute(f"UPDATE {table} SET {assignments} WHERE {key}=?",
                 tuple(row[column] for column in row if column != key) + (row[key],))
    return "updated"


def persist_afl_metadata(conn: sqlite3.Connection, result: CollectionResult) -> BootstrapSummary:
    """Atomically insert or update one fully collected season."""
    now = datetime.now(timezone.utc).isoformat()
    outcomes = {"inserted": 0, "updated": 0, "unchanged": 0}

    def save(table: str, key: str, row: dict[str, Any]) -> None:
        outcomes[_upsert(conn, table, key, row)] += 1

    competition = result.competition
    season = result.season
    try:
        conn.execute("BEGIN")
        save("afl_competitions", "afl_id", {
            "afl_id": competition["afl_id"], "provider_id": competition.get("provider_id"),
            "code": competition.get("code"), "name": competition.get("name"),
            "metadata_json": _json(competition.get("metadata")),
            "source_json": _json(competition.get("source")), "updated_at": now,
        })
        # The canonical current-season marker is decided once, independently of
        # any single upstream season["current"] field (which may be absent),
        # by CollectionResult.current_season_afl_id -- see collectors.is_current_season.
        is_current = 1 if (result.current_season_afl_id is not None
                           and result.current_season_afl_id == season["afl_id"]) else 0
        save("afl_seasons", "afl_id", {
            "afl_id": season["afl_id"], "provider_id": season.get("provider_id"),
            "competition_id": competition["afl_id"], "name": season.get("name"),
            "short_name": season.get("short_name"), "year": season.get("year"),
            "is_current": is_current,
            "current_round_number": season.get("current_round_number"),
            "start_time": season.get("start_time"), "end_time": season.get("end_time"),
            "metadata_json": _json(season.get("metadata")), "source_json": _json(season.get("source")),
            "updated_at": now,
        })
        if result.current_season_afl_id is not None:
            # Enforce the competition-wide invariant: at most one persisted
            # season is current. This also clears a previous current season
            # when the current season advances, and repairs any pre-existing
            # NULL/stale markers left by earlier persistence.
            conn.execute(
                "UPDATE afl_seasons SET is_current=0, updated_at=? "
                "WHERE competition_id=? AND afl_id!=? AND is_current IS NOT 0",
                (now, competition["afl_id"], result.current_season_afl_id),
            )
        for team in result.teams:
            save("afl_teams", "afl_id", {
                "afl_id": team["afl_id"], "provider_id": team.get("provider_id"),
                "name": team.get("name"),
                "abbreviation": team.get("abbreviation"), "nickname": team.get("nickname"),
                "display_name": team.get("displayName"), "short_name": team.get("shortName"),
                "team_type": team.get("team_type"), "metadata_json": _json(team.get("metadata")),
                "club_json": _json(team.get("club")), "source_json": _json(team.get("source")),
                "updated_at": now,
            })
            conn.execute(
                "INSERT INTO afl_team_seasons(competition_season_id,team_id,created_at,updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(competition_season_id,team_id) "
                "DO UPDATE SET updated_at=excluded.updated_at",
                (season["afl_id"], team["afl_id"], now, now),
            )
        for round_record in result.rounds:
            save("rounds", "round_id", {
                "round_id": round_record["afl_id"], "round_label": round_record.get("name"),
                "season_id": season["afl_id"], "competition_id": competition["afl_id"],
                "scraped_at": now, "provider_id": round_record.get("provider_id"),
                "round_number": round_record.get("round_number"),
                "competition_phase": round_record.get("competition_phase"),
                "abbreviation": round_record.get("abbreviation"),
                "start_time": round_record.get("start_time"), "end_time": round_record.get("end_time"),
                "byes_json": _json(round_record.get("byes")),
                "metadata_json": _json(round_record.get("metadata")),
                "source_json": _json(round_record.get("source")), "updated_at": now,
            })
        for match in result.matches:
            round_value = match.get("round")
            round_id = round_value.get("id") if isinstance(round_value, Mapping) else None
            venue = match.get("venue")
            venue_name = next((venue.get(k) for k in ("name", "displayName") if isinstance(venue, Mapping)
                               and isinstance(venue.get(k), str)), None)
            save("matches", "match_id", {
                "match_id": match["afl_id"], "match_provider_id": match.get("provider_id"),
                "round_id": round_id, "home_team": _display(match.get("home")),
                "away_team": _display(match.get("away")), "venue": venue_name,
                "status": match.get("status"),
                "start_time_utc": normalize_utc_iso(match.get("utc_start_time")),
                "score_home": _score(match.get("home_score")), "score_away": _score(match.get("away_score")),
                "scraped_at": now, "season_id": season["afl_id"],
                "home_team_id": _team_id(match.get("home")), "away_team_id": _team_id(match.get("away")),
                "home_json": _json(match.get("home")), "away_json": _json(match.get("away")),
                "venue_json": _json(venue), "metadata_json": _json(match.get("metadata")),
                "source_json": _json(match.get("source")), "updated_at": now,
            })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    read = 2 + len(result.teams) + len(result.rounds) + len(result.matches)
    return BootstrapSummary(read, outcomes["inserted"], outcomes["updated"], outcomes["unchanged"])
