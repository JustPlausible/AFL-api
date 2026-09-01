"""Offline, deterministic Home & Away summaries from canonical CFS facts."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from .match_data_exceptions import active_stats_exception
from .match_status import normalise_match_status
from .season_report import authoritative_stats_finality_for_match

SCOPE = "home_and_away"
SOURCE = "DERIVED_MATCH_STATS"
POPULATION_SOURCE = "competition_season_players"
ADDITIVE_FIELDS = ("goals", "behinds", "kicks", "handballs", "disposals", "marks", "tackles", "hitouts")
UNSUPPORTED_FIELDS = (
    "disposalEfficiency", "kickEfficiency", "contestedPossessionRate",
    "hitoutWinPercentage", "hitoutToAdvantageRate", "contestOffWinsPercentage",
    "contestDefLossPercentage", "timeOnGroundPercentage", "ratingPoints", "ranking",
)


class SummaryNotReady(ValueError):
    """Finalized facts cannot be produced without complete canonical evidence."""


@dataclass(frozen=True)
class BuildReport:
    season_id: int
    scope: str
    matches_selected: int
    authoritative_snapshots: int
    reviewed_exceptions: int
    population: int
    players_with_games: int
    zero_game_players: int
    inserted: int
    updated: int
    unchanged: int
    unsupported_fields: tuple[str, ...]
    source_max_updated_at: str | None
    status: str = "FINALIZED"


def select_home_and_away_matches(conn: sqlite3.Connection, season_id: int):
    """Select by persisted competition semantics, never number or display name."""
    conn.row_factory = sqlite3.Row
    unclassified = conn.execute(
        "SELECT COUNT(*) FROM rounds WHERE season_id=? AND competition_phase IS NULL",
        (season_id,),
    ).fetchone()[0]
    if unclassified:
        raise SummaryNotReady(f"{unclassified} rounds lack canonical competition_phase classification")
    return conn.execute(
        "SELECT m.* FROM matches m JOIN rounds r ON r.round_id=m.round_id "
        "WHERE m.season_id=? AND r.competition_phase='HOME_AND_AWAY' ORDER BY m.match_id",
        (season_id,),
    ).fetchall()


def _required_number(value, *, match_provider_id: str, player_provider_id: str,
                     field: str) -> Decimal:
    """Return a finite source number; an appearance's missing fact is not zero."""
    if value is None or isinstance(value, bool):
        raise SummaryNotReady(
            f"authoritative player fact has invalid {field}: match={match_provider_id} "
            f"player={player_provider_id} value={value!r}"
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SummaryNotReady(
            f"authoritative player fact has invalid {field}: match={match_provider_id} "
            f"player={player_provider_id} value={value!r}"
        ) from None
    if not number.is_finite():
        raise SummaryNotReady(
            f"authoritative player fact has invalid {field}: match={match_provider_id} "
            f"player={player_provider_id} value={value!r}"
        )
    return number


def build_home_and_away_player_summaries(conn: sqlite3.Connection, season_id: int, *,
        finalize: bool = True, clock: Callable[[], datetime] | None = None) -> BuildReport:
    """Validate, calculate, and atomically replace one finalized local summary."""
    conn.row_factory = sqlite3.Row
    if conn.execute("SELECT 1 FROM afl_seasons WHERE afl_id=?", (season_id,)).fetchone() is None:
        raise ValueError("season not found")
    matches = select_home_and_away_matches(conn, season_id)
    included, reviewed, missing = [], 0, []
    for match in matches:
        if normalise_match_status(match["status"]) != "CONCLUDED":
            if finalize:
                missing.append(match["match_id"])
            continue
        finality = authoritative_stats_finality_for_match(conn, match["match_provider_id"])
        if finality.has_satisfactory_concluded_coverage:
            included.append(match["match_provider_id"])
        elif active_stats_exception(conn, match["match_id"]) is not None:
            reviewed += 1
        else:
            missing.append(match["match_id"])
    if finalize and missing:
        raise SummaryNotReady(
            f"{len(missing)} concluded Home & Away matches are missing authoritative player "
            f"statistics and have no active stats_not_expected review: {missing}"
        )

    population = conn.execute(
        "SELECT player_id,team_id FROM competition_season_players "
        "WHERE competition_season_id=? ORDER BY player_id", (season_id,),
    ).fetchall()
    totals = {row["player_id"]: {field: Decimal(0) for field in ADDITIVE_FIELDS} for row in population}
    games = {row["player_id"]: 0 for row in population}
    source_max = None
    if included:
        placeholders = ",".join("?" for _ in included)
        facts = conn.execute(
            f"SELECT match_provider_id,champion_data_player_id,canonical_player_id,collected_at,"
            f"{','.join(ADDITIVE_FIELDS)} "
            f"FROM cfs_player_stats WHERE snapshot_authority=2 AND match_provider_id IN ({placeholders})",
            included,
        ).fetchall()
        appearances: dict[int, int] = {}
        for fact in facts:
            player_id = fact["canonical_player_id"]
            if player_id is None:
                raise SummaryNotReady(
                    "authoritative player fact has unresolved canonical identity: "
                    f"match={fact['match_provider_id']} player={fact['champion_data_player_id']}"
                )
            if player_id not in totals:
                raise SummaryNotReady(
                    "authoritative player fact is absent from season population: "
                    f"match={fact['match_provider_id']} player={fact['champion_data_player_id']} "
                    f"canonical_player_id={player_id} season_id={season_id}"
                )
            appearances[player_id] = appearances.get(player_id, 0) + 1
            for field in ADDITIVE_FIELDS:
                totals[player_id][field] += _required_number(
                    fact[field], match_provider_id=fact["match_provider_id"],
                    player_provider_id=fact["champion_data_player_id"], field=field,
                )
            source_max = max(filter(None, (source_max, fact["collected_at"])), default=None)
        games.update(appearances)

    now = (clock or (lambda: datetime.now(timezone.utc)))().isoformat()
    existing = {row["canonical_player_id"]: row for row in conn.execute(
        "SELECT * FROM derived_player_season_summaries WHERE season_id=? AND scope=?",
        (season_id, SCOPE),
    )}
    prepared = []
    inserted = updated = unchanged = 0
    for member in population:
        pid = member["player_id"]
        serial_totals = {key: int(value) if value == value.to_integral() else float(value)
                         for key, value in totals[pid].items()}
        attempts = totals[pid]["goals"] + totals[pid]["behinds"]
        rates = {"goal_accuracy": (float(totals[pid]["goals"] * 100 / attempts)
                                    if attempts else None)}
        encoded_totals = json.dumps(serial_totals, sort_keys=True, separators=(",", ":"))
        encoded_rates = json.dumps(rates, sort_keys=True, separators=(",", ":"))
        old = existing.get(pid)
        comparable = (member["team_id"], games[pid], encoded_totals, encoded_rates, source_max)
        old_comparable = ((old["team_id"], old["games_played"], old["totals"],
                           old["derived_rates"], old["source_max_updated_at"]) if old else None)
        if old is None: inserted += 1
        elif comparable == old_comparable: unchanged += 1
        else: updated += 1
        prepared.append((season_id,pid,member["team_id"],SCOPE,SOURCE,POPULATION_SOURCE,
                         games[pid],encoded_totals,encoded_rates,now,source_max,int(finalize)))
    # Calculation and all readiness checks precede the transaction's destructive step.
    with conn:
        conn.execute("DELETE FROM derived_player_season_summaries WHERE season_id=? AND scope=?",
                     (season_id,SCOPE))
        conn.executemany("INSERT INTO derived_player_season_summaries "
            "(season_id,canonical_player_id,team_id,scope,source,population_source,games_played,"
            "totals,derived_rates,built_at,source_max_updated_at,finalized) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            prepared)
    with_games = sum(value > 0 for value in games.values())
    return BuildReport(season_id,SCOPE,len(matches),len(included),reviewed,len(population),with_games,
                       len(population)-with_games,inserted,updated,unchanged,UNSUPPORTED_FIELDS,source_max)
