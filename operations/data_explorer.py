"""Read-only Admin AFL Data Explorer reporting (Issue #226).

Provides the human-readable Season -> Round -> Match -> Player inspection
hierarchy for the Admin UI. Like ``operations/dashboard.py`` (Issue #225),
this module never mutates anything, never opens its own connection (the
caller passes an already-open read-only connection), and never invents a
second, independent notion of completeness:

* Season-level completeness is exactly :class:`afl_json.season_report.ReportStatus`
  from :class:`~afl_json.season_report.SeasonCompletenessReporter` -- the same
  report already shown on the Admin Season Review page.
* Round- and match-level "does this concern this match" state is the same
  :class:`~afl_json.season_report.Finding` set from that report, filtered by
  ``match_id`` and reduced with ``operations.dashboard.state_from_findings`` --
  the identical severity/HealthState decision the operations dashboard uses.
* Per-match dataset *presence* (rosters, player statistics, commentary,
  interchange) reuses ``operations.dashboard.dataset_presence_state``, the
  same lifecycle-aware rule already applied at round granularity by the
  operations dashboard, and ``afl_json.season_report.evaluate_authoritative_stats_finality``
  for player-statistics finality specifically.
* Row projection (team names, player display names, provider-ID crosswalks,
  season memberships, canonical rounds/byes) reuses the private projection
  helpers already written for the versioned consumer API
  (``api/routes_v1.py``) rather than re-deriving them here.
* Roster/commentary/interchange row retrieval reuses the exact repository
  functions the consumer API itself calls (``afl_json.rosters``,
  ``afl_json.match_commentary``, ``afl_json.match_interchange``).

Every query here is scoped to one season, round, match or player -- never a
full-table scan -- consistent with this being a page-rendering reporter, not
a bulk export.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from afl_json.match_commentary import event_rows as commentary_event_rows
from afl_json.match_interchange import current_state_rows as interchange_current_state_rows
from afl_json.match_status import normalise_match_status
from afl_json.player_stats import CANONICAL_STAT_FIELDS
from afl_json.rosters import current_roster_context, current_roster_selections, current_roster_teams
from afl_json.season_report import (
    SeasonCompletenessReporter,
    authoritative_stats_finality_for_match,
    list_persisted_afl_seasons,
)

# Reuse the exact projection helpers the versioned consumer API already uses
# (Issue #226 requirement: do not recreate data-access/projection logic).
from api.routes_v1 import (
    _current_team as api_current_team,
    _display_name as api_display_name,
    _identifiers as api_identifiers,
    _round_from_row as api_round_from_row,
    _season_memberships as api_season_memberships,
    _team_projection as api_team_projection,
)
from operations.dashboard import (
    DEFAULT_COMPETITION_CODE,
    DEFAULT_COMPETITION_PROVIDER_ID,
    HealthState,
    dataset_presence_state,
    state_from_findings,
)

COMMENTARY_PREVIEW_LIMIT = 20
PLAYER_RECENT_MATCH_LIMIT = 15
_CHECKPOINT_ORDER = ("BASELINE", "QT", "HT", "3QT", "FT", "CONCLUDED")

# Worst-first precedence for rolling several dataset states into one compact
# badge (mirrors the explicit-precedence style of
# ``OperationsDashboardReporter._overall_state``, generalised to one match).
_STATE_PRECEDENCE = (
    HealthState.FAILED, HealthState.MISSING, HealthState.ATTENTION, HealthState.PARTIAL,
    HealthState.STALE, HealthState.UNKNOWN, HealthState.UPCOMING, HealthState.HEALTHY,
)


@dataclass(frozen=True, slots=True)
class TeamRef:
    team_id: int | None
    name: str | None
    abbreviation: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerRef:
    canonical_player_id: int | None
    champion_data_player_id: str | None
    display_name: str | None


@dataclass(frozen=True, slots=True)
class SeasonListItem:
    season_id: int
    year: int
    name: str
    is_current: bool
    round_count: int
    match_count: int
    team_count: int
    status: str
    status_summary: str


@dataclass(frozen=True, slots=True)
class RoundListItem:
    round_id: int
    round_number: int | None
    label: str
    start_time: str | None
    end_time: str | None
    match_count: int
    concluded_count: int
    live_count: int
    scheduled_count: int
    state: HealthState
    state_summary: str


@dataclass(slots=True)
class SeasonDetail:
    season_id: int
    year: int
    name: str
    is_current: bool
    current_round_number: int | None
    status: str
    aggregates: dict[str, Any]
    rounds: list[RoundListItem] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MatchListItem:
    match_id: int
    round_id: int
    home: TeamRef
    away: TeamRef
    status: str | None
    lifecycle: str | None
    start_time_utc: str | None
    venue: str | None
    score_home: int | None
    score_away: int | None
    state: HealthState
    state_summary: str


@dataclass(slots=True)
class RoundDetail:
    round_id: int
    season_id: int
    season_year: int
    round_number: int | None
    label: str
    start_time: str | None
    end_time: str | None
    byes: list[TeamRef] = field(default_factory=list)
    matches: list[MatchListItem] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DatasetState:
    key: str
    label: str
    state: HealthState
    summary: str
    count: int | None = None
    last_observed_utc: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerStatRow:
    player: PlayerRef
    side: str
    team: TeamRef | None
    stats: dict[str, Any]
    snapshot_authority: int | None


@dataclass(frozen=True, slots=True)
class RosterContextEntry:
    player: PlayerRef
    reason: str | None


@dataclass(frozen=True, slots=True)
class RosterSelectionEntry:
    player: PlayerRef
    position: str | None
    jumper_number: int | None
    captain: bool | None


@dataclass(frozen=True, slots=True)
class RosterSideView:
    team: TeamRef | None
    team_status: str | None
    selections: list[RosterSelectionEntry]
    ins: list[RosterContextEntry]
    outs: list[RosterContextEntry]
    late_changes: list[RosterContextEntry]
    club_debuts: list[RosterContextEntry]
    milestones: list[RosterContextEntry]


@dataclass(frozen=True, slots=True)
class CommentaryEventView:
    id: int
    period_number: int | None
    period_seconds: int | None
    comment: str | None
    score_event: bool | None
    player_name: str | None
    team_name: str | None
    observed_at: str


@dataclass(frozen=True, slots=True)
class InterchangeStatusView:
    player_name: str | None
    side: str
    team_name: str | None
    on_bench: bool
    interchange_count: int | None
    bench_reason: str | None
    observed_at: str


@dataclass(frozen=True, slots=True)
class PeriodCheckpointSummary:
    marker: str
    player_count: int


@dataclass(frozen=True, slots=True)
class ProviderEvidence:
    match_provider_id: str | None
    latest_scrape_status: str | None
    latest_scrape_at: str | None
    latest_scrape_rows_written: int | None
    diagnostics: list[DatasetState] = field(default_factory=list)


@dataclass(slots=True)
class MatchDetail:
    match_id: int
    round_id: int
    round_label: str | None
    season_id: int | None
    season_year: int | None
    match_provider_id: str | None
    home: TeamRef
    away: TeamRef
    status: str | None
    lifecycle: str | None
    start_time_utc: str | None
    venue: str | None
    score_home: int | None
    score_away: int | None
    datasets: list[DatasetState]
    overall_state: HealthState
    overall_state_summary: str
    player_stats: list[PlayerStatRow] = field(default_factory=list)
    rosters: dict[str, RosterSideView | None] = field(default_factory=dict)
    commentary_events: list[CommentaryEventView] = field(default_factory=list)
    commentary_total_count: int = 0
    interchanges: list[InterchangeStatusView] = field(default_factory=list)
    period_checkpoints: list[PeriodCheckpointSummary] = field(default_factory=list)
    provider_evidence: ProviderEvidence | None = None


@dataclass(frozen=True, slots=True)
class PlayerSeasonView:
    season_id: int
    year: int
    name: str
    team: TeamRef | None


@dataclass(frozen=True, slots=True)
class PlayerMatchInvolvement:
    match_id: int
    round_label: str | None
    opponent: TeamRef | None
    side: str | None
    status: str | None
    start_time_utc: str | None


@dataclass(slots=True)
class PlayerDetail:
    canonical_player_id: int
    display_name: str | None
    afl_player_id: int | None
    champion_data_player_id: str | None
    current_team: TeamRef | None
    seasons: list[PlayerSeasonView] = field(default_factory=list)
    recent_matches: list[PlayerMatchInvolvement] = field(default_factory=list)


def _team_ref(team_id: int | None, teams: dict[int, tuple[str | None, str | None]]) -> TeamRef | None:
    if team_id is None:
        return None
    name, abbreviation = teams.get(team_id, (None, None))
    return TeamRef(team_id=team_id, name=name, abbreviation=abbreviation)


def _status_summary(report) -> str:
    counts = {key: value for key, value in report.severity_counts.items() if value}
    if not counts:
        return "No findings against the shared season completeness report."
    parts = ", ".join(f"{value} {key}" for key, value in counts.items())
    return f"{parts} finding(s) in the shared season completeness report."


def _presence_summary(state: HealthState, count: int, noun: str, lifecycle: str | None) -> str:
    if state is HealthState.UPCOMING:
        return f"Match has not started; {noun} not yet expected."
    if count == 0:
        if state is HealthState.ATTENTION:
            return f"Concluded match has no {noun} yet -- appears to be missing."
        return f"No {noun} observed yet; match is still in progress."
    return f"{count} {noun} observed."


def _stats_dataset_state(finality, lifecycle: str | None) -> tuple[HealthState, str]:
    """Player-statistics state, reusing the shared CFS finality predicate."""
    if lifecycle == "CONCLUDED":
        if finality.has_satisfactory_concluded_coverage:
            return HealthState.HEALTHY, (
                f"{finality.authoritative_rows} authoritative player-statistic row(s); "
                "two-sided and final."
            )
        if finality.has_authoritative_snapshot:
            return HealthState.PARTIAL, (
                f"{finality.authoritative_rows} authoritative row(s), but mixed-authority or "
                "one-sided."
            )
        return HealthState.MISSING, "Concluded match has no authoritative player-statistics snapshot yet."
    if lifecycle in ("LIVE", "POSTGAME"):
        if finality.has_authoritative_snapshot:
            return HealthState.PARTIAL, (
                f"{finality.authoritative_rows} row(s) captured so far; match is still in progress."
            )
        return HealthState.UNKNOWN, "No authoritative statistics observed yet; match is still in progress."
    if lifecycle == "SCHEDULED":
        return HealthState.UPCOMING, "Match has not started; player statistics are not yet expected."
    return HealthState.UNKNOWN, "Match lifecycle is not recognised; statistics completeness cannot be judged."


def _fixture_dataset_state(*, match_provider_id: str | None, home_team_id: int | None,
                           away_team_id: int | None, lifecycle: str | None) -> DatasetState:
    issues = []
    if home_team_id is None or away_team_id is None:
        issues.append("a participating team is unresolved")
    if not match_provider_id and lifecycle == "CONCLUDED":
        issues.append("no Champion Data provider identifier is recorded")
    if issues:
        return DatasetState("fixture", "Fixture", HealthState.ATTENTION,
                             ("Concluded match: " if lifecycle == "CONCLUDED" else "") +
                             "; ".join(issues).capitalize() + ".")
    return DatasetState("fixture", "Fixture", HealthState.HEALTHY, "Canonical fixture record is complete.")


def _overall_state(datasets: list[DatasetState]) -> tuple[HealthState, str]:
    if not datasets:
        return HealthState.UNKNOWN, "No dataset information available."
    worst = min(datasets, key=lambda item: _STATE_PRECEDENCE.index(item.state))
    return worst.state, worst.summary


class DataExplorerReporter:
    """Gather bounded, read-only Season/Round/Match/Player evidence for the Admin Data Explorer."""

    def __init__(self, conn: sqlite3.Connection, *, clock=None, database: str | None = None,
                 competition_code: str = DEFAULT_COMPETITION_CODE,
                 competition_provider_id: str = DEFAULT_COMPETITION_PROVIDER_ID):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.database = database
        self.competition_code = competition_code
        self.competition_provider_id = competition_provider_id

    # -- season list -------------------------------------------------------

    def list_seasons(self) -> list[SeasonListItem]:
        seasons = list_persisted_afl_seasons(
            self.conn, competition_code=self.competition_code,
            competition_provider_id=self.competition_provider_id,
        )
        items = []
        for season in seasons:
            counts = self.conn.execute(
                "SELECT (SELECT COUNT(*) FROM rounds WHERE season_id=?) AS rounds,"
                "(SELECT COUNT(*) FROM matches WHERE season_id=?) AS matches,"
                "(SELECT COUNT(*) FROM afl_team_seasons ts JOIN afl_teams t ON t.afl_id=ts.team_id "
                "WHERE ts.competition_season_id=? AND t.season_id=?) AS teams",
                (season.season_id, season.season_id, season.season_id, season.season_id),
            ).fetchone()
            report = self._season_report(season.year)
            items.append(SeasonListItem(
                season_id=season.season_id, year=season.year, name=season.name,
                is_current=season.is_current, round_count=counts["rounds"],
                match_count=counts["matches"], team_count=counts["teams"],
                status=report.status.value, status_summary=_status_summary(report),
            ))
        return items

    def _season_report(self, year: int):
        return SeasonCompletenessReporter(self.conn, clock=self.clock, database=self.database).report(
            year, competition_code=self.competition_code, competition_provider_id=self.competition_provider_id,
        )

    # -- season detail -------------------------------------------------------

    def season_detail(self, season_id: int) -> SeasonDetail | None:
        season_row = self.conn.execute(
            "SELECT s.afl_id, s.year, s.name, s.is_current, s.current_round_number "
            "FROM afl_seasons s JOIN afl_competitions c ON c.afl_id=s.competition_id "
            "WHERE c.code=? AND c.provider_id=? AND s.afl_id=?",
            (self.competition_code, self.competition_provider_id, season_id),
        ).fetchone()
        if season_row is None:
            return None
        report = self._season_report(season_row["year"])

        round_rows = self.conn.execute(
            "SELECT round_id, round_label, round_number, start_time, end_time "
            "FROM rounds WHERE season_id=? "
            "ORDER BY round_number IS NULL, round_number, round_id", (season_id,),
        ).fetchall()
        match_rows = self.conn.execute(
            "SELECT match_id, round_id, status FROM matches WHERE season_id=?", (season_id,),
        ).fetchall()
        matches_by_round: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for row in match_rows:
            matches_by_round[row["round_id"]].append(row)

        rounds = []
        for row in round_rows:
            round_matches = matches_by_round.get(row["round_id"], [])
            lifecycles = [normalise_match_status(m["status"]) for m in round_matches]
            concluded = lifecycles.count("CONCLUDED")
            live = sum(1 for item in lifecycles if item in ("LIVE", "POSTGAME"))
            scheduled = lifecycles.count("SCHEDULED")
            match_ids = {m["match_id"] for m in round_matches}
            relevant_findings = [item for item in report.findings
                                 if item.match_id is not None and item.match_id in match_ids]
            state = state_from_findings(relevant_findings)
            if state is HealthState.HEALTHY and concluded == 0 and live == 0 and scheduled > 0:
                state = HealthState.UPCOMING
            if not round_matches:
                state = HealthState.UNKNOWN
                summary = "No matches are scheduled for this round yet."
            elif state is HealthState.UPCOMING:
                summary = "Round has not started; results and statistics are not yet expected."
            elif state is HealthState.HEALTHY:
                summary = f"{concluded} of {len(round_matches)} match(es) concluded; no findings."
            else:
                summary = (f"{len(relevant_findings)} finding(s) against this round's matches in the "
                           "shared season completeness report.")
            rounds.append(RoundListItem(
                round_id=row["round_id"], round_number=row["round_number"],
                label=row["round_label"] or (f"Round {row['round_number']}" if row["round_number"] else f"Round {row['round_id']}"),
                start_time=row["start_time"], end_time=row["end_time"],
                match_count=len(round_matches), concluded_count=concluded, live_count=live,
                scheduled_count=scheduled, state=state, state_summary=summary,
            ))

        return SeasonDetail(
            season_id=season_row["afl_id"], year=season_row["year"],
            name=season_row["name"] or str(season_row["year"]), is_current=bool(season_row["is_current"]),
            current_round_number=season_row["current_round_number"], status=report.status.value,
            aggregates=report.aggregates, rounds=rounds,
        )

    # -- round detail -------------------------------------------------------

    def round_detail(self, season_id: int, round_id: int) -> RoundDetail | None:
        season_row = self.conn.execute(
            "SELECT afl_id, year FROM afl_seasons WHERE afl_id=?", (season_id,)
        ).fetchone()
        if season_row is None:
            return None
        round_row = self.conn.execute(
            "SELECT round_id, round_label, season_id, round_number, abbreviation, start_time, end_time, "
            "byes_json FROM rounds WHERE round_id=? AND season_id=?", (round_id, season_id),
        ).fetchone()
        if round_row is None:
            return None

        teams = api_team_projection(self.conn)
        canonical_round = api_round_from_row(round_row, teams)
        byes = [TeamRef(b.team_id, b.name, b.abbreviation) for b in (canonical_round.byes or [])]

        match_rows = self.conn.execute(
            "SELECT m.match_id, m.match_provider_id, m.status, m.start_time_utc, m.venue, "
            "m.score_home, m.score_away, m.home_team_id, m.away_team_id, "
            "ht.afl_id AS home_id, ht.name AS home_name, ht.abbreviation AS home_abbr, "
            "at.afl_id AS away_id, at.name AS away_name, at.abbreviation AS away_abbr "
            "FROM matches m LEFT JOIN afl_teams ht ON ht.afl_id=m.home_team_id "
            "LEFT JOIN afl_teams at ON at.afl_id=m.away_team_id "
            "WHERE m.round_id=? "
            "ORDER BY m.start_time_utc IS NULL, m.start_time_utc, m.match_id", (round_id,),
        ).fetchall()

        matches = []
        for row in match_rows:
            lifecycle = normalise_match_status(row["status"])
            datasets = [
                _fixture_dataset_state(match_provider_id=row["match_provider_id"],
                                       home_team_id=row["home_team_id"], away_team_id=row["away_team_id"],
                                       lifecycle=lifecycle),
                *self._match_dataset_states(match_id=row["match_id"], match_provider_id=row["match_provider_id"],
                                            lifecycle=lifecycle),
            ]
            state, summary = _overall_state(datasets)
            matches.append(MatchListItem(
                match_id=row["match_id"], round_id=round_id,
                home=TeamRef(row["home_id"], row["home_name"], row["home_abbr"]),
                away=TeamRef(row["away_id"], row["away_name"], row["away_abbr"]),
                status=row["status"], lifecycle=lifecycle, start_time_utc=row["start_time_utc"],
                venue=row["venue"], score_home=row["score_home"], score_away=row["score_away"],
                state=state, state_summary=summary,
            ))

        label = round_row["round_label"] or (
            f"Round {round_row['round_number']}" if round_row["round_number"] else f"Round {round_id}")
        return RoundDetail(
            round_id=round_id, season_id=season_id, season_year=season_row["year"],
            round_number=round_row["round_number"], label=label,
            start_time=round_row["start_time"], end_time=round_row["end_time"],
            byes=byes, matches=matches,
        )

    # -- shared per-match dataset presence ------------------------------------

    def _match_dataset_states(self, *, match_id: int, match_provider_id: str | None,
                              lifecycle: str | None) -> list[DatasetState]:
        finality = authoritative_stats_finality_for_match(self.conn, match_provider_id)
        stats_state, stats_summary = _stats_dataset_state(finality, lifecycle)
        states = [DatasetState("player_statistics", "Player statistics", stats_state, stats_summary,
                                count=finality.authoritative_rows)]

        team_rows = current_roster_teams(self.conn, match_id)
        roster_state = dataset_presence_state(lifecycle, len(team_rows))
        last_roster_update = max(
            (r["source_last_updated"] for r in team_rows if r["source_last_updated"]), default=None)
        states.append(DatasetState(
            "rosters", "Rosters / lineups", roster_state,
            _presence_summary(roster_state, len(team_rows), "roster observation(s)", lifecycle),
            count=len(team_rows), last_observed_utc=last_roster_update,
        ))

        commentary_row = self.conn.execute(
            "SELECT COUNT(*) AS n, MAX(first_observed_at) AS last FROM match_commentary_events "
            "WHERE match_id=?", (match_id,),
        ).fetchone()
        commentary_state = dataset_presence_state(lifecycle, commentary_row["n"])
        states.append(DatasetState(
            "commentary", "Commentary", commentary_state,
            _presence_summary(commentary_state, commentary_row["n"], "event(s)", lifecycle),
            count=commentary_row["n"], last_observed_utc=commentary_row["last"],
        ))

        interchange_row = self.conn.execute(
            "SELECT COUNT(*) AS n, MAX(last_observed_at) AS last FROM match_interchange_state "
            "WHERE match_id=?", (match_id,),
        ).fetchone()
        interchange_state = dataset_presence_state(lifecycle, interchange_row["n"])
        states.append(DatasetState(
            "interchange", "Interchange", interchange_state,
            _presence_summary(interchange_state, interchange_row["n"], "player record(s)", lifecycle),
            count=interchange_row["n"], last_observed_utc=interchange_row["last"],
        ))
        return states

    # -- match detail -------------------------------------------------------

    def match_detail(self, match_id: int) -> MatchDetail | None:
        row = self.conn.execute(
            "SELECT m.match_id, m.match_provider_id, m.round_id, m.season_id, m.status, "
            "m.start_time_utc, m.venue, m.score_home, m.score_away, m.home_team_id, m.away_team_id, "
            "ht.afl_id AS home_id, ht.name AS home_name, ht.abbreviation AS home_abbr, "
            "at.afl_id AS away_id, at.name AS away_name, at.abbreviation AS away_abbr, "
            "r.round_label, r.round_number, s.year AS season_year "
            "FROM matches m "
            "LEFT JOIN afl_teams ht ON ht.afl_id=m.home_team_id "
            "LEFT JOIN afl_teams at ON at.afl_id=m.away_team_id "
            "LEFT JOIN rounds r ON r.round_id=m.round_id "
            "LEFT JOIN afl_seasons s ON s.afl_id=m.season_id "
            "WHERE m.match_id=?", (match_id,),
        ).fetchone()
        if row is None:
            return None

        lifecycle = normalise_match_status(row["status"])
        match_provider_id = row["match_provider_id"]
        datasets = [
            _fixture_dataset_state(match_provider_id=match_provider_id, home_team_id=row["home_team_id"],
                                   away_team_id=row["away_team_id"], lifecycle=lifecycle),
            *self._match_dataset_states(match_id=match_id, match_provider_id=match_provider_id, lifecycle=lifecycle),
        ]
        overall_state, overall_summary = _overall_state(datasets)

        teams = api_team_projection(self.conn)
        player_stats = self._player_stats(match_provider_id, teams) if match_provider_id else []
        rosters = self._roster_views(match_id, teams)
        commentary_events, commentary_total = self._commentary_preview(match_id, teams)
        interchanges = self._interchange_status(match_id, teams)
        period_checkpoints = self._period_checkpoints(match_provider_id)
        provider_evidence = self._provider_evidence(match_id, match_provider_id)

        round_label = row["round_label"] or (
            f"Round {row['round_number']}" if row["round_number"] else None)
        return MatchDetail(
            match_id=row["match_id"], round_id=row["round_id"], round_label=round_label,
            season_id=row["season_id"], season_year=row["season_year"], match_provider_id=match_provider_id,
            home=TeamRef(row["home_id"], row["home_name"], row["home_abbr"]),
            away=TeamRef(row["away_id"], row["away_name"], row["away_abbr"]),
            status=row["status"], lifecycle=lifecycle, start_time_utc=row["start_time_utc"],
            venue=row["venue"], score_home=row["score_home"], score_away=row["score_away"],
            datasets=datasets, overall_state=overall_state, overall_state_summary=overall_summary,
            player_stats=player_stats, rosters=rosters, commentary_events=commentary_events,
            commentary_total_count=commentary_total, interchanges=interchanges,
            period_checkpoints=period_checkpoints, provider_evidence=provider_evidence,
        )

    def _player_stats(self, match_provider_id: str,
                      teams: dict[int, tuple[str | None, str | None]]) -> list[PlayerStatRow]:
        stat_columns = ", ".join(f"s.{name}" for name in CANONICAL_STAT_FIELDS)
        rows = self.conn.execute(
            "SELECT s.champion_data_player_id, s.canonical_player_id, s.side, s.snapshot_authority, "
            f"{stat_columns}, cp.display_name, cp.given_name, cp.family_name, "
            "CASE s.side WHEN 'home' THEN m.home_team_id WHEN 'away' THEN m.away_team_id END AS team_id "
            "FROM cfs_player_stats s JOIN matches m ON m.match_provider_id=s.match_provider_id "
            "LEFT JOIN canonical_players cp ON cp.id=s.canonical_player_id "
            "WHERE s.match_provider_id=? ORDER BY s.side, s.champion_data_player_id",
            (match_provider_id,),
        ).fetchall()
        return [
            PlayerStatRow(
                player=PlayerRef(row["canonical_player_id"], row["champion_data_player_id"],
                                 api_display_name(row)),
                side=row["side"], team=_team_ref(row["team_id"], teams),
                stats={name: row[name] for name in CANONICAL_STAT_FIELDS},
                snapshot_authority=row["snapshot_authority"],
            )
            for row in rows
        ]

    def _roster_views(self, match_id: int,
                      teams: dict[int, tuple[str | None, str | None]]) -> dict[str, RosterSideView | None]:
        team_rows = current_roster_teams(self.conn, match_id)
        selection_rows = current_roster_selections(self.conn, match_id)
        context_rows = current_roster_context(self.conn, match_id)
        player_names = self._player_name_lookup(
            {row["canonical_player_id"] for row in (*selection_rows, *context_rows)
             if row["canonical_player_id"] is not None})

        def player_ref(row) -> PlayerRef:
            return PlayerRef(row["canonical_player_id"], row["player_provider_id"],
                             player_names.get(row["canonical_player_id"]))

        sides: dict[str, RosterSideView] = {}
        for team_row in team_rows:
            side = team_row["side"]
            selections = [
                RosterSelectionEntry(
                    player=player_ref(row), position=row["position"], jumper_number=row["jumper_number"],
                    captain=(bool(row["captain"]) if row["captain"] is not None else None),
                )
                for row in selection_rows if row["team_provider_id"] == team_row["team_provider_id"]
            ]
            context_by_type: dict[str, list[RosterContextEntry]] = defaultdict(list)
            for row in context_rows:
                if row["team_provider_id"] != team_row["team_provider_id"]:
                    continue
                context_by_type[row["context_type"]].append(
                    RosterContextEntry(player=player_ref(row), reason=row["reason"]))
            sides[side] = RosterSideView(
                team=_team_ref(team_row["canonical_team_id"], teams), team_status=team_row["team_status"],
                selections=selections, ins=context_by_type["ins"], outs=context_by_type["outs"],
                late_changes=context_by_type["lateChanges"], club_debuts=context_by_type["clubDebuts"],
                milestones=context_by_type["milestones"],
            )
        return {"home": sides.get("home"), "away": sides.get("away")}

    def _commentary_preview(self, match_id: int,
                            teams: dict[int, tuple[str | None, str | None]],
                            ) -> tuple[list[CommentaryEventView], int]:
        all_events = commentary_event_rows(self.conn, match_id=match_id)
        total = len(all_events)
        preview = list(reversed(all_events[-COMMENTARY_PREVIEW_LIMIT:]))
        player_names = self._player_name_lookup(
            {row["canonical_player_id"] for row in preview if row["canonical_player_id"] is not None})
        events = [
            CommentaryEventView(
                id=row["id"], period_number=row["period_number"], period_seconds=row["period_seconds"],
                comment=row["comment"], score_event=row["score_event"],
                player_name=player_names.get(row["canonical_player_id"]),
                team_name=(teams.get(row["canonical_team_id"], (None, None))[0]
                          if row["canonical_team_id"] is not None else None),
                observed_at=row["first_observed_at"],
            )
            for row in preview
        ]
        return events, total

    def _interchange_status(self, match_id: int,
                            teams: dict[int, tuple[str | None, str | None]]) -> list[InterchangeStatusView]:
        rows = interchange_current_state_rows(self.conn, match_id=match_id)
        player_names = self._player_name_lookup(
            {row["canonical_player_id"] for row in rows if row["canonical_player_id"] is not None})
        return [
            InterchangeStatusView(
                player_name=player_names.get(row["canonical_player_id"]), side=row["side"],
                team_name=(teams.get(row["canonical_team_id"], (None, None))[0]
                          if row["canonical_team_id"] is not None else None),
                on_bench=row["on_bench"], interchange_count=row["interchange_count"],
                bench_reason=row["bench_reason"], observed_at=row["last_observed_at"],
            )
            for row in rows
        ]

    def _period_checkpoints(self, match_provider_id: str | None) -> list[PeriodCheckpointSummary]:
        if not match_provider_id:
            return []
        rows = self.conn.execute(
            "SELECT checkpoint_marker, COUNT(DISTINCT champion_data_player_id) AS player_count "
            "FROM cfs_player_stat_checkpoints WHERE match_provider_id=? GROUP BY checkpoint_marker",
            (match_provider_id,),
        ).fetchall()

        def sort_key(row) -> int:
            try:
                return _CHECKPOINT_ORDER.index(row["checkpoint_marker"])
            except ValueError:
                return len(_CHECKPOINT_ORDER)

        return [PeriodCheckpointSummary(marker=row["checkpoint_marker"], player_count=row["player_count"])
                for row in sorted(rows, key=sort_key)]

    def _provider_evidence(self, match_id: int, match_provider_id: str | None) -> ProviderEvidence:
        latest_scrape = None
        if match_provider_id:
            latest_scrape = self.conn.execute(
                "SELECT status, started_at, rows_written FROM scrape_runs "
                "WHERE target_type='match' AND target_identifier=? "
                "ORDER BY started_at DESC LIMIT 1", (match_provider_id,),
            ).fetchone()

        diagnostics = []
        for table, timestamp_column, label in (
            ("match_state_evidence_observations", "observed_at", "Match-clock diagnostic evidence"),
            ("match_interchange_evidence_observations", "observed_at", "Interchange diagnostic evidence"),
            ("commentary_evidence_events", "last_seen_at", "Commentary diagnostic evidence"),
        ):
            # Table and column names are a fixed, checked-in set (never user input).
            evidence_row = self.conn.execute(
                f"SELECT COUNT(*) AS n, MAX({timestamp_column}) AS last FROM {table} WHERE match_id=?",
                (match_id,),
            ).fetchone()
            if evidence_row["n"]:
                diagnostics.append(DatasetState(
                    table, label, HealthState.UNKNOWN, f"{evidence_row['n']} diagnostic observation(s).",
                    count=evidence_row["n"], last_observed_utc=evidence_row["last"],
                ))

        return ProviderEvidence(
            match_provider_id=match_provider_id,
            latest_scrape_status=latest_scrape["status"] if latest_scrape else None,
            latest_scrape_at=latest_scrape["started_at"] if latest_scrape else None,
            latest_scrape_rows_written=latest_scrape["rows_written"] if latest_scrape else None,
            diagnostics=diagnostics,
        )

    def _player_name_lookup(self, player_ids: set[int]) -> dict[int, str | None]:
        if not player_ids:
            return {}
        placeholders = ",".join("?" for _ in player_ids)
        rows = self.conn.execute(
            f"SELECT id, display_name, given_name, family_name FROM canonical_players WHERE id IN ({placeholders})",
            tuple(player_ids),
        ).fetchall()
        return {row["id"]: api_display_name(row) for row in rows}

    # -- player detail -------------------------------------------------------

    def player_detail(self, player_id: int) -> PlayerDetail | None:
        row = self.conn.execute(
            "SELECT id, display_name, given_name, family_name FROM canonical_players WHERE id=?",
            (player_id,),
        ).fetchone()
        if row is None:
            return None

        teams = api_team_projection(self.conn)
        identifiers = api_identifiers(self.conn, player_id)
        current_team = api_current_team(self.conn, player_id)
        seasons = api_season_memberships(self.conn, player_id)

        recent_rows = self.conn.execute(
            "SELECT DISTINCT m.match_id, m.round_id, m.status, m.start_time_utc, s.side, r.round_label, "
            "CASE s.side WHEN 'home' THEN m.away_team_id ELSE m.home_team_id END AS opponent_id "
            "FROM cfs_player_stats s JOIN matches m ON m.match_provider_id=s.match_provider_id "
            "LEFT JOIN rounds r ON r.round_id=m.round_id "
            "WHERE s.canonical_player_id=? "
            "ORDER BY m.start_time_utc IS NULL, m.start_time_utc DESC LIMIT ?",
            (player_id, PLAYER_RECENT_MATCH_LIMIT),
        ).fetchall()

        return PlayerDetail(
            canonical_player_id=row["id"], display_name=api_display_name(row),
            afl_player_id=identifiers.afl_player_id, champion_data_player_id=identifiers.champion_data_player_id,
            current_team=_team_ref(current_team.team_id, teams) if current_team else None,
            seasons=[
                PlayerSeasonView(season_id=item.season_id, year=item.year, name=item.name,
                                 team=_team_ref(item.team.team_id, teams) if item.team else None)
                for item in seasons
            ],
            recent_matches=[
                PlayerMatchInvolvement(
                    match_id=item["match_id"], round_label=item["round_label"],
                    opponent=_team_ref(item["opponent_id"], teams), side=item["side"],
                    status=item["status"], start_time_utc=item["start_time_utc"],
                )
                for item in recent_rows
            ],
        )
