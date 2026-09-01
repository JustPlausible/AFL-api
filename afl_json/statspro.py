"""AFL StatsPro contracts, normalisation and transactional persistence.

StatsPro is published-summary authority.  It is deliberately separate from the
live, match-scoped ``cfs_player_stats`` pipeline.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .client import AflJsonClient, AflJsonInvalidResponse

SOURCE = "AFL_STATSPRO"
SEASON_TOTAL = "SEASON_TOTAL"
LEAGUE_ROUND_TOTAL = "LEAGUE_ROUND_TOTAL"
FULL_SEASON = "full_season"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StatsProPlayerSummary:
    player_provider_id: str
    team_provider_id: str | None
    games_played: int
    totals: dict[str, Any]
    averages: dict[str, Any]
    source_updated_at: str | None = None
    opponent_provider_id: str | None = None
    result_context: str | None = None


@dataclass(frozen=True, slots=True)
class StatsProCollectionReport:
    source_context: str
    provider_id: str
    players_returned: int
    players_resolved: int
    players_unresolved: int
    zero_game_players: int
    inserted: int
    updated: int
    unchanged: int
    collected_at: str


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AflJsonInvalidResponse(f"StatsPro {label} is not an object", endpoint="statspro")
    return dict(value)


def _records(payload: Any) -> list[Any]:
    body = _object(payload, "response")
    # Both envelopes have been observed across AFL API generations.  Accepting
    # only named arrays prevents metadata/error objects becoming an empty snapshot.
    for key in ("players", "playerStats", "playersStats", "data"):
        value = body.get(key)
        if isinstance(value, list):
            if not value:
                raise AflJsonInvalidResponse("StatsPro response contains no players", endpoint="statspro")
            return value
    raise AflJsonInvalidResponse("StatsPro response has no recognised player array", endpoint="statspro")


def _provider_id(entry: Mapping[str, Any], field: str, nested: str) -> str | None:
    value = entry.get(field)
    if value is None and isinstance(entry.get(nested), dict):
        value = entry[nested].get("providerId") or entry[nested].get("id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def normalise_statspro(payload: Any, *, context: str) -> list[StatsProPlayerSummary]:
    if context not in {SEASON_TOTAL, LEAGUE_ROUND_TOTAL}:
        raise ValueError("unsupported StatsPro context")
    result: list[StatsProPlayerSummary] = []
    seen: set[str] = set()
    for index, raw in enumerate(_records(payload)):
        entry = _object(raw, f"player[{index}]")
        player_id = _provider_id(entry, "playerProviderId", "player") or _provider_id(entry, "playerId", "player")
        if not player_id or not player_id.startswith("CD_I"):
            raise AflJsonInvalidResponse(f"StatsPro player[{index}] has no valid provider ID", endpoint="statspro")
        if player_id in seen:
            raise AflJsonInvalidResponse("StatsPro response contains duplicate player IDs", endpoint="statspro")
        seen.add(player_id)
        if context == SEASON_TOTAL:
            missing = [field for field in ("gamesPlayed", "totals", "averages") if field not in entry]
            if missing:
                raise AflJsonInvalidResponse(
                    f"StatsPro player[{index}] is missing required {', '.join(missing)}",
                    endpoint="statspro",
                )
        totals = _object(entry.get("totals", {}), f"player[{index}].totals")
        averages = _object(entry.get("averages", {}), f"player[{index}].averages")
        if context == SEASON_TOTAL and (not totals or not averages):
            raise AflJsonInvalidResponse(
                f"StatsPro player[{index}] has an empty authoritative summary",
                endpoint="statspro",
            )
        games = entry.get("gamesPlayed", totals.get("gamesPlayed", entry.get("games", 0)))
        if isinstance(games, bool) or not isinstance(games, int) or games < 0:
            raise AflJsonInvalidResponse(f"StatsPro player[{index}] has invalid gamesPlayed", endpoint="statspro")
        result.append(StatsProPlayerSummary(
            player_provider_id=player_id,
            team_provider_id=_provider_id(entry, "teamProviderId", "team"),
            games_played=games, totals=totals, averages=averages,
            source_updated_at=entry.get("updatedAt") if isinstance(entry.get("updatedAt"), str) else None,
            opponent_provider_id=_provider_id(entry, "opponentProviderId", "opponent"),
            result_context=entry.get("result") if isinstance(entry.get("result"), str) else None,
        ))
    return result


class StatsProCollector:
    def __init__(self, client: AflJsonClient, *, clock: Callable[[], datetime] | None = None):
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch_season(self, season_provider_id: str) -> tuple[list[StatsProPlayerSummary], str]:
        response = self.client.get("statspro_season_total",
            path_parameters={"season_provider_id": season_provider_id},
            params={"includeBenchmarks": "false", "playerNameLike": "", "playerPosition": "", "teamId": ""})
        return normalise_statspro(response.data, context=SEASON_TOTAL), self._now()

    def fetch_round(self, round_provider_id: str) -> tuple[list[StatsProPlayerSummary], str]:
        response = self.client.get("statspro_round_total",
            path_parameters={"round_provider_id": round_provider_id}, params={"teamId": ""})
        return normalise_statspro(response.data, context=LEAGUE_ROUND_TOTAL), self._now()

    def _now(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat()


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _resolve(conn: sqlite3.Connection, table: str, provider_id: str | None) -> int | None:
    if provider_id is None:
        return None
    if table == "player_provider_ids":
        row = conn.execute(
            "SELECT player_id FROM player_provider_ids "
            "WHERE provider = 'champion_data' AND provider_player_id = ?",
            (provider_id,),
        ).fetchone()
    else:
        row = conn.execute("SELECT afl_id FROM afl_teams WHERE provider_id = ?", (provider_id,)).fetchone()
    return row[0] if row else None


def persist_season(conn: sqlite3.Connection, records: list[StatsProPlayerSummary], *,
                   season_id: int, season_provider_id: str, collected_at: str) -> StatsProCollectionReport:
    if not records:
        raise ValueError("refusing to persist an empty StatsPro season snapshot")
    inserted = updated = unchanged = resolved = 0
    # Caller connection remains usable; the savepoint makes any row failure atomic.
    conn.execute("SAVEPOINT statspro_season_refresh")
    try:
        for record in records:
            player_id = _resolve(conn, "player_provider_ids", record.player_provider_id)
            team_id = _resolve(conn, "afl_teams", record.team_provider_id)
            resolved += player_id is not None
            material = (player_id, record.team_provider_id, team_id, record.games_played,
                        _json(record.totals), _json(record.averages), record.source_updated_at)
            old = conn.execute("SELECT canonical_player_id, team_provider_id, team_id, games_played, published_totals, published_averages, source_updated_at FROM statspro_player_season_summaries WHERE season_id=? AND player_provider_id=? AND source_context=?",
                               (season_id, record.player_provider_id, SEASON_TOTAL)).fetchone()
            if old is None:
                conn.execute("INSERT INTO statspro_player_season_summaries(canonical_player_id,player_provider_id,season_id,season_provider_id,team_id,team_provider_id,source,source_context,scope,games_played,published_totals,published_averages,source_updated_at,collected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (player_id, record.player_provider_id, season_id, season_provider_id, team_id,
                     record.team_provider_id, SOURCE, SEASON_TOTAL, FULL_SEASON, record.games_played,
                     material[4], material[5], record.source_updated_at, collected_at))
                inserted += 1
            elif tuple(old) == material:
                unchanged += 1
            else:
                conn.execute("UPDATE statspro_player_season_summaries SET canonical_player_id=?,team_provider_id=?,team_id=?,games_played=?,published_totals=?,published_averages=?,source_updated_at=?,collected_at=?,season_provider_id=? WHERE season_id=? AND player_provider_id=? AND source_context=?",
                    (*material, collected_at, season_provider_id, season_id, record.player_provider_id, SEASON_TOTAL))
                updated += 1
        conn.execute("RELEASE SAVEPOINT statspro_season_refresh")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT statspro_season_refresh")
        conn.execute("RELEASE SAVEPOINT statspro_season_refresh")
        raise
    report = StatsProCollectionReport(SEASON_TOTAL, season_provider_id, len(records), resolved,
        len(records)-resolved, sum(r.games_played == 0 for r in records), inserted, updated, unchanged, collected_at)
    logger.info("StatsPro collection source=%s context=%s provider_id=%s records=%d resolved=%d unresolved=%d zero_game=%d inserted=%d updated=%d unchanged=%d",
                SOURCE, SEASON_TOTAL, season_provider_id, len(records), resolved, len(records)-resolved,
                report.zero_game_players, inserted, updated, unchanged)
    return report


def persist_round(conn: sqlite3.Connection, records: list[StatsProPlayerSummary], *, season_id: int,
                  round_id: int, round_provider_id: str, collected_at: str) -> StatsProCollectionReport:
    if not records:
        raise ValueError("refusing to persist an empty StatsPro round snapshot")
    inserted = updated = unchanged = resolved = 0
    conn.execute("SAVEPOINT statspro_round_refresh")
    try:
        for record in records:
            player_id = _resolve(conn, "player_provider_ids", record.player_provider_id)
            team_id = _resolve(conn, "afl_teams", record.team_provider_id)
            resolved += player_id is not None
            values = (player_id, record.team_provider_id, team_id, record.opponent_provider_id,
                      record.result_context, _json(record.totals))
            old = conn.execute("SELECT canonical_player_id,team_provider_id,team_id,opponent_provider_id,result_context,published_totals FROM statspro_player_round_summaries WHERE round_id=? AND player_provider_id=? AND source_context=?", (round_id,record.player_provider_id,LEAGUE_ROUND_TOTAL)).fetchone()
            if old is None:
                conn.execute("INSERT INTO statspro_player_round_summaries(canonical_player_id,player_provider_id,season_id,round_id,round_provider_id,team_id,team_provider_id,opponent_provider_id,result_context,source,source_context,published_totals,collected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (player_id,record.player_provider_id,season_id,round_id,round_provider_id,team_id,record.team_provider_id,record.opponent_provider_id,record.result_context,SOURCE,LEAGUE_ROUND_TOTAL,values[-1],collected_at)); inserted += 1
            elif tuple(old) == values:
                unchanged += 1
            else:
                conn.execute("UPDATE statspro_player_round_summaries SET canonical_player_id=?,team_provider_id=?,team_id=?,opponent_provider_id=?,result_context=?,published_totals=?,collected_at=?,round_provider_id=? WHERE round_id=? AND player_provider_id=? AND source_context=?", (*values,collected_at,round_provider_id,round_id,record.player_provider_id,LEAGUE_ROUND_TOTAL)); updated += 1
        conn.execute("RELEASE SAVEPOINT statspro_round_refresh")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT statspro_round_refresh"); conn.execute("RELEASE SAVEPOINT statspro_round_refresh"); raise
    return StatsProCollectionReport(LEAGUE_ROUND_TOTAL,round_provider_id,len(records),resolved,len(records)-resolved,sum(r.games_played==0 for r in records),inserted,updated,unchanged,collected_at)
