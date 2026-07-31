"""Transactional persistence for normalized canonical player collections."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .collectors import PlayerCollectionResult


class PlayerIdentityConflict(RuntimeError):
    """Provider identifiers contradict an already persisted crosswalk."""


@dataclass(frozen=True, slots=True)
class PlayerPersistenceSummary:
    status: str
    records_read: int
    players_inserted: int
    players_updated: int
    mappings_inserted: int
    associations_inserted: int
    associations_updated: int
    unchanged: int
    missing_team_links: int

    @property
    def rows_written(self) -> int:
        return (self.players_inserted + self.players_updated + self.mappings_inserted
                + self.associations_inserted + self.associations_updated)


def _mapping(conn: sqlite3.Connection, provider: str, value: object) -> int | None:
    if value is None:
        return None
    row = conn.execute(
        "SELECT player_id FROM player_provider_ids WHERE provider=? AND provider_player_id=?",
        (provider, str(value)),
    ).fetchone()
    return row[0] if row else None


def persist_player_seasons(conn: sqlite3.Connection, result: PlayerCollectionResult,
                           *, provider_season_id: str) -> PlayerPersistenceSummary:
    """Persist one non-destructive player-season snapshot as a single transaction."""
    if result.status == "unavailable":
        return PlayerPersistenceSummary("unavailable", 0, 0, 0, 0, 0, 0, 0, 0)
    season = conn.execute(
        "SELECT afl_id FROM afl_seasons WHERE provider_id=?", (provider_season_id,)
    ).fetchone()
    if season is None:
        raise ValueError(f"competition season {provider_season_id!r} is not persisted")
    season_id = season[0]
    now = datetime.now(timezone.utc).isoformat()
    counts = dict(players_inserted=0, players_updated=0, mappings_inserted=0,
                  associations_inserted=0, associations_updated=0, unchanged=0,
                  missing_team_links=0)
    associations = {item["champion_data_player_id"]: item for item in result.player_seasons}
    conn.execute("BEGIN")
    try:
        for identity in result.players:
            cd_id = identity.get("champion_data_player_id")
            afl_id = identity.get("afl_player_id")
            resolved = {value for value in (
                _mapping(conn, "champion_data", cd_id), _mapping(conn, "afl", afl_id)
            ) if value is not None}
            if len(resolved) > 1:
                raise PlayerIdentityConflict(
                    f"conflicting crosswalk: Champion Data {cd_id!r} and AFL {afl_id!r} "
                    "already belong to different canonical players"
                )
            player_id = next(iter(resolved), None)
            if player_id is None:
                cursor = conn.execute(
                    "INSERT INTO canonical_players(display_name,given_name,family_name,created_at,updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (identity.get("name"), identity.get("given_name"),
                     identity.get("family_name"), now, now),
                )
                player_id = cursor.lastrowid
                counts["players_inserted"] += 1
            else:
                existing = conn.execute(
                    "SELECT display_name,given_name,family_name FROM canonical_players WHERE id=?",
                    (player_id,),
                ).fetchone()
                desired = tuple(candidate if candidate is not None else existing[index]
                                for index, candidate in enumerate((
                                    identity.get("name"), identity.get("given_name"),
                                    identity.get("family_name"),
                                )))
                if tuple(existing) != desired:
                    conn.execute(
                        "UPDATE canonical_players SET display_name=?,given_name=?,family_name=?,updated_at=? WHERE id=?",
                        (*desired, now, player_id),
                    )
                    counts["players_updated"] += 1
            for provider, provider_id in (("champion_data", cd_id), ("afl", afl_id)):
                if provider_id is None:
                    continue
                current = _mapping(conn, provider, provider_id)
                if current is not None and current != player_id:
                    raise PlayerIdentityConflict(
                        f"{provider} identifier {provider_id!r} is already assigned to another canonical player"
                    )
                owned = conn.execute(
                    "SELECT provider_player_id FROM player_provider_ids WHERE player_id=? AND provider=?",
                    (player_id, provider),
                ).fetchone()
                if owned and owned[0] != str(provider_id):
                    raise PlayerIdentityConflict(
                        f"canonical player {player_id} already has a different {provider} identifier"
                    )
                if current is None:
                    conn.execute(
                        "INSERT INTO player_provider_ids(player_id,provider,provider_player_id,created_at,updated_at) "
                        "VALUES (?,?,?,?,?)", (player_id, provider, str(provider_id), now, now),
                    )
                    counts["mappings_inserted"] += 1
            association = associations[cd_id]
            team_id = None
            team_provider_id = association.get("team_id")
            if team_provider_id:
                team = conn.execute(
                    "SELECT ts.team_id FROM afl_team_seasons ts JOIN afl_teams t ON t.afl_id=ts.team_id "
                    "WHERE ts.competition_season_id=? AND t.provider_id=?",
                    (season_id, team_provider_id),
                ).fetchone()
                if team:
                    team_id = team[0]
                else:
                    counts["missing_team_links"] += 1
            values = (team_id, "champion_data", association.get("jumper_number"),
                      association.get("listed_position"), association.get("photo_url"),
                      json.dumps(association.get("source"), sort_keys=True, separators=(",", ":")))
            existing = conn.execute(
                "SELECT team_id,source_provider,jumper_number,listed_position,photo_url,source_json "
                "FROM competition_season_players WHERE player_id=? AND competition_season_id=?",
                (player_id, season_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO competition_season_players(player_id,competition_season_id,team_id,"
                    "source_provider,jumper_number,listed_position,photo_url,source_json,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", (player_id, season_id, *values, now, now),
                )
                counts["associations_inserted"] += 1
            elif tuple(existing) == values:
                counts["unchanged"] += 1
            else:
                conn.execute(
                    "UPDATE competition_season_players SET team_id=?,source_provider=?,jumper_number=?,"
                    "listed_position=?,photo_url=?,source_json=?,updated_at=? "
                    "WHERE player_id=? AND competition_season_id=?",
                    (*values, now, player_id, season_id),
                )
                counts["associations_updated"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return PlayerPersistenceSummary(result.status, len(result.players), **counts)
