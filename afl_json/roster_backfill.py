"""Manual, round-scoped persistence orchestration for canonical CFS rosters."""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .rosters import MatchRosterCollector, RosterStatus, persist_match_rosters


@dataclass(frozen=True, slots=True)
class RosterBackfillRoundResult:
    round_id: int
    round_number: int
    round_provider_id: str | None
    outcome: str
    rosters_written: int = 0
    selections_written: int = 0
    context_written: int = 0
    unmatched_matches: tuple[str, ...] = ()
    unmatched_teams: tuple[tuple[str, str], ...] = ()
    error: str | None = None


@dataclass(slots=True)
class RosterBackfillResult:
    requested_season: int
    season_id: int
    season_name: str | None
    selection: str
    rounds: list[RosterBackfillRoundResult] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        good = sum(item.outcome == "published" for item in self.rounds)
        if good == len(self.rounds):
            return "success"
        return "partial" if good else "failure"

    def to_dict(self) -> dict:
        # Use the shared operational-domain vocabulary even though this manual
        # multi-round command owns selection and reporting itself.
        from collection.source_policy import OperationalDomain
        unavailable = sum(item.outcome == "unavailable" for item in self.rounds)
        empty = sum(item.outcome == "conservative_empty" for item in self.rounds)
        failed = sum(item.outcome == "failed" for item in self.rounds)
        attention = [item.round_number for item in self.rounds if item.outcome != "published"]
        return {
            "operation": "sync_match_rosters", "domain": OperationalDomain.MATCH_ROSTERS.value,
            "season": self.requested_season,
            "season_id": self.season_id, "season_name": self.season_name,
            "selected_scope": self.selection, "outcome": self.outcome,
            "aggregates": {
                "rounds_selected": len(self.rounds),
                "rounds_published": sum(item.outcome == "published" for item in self.rounds),
                "rounds_unavailable": unavailable, "rounds_conservative_empty": empty,
                "rounds_failed": failed,
                "roster_rows_written": sum(item.rosters_written for item in self.rounds),
                "selection_rows_written": sum(item.selections_written for item in self.rounds),
                "context_rows_written": sum(item.context_written for item in self.rounds),
                "unmatched_matches": sum(len(item.unmatched_matches) for item in self.rounds),
                "unmatched_teams": sum(len(item.unmatched_teams) for item in self.rounds),
                "rounds_requiring_attention": attention,
            },
            "rounds": [asdict(item) for item in self.rounds],
        }


def sync_match_rosters(client, conn: sqlite3.Connection, *, year: int,
                       round_number: int | None = None,
                       round_from: int | None = None, round_to: int | None = None,
                       competition_code: str, competition_provider_id: str,
                       raw_directory=None) -> RosterBackfillResult:
    """Persist each selected canonical round independently, including concluded rounds."""
    seasons = conn.execute(
        "SELECT s.afl_id,s.name FROM afl_seasons s JOIN afl_competitions c "
        "ON c.afl_id=s.competition_id WHERE s.year=? AND (c.code=? OR c.provider_id=?)",
        (year, competition_code, competition_provider_id),
    ).fetchall()
    if not seasons:
        raise ValueError(f"canonical AFL season {year} is not persisted")
    if len(seasons) != 1:
        raise ValueError(f"canonical AFL season {year} is ambiguous")
    season_id, season_name = seasons[0]
    clauses, params = ["season_id=?"], [season_id]
    if round_number is not None:
        clauses.append("round_number=?"); params.append(round_number)
        selection = f"round {round_number}"
    elif round_from is not None:
        clauses.append("round_number BETWEEN ? AND ?"); params.extend((round_from, round_to))
        selection = f"rounds {round_from}-{round_to}"
    else:
        selection = "whole season"
    rows = conn.execute(
        "SELECT round_id,round_number,provider_id FROM rounds WHERE " +
        " AND ".join(clauses) + " ORDER BY round_number,round_id", params,
    ).fetchall()
    if not rows:
        raise ValueError(f"season {year} has no canonical rounds matching {selection}")
    if round_number is not None and len(rows) != 1:
        raise ValueError(f"season {year} round {round_number} is not uniquely selectable")
    if round_from is not None:
        present = {row[1] for row in rows}
        missing = [number for number in range(round_from, round_to + 1) if number not in present]
        if missing:
            raise ValueError(f"season {year} is missing canonical rounds: {', '.join(map(str, missing))}")

    result = RosterBackfillResult(year, season_id, season_name, selection)
    collector = MatchRosterCollector(client, raw_directory=raw_directory)
    for round_id, number, provider_id in rows:
        if not provider_id or not str(provider_id).startswith("CD_R"):
            result.rounds.append(RosterBackfillRoundResult(
                round_id, number, provider_id, "failed", error="missing Champion Data round provider ID"))
            continue
        try:
            collected = collector.collect(provider_id)
            if collected.status is RosterStatus.UNAVAILABLE:
                item = RosterBackfillRoundResult(round_id, number, provider_id, "unavailable")
            elif collected.status is RosterStatus.EMPTY:
                item = RosterBackfillRoundResult(round_id, number, provider_id, "conservative_empty")
            else:
                summary = persist_match_rosters(
                    conn, collected, observed_at=datetime.now(timezone.utc).isoformat())
                conn.commit()
                outcome = "published" if not summary.unmatched_matches else "failed"
                item = RosterBackfillRoundResult(
                    round_id, number, provider_id, outcome, summary.rosters_written,
                    summary.selections_written, summary.context_written,
                    summary.unmatched_matches, summary.unmatched_teams,
                    "unmatched canonical matches" if summary.unmatched_matches else None)
            result.rounds.append(item)
        except Exception as exc:  # round isolation is the operator contract
            conn.rollback()
            result.rounds.append(RosterBackfillRoundResult(
                round_id, number, provider_id, "failed", error=f"{type(exc).__name__}: {exc}"))
    return result
