"""Read-only Admin operations / data-health dashboard reporting (Issue #225).

This module aggregates *existing* authoritative evidence -- it never adds a
second, independent notion of "health". Concretely it reuses:

* :class:`afl_json.season_report.SeasonCompletenessReporter` for every
  season-scoped dataset (seasons, teams, rounds, matches, canonical
  players/season memberships, player statistics) -- the exact report and
  finding codes already shown on the Admin Season Review page.
* ``afl_seasons.is_current`` / ``afl_seasons.current_round_number``,
  populated by season-sync persistence (see ``GET /api/v1/seasons``), for
  "what season/round is current" rather than deriving it independently.
* :func:`scheduler.registry.job_type_activity_summary` and
  :func:`scheduler.match_windows.status_summary` for scheduler/collector
  activity -- both are bounded ``GROUP BY`` aggregates over the same
  persisted scheduler state already shown on the Admin Scheduling page.
* The already-validated scheduler health contract
  (``admin._fetch_scheduler_health``, Issue #178) passed in by the caller,
  never re-fetched or re-validated here.

Everything else is a small number of additional bounded, mostly aggregate
queries (single ``GROUP BY``s, scoped to the current season or its single
most relevant round) for datasets that have no dedicated reporter yet
(match rosters/lineups, commentary, interchange, injuries). Where no
authoritative rule exists for a dataset yet, the result is ``UNKNOWN``
rather than an invented completeness rule -- see
``docs/admin_operations_dashboard.md``.

This module never opens its own read-write connection and never mutates
anything: every method takes an already-open connection from the caller
(the Admin route uses ``db.connection.get_read_only_db_connection``), so a
failure or slow query here cannot contend with, or affect, ingestion.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable

from afl_json.match_status import normalise_match_status
from afl_json.season_report import Severity, SeasonCompletenessReporter, SeasonReport
from scheduler.match_windows import status_summary as match_window_status_summary
from scheduler.registry import job_type_activity_summary


class HealthState(str, Enum):
    """Operator-facing dataset/system states (Issue #225).

    Deliberately not a weighted/percentage score: every state here is a
    label an operator can act on, derived from existing authoritative
    evidence. ``UNKNOWN`` is used whenever the repository has no
    sufficiently authoritative rule to distinguish the other states,
    rather than inventing one.
    """

    HEALTHY = "healthy"
    ATTENTION = "attention"
    PARTIAL = "partial"
    STALE = "stale"
    MISSING = "missing"
    FAILED = "failed"
    UPCOMING = "upcoming"
    UNKNOWN = "unknown"
    # A human-reviewed disposition (e.g. stats_not_expected) explains an
    # otherwise-missing dataset -- distinct from HEALTHY (data was collected)
    # and MISSING/ATTENTION (nothing explains the absence). See
    # afl_json.match_data_exceptions and Issue #233.
    REVIEWED = "reviewed"


_STATE_STYLE = {
    HealthState.HEALTHY: "success",
    HealthState.UPCOMING: "info",
    HealthState.PARTIAL: "warning",
    HealthState.STALE: "warning",
    HealthState.ATTENTION: "warning",
    HealthState.MISSING: "secondary",
    HealthState.FAILED: "danger",
    HealthState.UNKNOWN: "secondary",
    HealthState.REVIEWED: "info",
}


def state_style(state: HealthState) -> str:
    """Bootstrap badge/alert style for a health state; keeps this mapping out of templates."""
    return _STATE_STYLE[state]


_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}

# Dashboard-presentation thresholds only. Not a collection SLA enforced
# anywhere else in the system -- see docs/admin_operations_dashboard.md.
INJURIES_STALE_AFTER = timedelta(hours=36)
INJURIES_MISSING_AFTER = timedelta(days=7)
ROUND_IN_PROGRESS_GRACE = timedelta(hours=6)
ATTENTION_ITEM_LIMIT = 25

DEFAULT_COMPETITION_CODE = "AFL"
DEFAULT_COMPETITION_PROVIDER_ID = "CD_C014"

_JOB_TYPE_LABELS = {
    "injury": "Daily injuries",
    "fixture": "Fixtures",
    "general_refresh": "General refresh",
    "lineup": "Lineups / rosters",
    "player_stats": "Player statistics (manual/legacy trigger)",
    "match_refresh": "Match refresh",
}

# Finding codes from SeasonCompletenessReporter attributed to each
# season-scoped dataset below. Kept explicit (rather than using `domain`
# directly) because a handful of codes describing team/player context sit in
# a different report domain than the dataset they are most useful attached
# to -- see afl_json/season_report.py.
_TEAMS_CODES = frozenset({"season.no_teams"})
_ROUNDS_FIXTURES_CODES = frozenset({
    "season.no_rounds", "season.no_matches", "match.missing_round",
})
_MATCHES_CODES = frozenset({
    "match.missing_team", "match.duplicate_provider_id", "match.missing_provider_id",
})
_PLAYER_STATS_CODES = frozenset({
    "match.final_without_authoritative_stats", "match.partial_authoritative_stats",
    "stats.suspicious_player_count", "stats.authority_lifecycle_conflict",
    "stats.team_participant_mismatch", "audit.no_successful_stat_run",
    "audit.latest_run_failed_or_partial", "audit.success_without_rows",
})
_PLAYERS_MEMBERSHIPS_CODES = frozenset({
    "player.incomplete_identity", "player.missing_afl_provider_id",
    "player.missing_champion_data_provider_id", "player.provider_mapping_conflict",
    "stats.player_missing_season_membership", "stats.unresolved_canonical_player",
    "stats.provider_player_unknown",
})


@dataclass(frozen=True, slots=True)
class DatasetHealth:
    """One row of the dataset-health section. ``link`` targets an existing Admin page."""

    key: str
    label: str
    state: HealthState
    summary: str
    detail: str | None = None
    count: int | None = None
    last_observed_utc: str | None = None
    link: str | None = None


@dataclass(frozen=True, slots=True)
class AttentionItem:
    """One bounded, prioritised finding shown in the attention/exceptions section."""

    severity: Severity
    source: str
    code: str
    message: str
    link: str | None = None


@dataclass(frozen=True, slots=True)
class SchedulerJobTypeActivity:
    job_type: str
    label: str
    state: HealthState
    total: int
    pending: int
    running: int
    succeeded: int
    failed: int
    skipped: int
    last_success_utc: str | None
    last_attempt_utc: str | None
    next_scheduled_utc: str | None


@dataclass(frozen=True, slots=True)
class MatchWindowActivity:
    """One status bucket of the durable per-match player-statistics collection plan."""

    status: str
    count: int
    failing: int
    last_success_utc: str | None
    next_due_utc: str | None


@dataclass(frozen=True, slots=True)
class RoundContext:
    round_id: int
    label: str
    round_number: int | None
    window_state: str  # in_progress | upcoming | recently_concluded | unknown
    match_count: int
    starts_at_utc: str | None
    ends_at_utc: str | None


@dataclass(frozen=True, slots=True)
class SeasonContext:
    season_id: int
    year: int
    name: str
    is_current: bool
    current_round_number: int | None
    round: RoundContext | None


@dataclass(frozen=True, slots=True)
class SystemOverview:
    generated_at_utc: str
    database_state: HealthState
    scheduler_state: HealthState
    scheduler_label: str
    scheduler_detail: str
    season: SeasonContext | None
    last_successful_collection_utc: str | None
    failing_job_types: int
    attention_count: int
    overall_state: HealthState


@dataclass(slots=True)
class OperationsReport:
    generated_at_utc: str
    overview: SystemOverview
    datasets: list[DatasetHealth] = field(default_factory=list)
    scheduler_activity: list[SchedulerJobTypeActivity] = field(default_factory=list)
    match_window_activity: list[MatchWindowActivity] = field(default_factory=list)
    diagnostic_profiles: dict[str, Any] = field(default_factory=dict)
    attention: list[AttentionItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at_utc": self.generated_at_utc,
            "overview": {**asdict(self.overview),
                        "database_state": self.overview.database_state.value,
                        "scheduler_state": self.overview.scheduler_state.value,
                        "overall_state": self.overview.overall_state.value},
            "datasets": [{**asdict(d), "state": d.state.value} for d in self.datasets],
            "scheduler_activity": [{**asdict(a), "state": a.state.value} for a in self.scheduler_activity],
            "match_window_activity": [asdict(m) for m in self.match_window_activity],
            "diagnostic_profiles": self.diagnostic_profiles,
            "attention": [{**asdict(a), "severity": a.severity.value} for a in self.attention],
        }


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_age(delta: timedelta) -> str:
    """Small, dashboard-local age formatter (mirrors admin.py's log-age formatting)."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "less than a minute ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def state_from_findings(findings) -> HealthState:
    """Severity -> HealthState, the single decision this module and the Admin Data
    Explorer (Issue #226) both apply to a set of already-selected findings."""
    if any(item.severity is Severity.ERROR for item in findings):
        return HealthState.FAILED
    if any(item.severity is Severity.WARNING for item in findings):
        return HealthState.ATTENTION
    return HealthState.HEALTHY


def _dataset_state_from_findings(findings, codes: frozenset[str]):
    relevant = [item for item in findings if item.code in codes]
    return state_from_findings(relevant), relevant


def dataset_presence_state(lifecycle: str | None, count: int) -> HealthState:
    """Shared lifecycle-aware "does this per-match dataset exist yet" rule.

    Mirrors the same reasoning ``_lifecycle_scoped_dataset`` below applies at
    round granularity (concluded-and-empty is a real gap; live/postgame-and-
    empty is ambiguous rather than a confirmed problem; not-yet-started is
    simply not yet expected), generalised to a single match/count pair so the
    Admin Data Explorer (Issue #226) can reuse the identical rule at match
    granularity instead of inventing a second completeness definition.
    """
    if lifecycle == "CONCLUDED":
        return HealthState.HEALTHY if count > 0 else HealthState.ATTENTION
    if lifecycle in ("LIVE", "POSTGAME"):
        return HealthState.HEALTHY if count > 0 else HealthState.UNKNOWN
    if lifecycle == "SCHEDULED":
        return HealthState.UPCOMING if count == 0 else HealthState.HEALTHY
    return HealthState.UNKNOWN


class OperationsDashboardReporter:
    """Gather bounded, read-only operational evidence without mutating the connection."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Callable[[], datetime] | None = None,
                 database: str | None = None, competition_code: str = DEFAULT_COMPETITION_CODE,
                 competition_provider_id: str = DEFAULT_COMPETITION_PROVIDER_ID):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.database = database
        self.competition_code = competition_code
        self.competition_provider_id = competition_provider_id

    def report(self, *, scheduler_health: dict[str, Any] | None = None) -> OperationsReport:
        now = self.clock().astimezone(timezone.utc)
        database_state = self._database_state()
        season = self._season_context(now)

        datasets: list[DatasetHealth] = []
        attention: list[AttentionItem] = []
        season_report: SeasonReport | None = None

        if season is not None:
            season_report = SeasonCompletenessReporter(
                self.conn, clock=self.clock, database=self.database,
            ).report(season.year, competition_code=self.competition_code,
                     competition_provider_id=self.competition_provider_id)
            datasets.extend(self._season_scoped_datasets(season, season_report))
            attention.extend(
                AttentionItem(item.severity, "season_review", item.code, item.message,
                              link=f"/season-review?season={season.year}")
                for item in season_report.findings if item.severity is not Severity.INFO
            )
        else:
            datasets.append(DatasetHealth(
                "seasons", "Seasons", HealthState.UNKNOWN,
                "No AFL season is marked current in persisted metadata.", link="/season-review",
            ))
            attention.append(AttentionItem(
                Severity.WARNING, "season_review", "season.no_current_season",
                "No AFL season is marked current in persisted metadata.", link="/season-review",
            ))

        round_scoped = self._round_scoped_datasets(season)
        injuries = self._injuries_dataset(now)
        datasets.extend(round_scoped)
        datasets.append(injuries)
        attention.extend(self._dataset_attention([*round_scoped, injuries]))

        scheduler_activity = self._scheduler_activity()
        match_window_activity = self._match_window_activity(season)
        diagnostic_profiles = self._diagnostic_profiles()
        attention.extend(self._scheduler_attention(scheduler_activity))

        attention.sort(key=lambda item: (_SEVERITY_ORDER[item.severity], item.source, item.code))
        bounded_attention = attention[:ATTENTION_ITEM_LIMIT]

        scheduler_state, scheduler_label, scheduler_detail = self._scheduler_state(scheduler_health)
        failing_job_types = sum(1 for row in scheduler_activity if row.state is HealthState.FAILED)
        overall_state = self._overall_state(database_state, scheduler_state, datasets, attention)
        last_success = self._last_successful_collection(scheduler_activity, match_window_activity)

        overview = SystemOverview(
            generated_at_utc=_iso(now), database_state=database_state,
            scheduler_state=scheduler_state, scheduler_label=scheduler_label,
            scheduler_detail=scheduler_detail, season=season,
            last_successful_collection_utc=last_success, failing_job_types=failing_job_types,
            attention_count=len(attention), overall_state=overall_state,
        )
        return OperationsReport(
            generated_at_utc=_iso(now), overview=overview, datasets=datasets,
            scheduler_activity=scheduler_activity, match_window_activity=match_window_activity,
            diagnostic_profiles=diagnostic_profiles, attention=bounded_attention,
        )

    # -- system overview -------------------------------------------------

    def _database_state(self) -> HealthState:
        try:
            self.conn.execute("SELECT 1").fetchone()
            return HealthState.HEALTHY
        except sqlite3.Error:
            return HealthState.FAILED

    def _scheduler_state(self, scheduler_health: dict[str, Any] | None) -> tuple[HealthState, str, str]:
        """Map the already-validated scheduler health contract to a HealthState.

        ``scheduler_health`` is the dict already produced by
        ``admin._fetch_scheduler_health()`` -- this never re-fetches or
        re-validates it, only maps its stable ``state`` field.
        """
        if not scheduler_health or not scheduler_health.get("available"):
            return HealthState.UNKNOWN, "Unavailable", "The scheduler health endpoint could not be reached."
        state = scheduler_health.get("state")
        job_count = scheduler_health.get("job_count")
        if state == "healthy":
            return HealthState.HEALTHY, "Healthy", f"{job_count} registered job(s)."
        if state == "starting":
            return HealthState.ATTENTION, "Starting", "The scheduler is starting and not yet accepting scheduled work."
        if state == "unhealthy":
            diagnostics = scheduler_health.get("diagnostics") or []
            detail = "The scheduler reports a required dependency or runtime failure."
            if diagnostics:
                detail += " (" + ", ".join(str(code) for code in diagnostics) + ")"
            return HealthState.FAILED, "Unhealthy", detail
        return HealthState.UNKNOWN, "Unknown", "The scheduler health response was not understood."

    def _season_context(self, now: datetime) -> SeasonContext | None:
        row = self.conn.execute(
            "SELECT s.afl_id, s.year, s.name, s.is_current, s.current_round_number "
            "FROM afl_seasons s JOIN afl_competitions c ON c.afl_id = s.competition_id "
            "WHERE c.code = ? AND c.provider_id = ? AND s.is_current = 1 "
            "ORDER BY s.afl_id DESC LIMIT 1",
            (self.competition_code, self.competition_provider_id),
        ).fetchone()
        if row is None:
            return None
        round_ctx = self._round_context(row["afl_id"], row["current_round_number"], now)
        return SeasonContext(
            season_id=row["afl_id"], year=row["year"], name=row["name"] or str(row["year"]),
            is_current=bool(row["is_current"]), current_round_number=row["current_round_number"],
            round=round_ctx,
        )

    def _round_context(self, season_id: int, current_round_number: int | None, now: datetime) -> RoundContext | None:
        rows = self.conn.execute(
            "SELECT r.round_id, r.round_label, r.round_number, "
            "MIN(m.start_time_utc) AS starts_at, MAX(m.start_time_utc) AS ends_at, "
            "COUNT(m.match_id) AS match_count "
            "FROM rounds r LEFT JOIN matches m ON m.round_id = r.round_id "
            "WHERE r.season_id = ? GROUP BY r.round_id ORDER BY starts_at",
            (season_id,),
        ).fetchall()
        if not rows:
            return None

        chosen = None
        if current_round_number is not None:
            chosen = next((row for row in rows if row["round_number"] == current_round_number), None)
        if chosen is not None:
            window_state = self._round_window_state(chosen, now)
        else:
            # Conservative fallback used only when current_round_number is not
            # populated: pick the round whose observed match window contains
            # now, else the nearest upcoming round, else the most recently
            # concluded one.
            in_progress = [row for row in rows if self._round_window_state(row, now) == "in_progress"]
            if in_progress:
                chosen, window_state = in_progress[0], "in_progress"
            else:
                upcoming = [row for row in rows if (_parse_utc(row["starts_at"]) or now) > now]
                if upcoming:
                    chosen, window_state = upcoming[0], "upcoming"
                else:
                    past = [row for row in rows if row["ends_at"]]
                    chosen, window_state = (past[-1], "recently_concluded") if past else (None, "unknown")
        if chosen is None:
            return None
        return RoundContext(
            round_id=chosen["round_id"], label=chosen["round_label"] or f"Round {chosen['round_number']}",
            round_number=chosen["round_number"], window_state=window_state,
            match_count=chosen["match_count"], starts_at_utc=chosen["starts_at"], ends_at_utc=chosen["ends_at"],
        )

    @staticmethod
    def _round_window_state(row: sqlite3.Row, now: datetime) -> str:
        start = _parse_utc(row["starts_at"])
        end = _parse_utc(row["ends_at"])
        if start is None or end is None:
            return "unknown"
        if start <= now <= end + ROUND_IN_PROGRESS_GRACE:
            return "in_progress"
        return "upcoming" if now < start else "recently_concluded"

    # -- season-scoped dataset health --------------------------------------

    def _season_scoped_datasets(self, season: SeasonContext, report: SeasonReport) -> list[DatasetHealth]:
        agg = report.aggregates
        link = f"/season-review?season={season.year}"

        seasons = DatasetHealth("seasons", "Seasons", HealthState.HEALTHY,
                                 f"{season.name} ({season.year}) is the configured current season.",
                                 count=1, link=link)

        state, _ = _dataset_state_from_findings(report.findings, _TEAMS_CODES)
        teams = DatasetHealth("teams", "Teams", state, f"{agg['teams']} participating team(s).",
                               count=agg["teams"], link=link)

        state, _ = _dataset_state_from_findings(report.findings, _ROUNDS_FIXTURES_CODES)
        by_status = ", ".join(f"{k}={v}" for k, v in sorted(agg["matches_by_status"].items())) or "none"
        rounds_fixtures = DatasetHealth(
            "rounds_fixtures", "Rounds & fixtures", state,
            f"{agg['rounds']} round(s), {agg['matches']} match(es).", detail=f"By status: {by_status}",
            count=agg["matches"], link=link,
        )

        state, _ = _dataset_state_from_findings(report.findings, _MATCHES_CODES)
        matches = DatasetHealth("matches", "Matches", state,
                                 f"{agg['matches']} match(es), {agg['concluded_matches']} concluded.",
                                 count=agg["matches"], link=link)

        stats_state, _ = _dataset_state_from_findings(report.findings, _PLAYER_STATS_CODES)
        concluded = agg["concluded_matches"]
        covered = agg["concluded_matches_with_authoritative_stats"]
        if concluded == 0:
            stats_state = HealthState.UPCOMING if stats_state is HealthState.HEALTHY else stats_state
            stats_summary = "No concluded matches yet this season; player statistics are not yet expected."
        else:
            stats_summary = f"{covered}/{concluded} concluded match(es) have authoritative player statistics."
        player_statistics = DatasetHealth("player_statistics", "Player statistics", stats_state, stats_summary,
                                           count=agg["authoritative_stat_rows"], link=link)

        state, _ = _dataset_state_from_findings(report.findings, _PLAYERS_MEMBERSHIPS_CODES)
        memberships = agg["season_memberships"]
        missing_team = agg["memberships_without_team"]
        players = DatasetHealth(
            "canonical_players", "Canonical players & season memberships", state,
            f"{memberships} season membership(s); {missing_team} without a resolved team.",
            count=memberships, link=link,
        )

        return [seasons, teams, rounds_fixtures, matches, player_statistics, players]

    # -- round-scoped and auxiliary dataset health --------------------------

    def _round_scoped_datasets(self, season: SeasonContext | None) -> list[DatasetHealth]:
        round_ctx = season.round if season is not None else None
        if round_ctx is None:
            detail = "No current round is known; round-scoped completeness cannot be determined."
            return [
                DatasetHealth("rosters", "Match rosters / lineups", HealthState.UNKNOWN, detail, link="/schedule"),
                DatasetHealth("commentary", "Commentary", HealthState.UNKNOWN, detail, link="/schedule"),
                DatasetHealth("interchange", "Interchange", HealthState.UNKNOWN, detail, link="/schedule"),
            ]
        return [
            self._rosters_dataset(round_ctx),
            self._lifecycle_scoped_dataset("commentary", "Commentary", round_ctx, "match_commentary_events"),
            self._lifecycle_scoped_dataset("interchange", "Interchange", round_ctx, "match_interchange_state"),
        ]

    def _rosters_dataset(self, round_ctx: RoundContext) -> DatasetHealth:
        """Roster/lineup collection health, derived from the scheduler job registry.

        Reuses the persisted ``scheduler_job_registry`` outcome for each
        ``lineup`` job tied to this round or one of its matches (see
        ``scheduler/schedule_lineup_scrapes.py``) instead of inventing a new
        pre-bounce timing rule -- the scheduler already encodes when a
        roster is expected.
        """
        counts = {
            row["status"]: row["count"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS count FROM scheduler_job_registry "
                "WHERE job_type = 'lineup' AND (round_id = ? OR match_id IN "
                "(SELECT match_id FROM matches WHERE round_id = ?)) GROUP BY status",
                (str(round_ctx.round_id), round_ctx.round_id),
            ).fetchall()
        }
        total = sum(counts.values())
        link = "/schedule"
        label = "Match rosters / lineups"
        if total == 0:
            if round_ctx.window_state == "upcoming":
                return DatasetHealth("rosters", label, HealthState.UPCOMING,
                                      f"No lineup collection is yet scheduled for {round_ctx.label}.",
                                      count=0, link=link)
            return DatasetHealth("rosters", label, HealthState.UNKNOWN,
                                  f"No scheduler registry evidence found for {round_ctx.label}'s lineup jobs.",
                                  count=0, link=link)
        if counts.get("failed"):
            return DatasetHealth("rosters", label, HealthState.FAILED,
                                  f"{counts['failed']} of {total} lineup job(s) failed for {round_ctx.label}.",
                                  count=total, link=link)
        if counts.get("skipped"):
            return DatasetHealth("rosters", label, HealthState.ATTENTION,
                                  f"{counts['skipped']} of {total} lineup job(s) were skipped for {round_ctx.label}.",
                                  count=total, link=link)
        if counts.get("pending") and not counts.get("succeeded"):
            return DatasetHealth("rosters", label, HealthState.UPCOMING,
                                  f"Lineup collection for {round_ctx.label} is scheduled but not yet due.",
                                  count=total, link=link)
        succeeded = counts.get("succeeded", 0)
        return DatasetHealth("rosters", label, HealthState.HEALTHY,
                              f"{succeeded} of {total} lineup job(s) for {round_ctx.label} succeeded.",
                              count=total, link=link)

    def _lifecycle_scoped_dataset(self, key: str, label: str, round_ctx: RoundContext, table: str) -> DatasetHealth:
        """Lifecycle-aware completeness for a per-match production table.

        ``table`` must be a checked-in table name (never user input): one of
        ``match_commentary_events`` or ``match_interchange_state``. A single
        aggregate query per round, never a per-match loop.
        """
        assert table in {"match_commentary_events", "match_interchange_state"}
        rows = self.conn.execute(
            f"SELECT m.match_id, m.status, COUNT(t.id) AS event_count "
            f"FROM matches m LEFT JOIN {table} t ON t.match_id = m.match_id "
            "WHERE m.round_id = ? GROUP BY m.match_id",
            (round_ctx.round_id,),
        ).fetchall()
        link = "/schedule"
        if not rows:
            return DatasetHealth(key, label, HealthState.UNKNOWN, f"No matches found for {round_ctx.label}.", link=link)

        concluded = concluded_with_data = live_or_post = live_or_post_with_data = 0
        for row in rows:
            lifecycle = normalise_match_status(row["status"])
            has_data = (row["event_count"] or 0) > 0
            if lifecycle == "CONCLUDED":
                concluded += 1
                concluded_with_data += int(has_data)
            elif lifecycle in ("LIVE", "POSTGAME"):
                live_or_post += 1
                live_or_post_with_data += int(has_data)

        if concluded:
            state = HealthState.HEALTHY if concluded_with_data == concluded else HealthState.ATTENTION
            return DatasetHealth(key, label, state,
                                  f"{concluded_with_data}/{concluded} concluded match(es) in {round_ctx.label} "
                                  f"have {label.lower()} data.", count=concluded_with_data, link=link)
        if live_or_post:
            state = HealthState.HEALTHY if live_or_post_with_data else HealthState.UNKNOWN
            return DatasetHealth(key, label, state,
                                  f"{live_or_post_with_data}/{live_or_post} in-progress match(es) in "
                                  f"{round_ctx.label} have {label.lower()} data so far.",
                                  count=live_or_post_with_data, link=link)
        return DatasetHealth(key, label, HealthState.UPCOMING,
                              f"{round_ctx.label} has not started; {label.lower()} is not yet expected.",
                              count=0, link=link)

    def _injuries_dataset(self, now: datetime) -> DatasetHealth:
        row = self.conn.execute("SELECT MAX(scraped_at) AS latest, COUNT(*) AS total FROM injuries WHERE current = 1").fetchone()
        latest = _parse_utc(row["latest"]) if row else None
        total = row["total"] if row else 0
        registry = self.conn.execute(
            "SELECT status, last_success_time, last_error_summary FROM scheduler_job_registry "
            "WHERE job_id = 'injuries_daily'"
        ).fetchone()
        last_success = _parse_utc(registry["last_success_time"]) if registry else None
        # A successful scrape that authoritatively finds zero current injuries
        # retires every previously-current row (observed-team authority; see
        # scraper/injuries/persistence.py) -- an empty `injuries` table is
        # then a valid, fresh result, not evidence of missing collection.
        # Fall back to the job's own last_success_time as freshness evidence
        # in that specific case.
        fresh_empty_result = total == 0 and latest is None and last_success is not None
        freshness = latest if latest is not None else (last_success if fresh_empty_result else None)
        link = "/table/injuries"
        if freshness is None:
            state = HealthState.MISSING if total == 0 else HealthState.UNKNOWN
            summary = ("No current injury records are persisted and no successful scheduled refresh is on record."
                       if total == 0 else f"{total} current injury record(s); update time unavailable.")
        else:
            age = now - freshness
            if age <= INJURIES_STALE_AFTER:
                state = HealthState.HEALTHY
            elif age <= INJURIES_MISSING_AFTER:
                state = HealthState.STALE
            else:
                state = HealthState.MISSING
            if fresh_empty_result:
                summary = f"0 current injury record(s); last successful refresh {format_age(age)} authoritatively reported none."
            else:
                summary = f"{total} current injury record(s); last updated {format_age(age)}."
        if registry is not None and registry["status"] == "failed" and state in (HealthState.HEALTHY, HealthState.STALE):
            state = HealthState.FAILED
            summary += f" Last scheduled refresh failed: {registry['last_error_summary'] or 'unknown error'}."
        return DatasetHealth("injuries", "Injuries", state, summary, count=total, last_observed_utc=_iso(freshness), link=link)

    @staticmethod
    def _dataset_attention(datasets: list[DatasetHealth]) -> list[AttentionItem]:
        items = []
        for dataset in datasets:
            if dataset.state in (HealthState.FAILED, HealthState.ATTENTION, HealthState.STALE, HealthState.MISSING):
                severity = Severity.ERROR if dataset.state is HealthState.FAILED else Severity.WARNING
                items.append(AttentionItem(severity, "dataset", f"dataset.{dataset.key}", dataset.summary, link=dataset.link))
        return items

    # -- scheduler / collector activity --------------------------------------

    def _scheduler_activity(self) -> list[SchedulerJobTypeActivity]:
        results = []
        for row in job_type_activity_summary(self.conn):
            total, failed = row["total"] or 0, row["failed"] or 0
            pending, running = row["pending"] or 0, row["running"] or 0
            succeeded, skipped = row["succeeded"] or 0, row["skipped"] or 0
            if failed:
                state = HealthState.FAILED
            elif skipped and not succeeded:
                state = HealthState.ATTENTION
            elif succeeded or running or pending:
                state = HealthState.HEALTHY
            else:
                state = HealthState.UNKNOWN
            job_type = row["job_type"]
            label = _JOB_TYPE_LABELS.get(job_type, job_type.replace("_", " ").title())
            results.append(SchedulerJobTypeActivity(
                job_type=job_type, label=label, state=state, total=total, pending=pending,
                running=running, succeeded=succeeded, failed=failed, skipped=skipped,
                last_success_utc=_iso(_parse_utc(row["last_success_time"])),
                last_attempt_utc=_iso(_parse_utc(row["last_attempt_time"])),
                next_scheduled_utc=_iso(_parse_utc(row["next_scheduled_run_time"])),
            ))
        return results

    @staticmethod
    def _scheduler_attention(scheduler_activity: list[SchedulerJobTypeActivity]) -> list[AttentionItem]:
        items = []
        for row in scheduler_activity:
            if row.failed:
                items.append(AttentionItem(
                    Severity.WARNING, "scheduler", f"scheduler.job_type_failed:{row.job_type}",
                    f"{row.failed} of {row.total} '{row.label}' job(s) are in a failed state.", link="/schedule",
                ))
        return items

    def _match_window_activity(self, season: SeasonContext | None) -> list[MatchWindowActivity]:
        season_id = str(season.season_id) if season is not None else None
        rows = match_window_status_summary(self.conn, season_id=season_id)
        return [MatchWindowActivity(
            status=row["status"], count=row["count"] or 0, failing=row["failing"] or 0,
            last_success_utc=_iso(_parse_utc(row["last_success_at"])), next_due_utc=_iso(_parse_utc(row["next_due_at"])),
        ) for row in rows]

    @staticmethod
    def _diagnostic_profiles() -> dict[str, Any]:
        """Diagnostic evidence-capture profile status (Issue #148 and beyond).

        Registration is process-local module-import state (see
        ``diagnostics/profiles/__init__.py``), not a scheduler runtime call,
        so this reads it directly rather than making an HTTP request to the
        scheduler service -- mirroring ``GET /scheduler/diagnostics``.
        """
        import diagnostics.profiles  # noqa: F401 - ensure checked-in profiles are registered
        from diagnostics.framework import diagnostics_enabled, registered_profiles

        return {
            "enabled": diagnostics_enabled(),
            "profiles": [profile.status() for profile in registered_profiles().values()],
        }

    # -- overall state --------------------------------------------------

    @staticmethod
    def _last_successful_collection(
        scheduler_activity: list[SchedulerJobTypeActivity], match_window_activity: list[MatchWindowActivity],
    ) -> str | None:
        candidates = [row.last_success_utc for row in scheduler_activity if row.last_success_utc]
        candidates += [row.last_success_utc for row in match_window_activity if row.last_success_utc]
        return max(candidates) if candidates else None

    @staticmethod
    def _overall_state(
        database_state: HealthState, scheduler_state: HealthState,
        datasets: list[DatasetHealth], attention: list[AttentionItem],
    ) -> HealthState:
        """Explicit precedence table, not a weighted score (Issue #225)."""
        if database_state is HealthState.FAILED or scheduler_state is HealthState.FAILED:
            return HealthState.FAILED
        if any(item.severity is Severity.ERROR for item in attention):
            return HealthState.FAILED
        if any(dataset.state is HealthState.FAILED for dataset in datasets):
            return HealthState.FAILED
        if scheduler_state is HealthState.ATTENTION:
            return HealthState.ATTENTION
        if any(dataset.state in (HealthState.ATTENTION, HealthState.STALE, HealthState.MISSING) for dataset in datasets):
            return HealthState.ATTENTION
        if any(item.severity is Severity.WARNING for item in attention):
            return HealthState.ATTENTION
        if any(dataset.state is HealthState.PARTIAL for dataset in datasets):
            return HealthState.PARTIAL
        if (any(dataset.state is HealthState.UNKNOWN for dataset in datasets)
                or scheduler_state is HealthState.UNKNOWN or database_state is HealthState.UNKNOWN):
            return HealthState.UNKNOWN
        return HealthState.HEALTHY
