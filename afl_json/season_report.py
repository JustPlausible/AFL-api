"""Deterministic, read-only completeness reporting for a persisted AFL season."""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .match_status import normalise_match_status


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ReportStatus(str, Enum):
    COMPLETE = "complete"
    USABLE_WITH_WARNINGS = "usable_with_warnings"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"


INCOMPLETE_CODES = frozenset({
    "season.missing", "season.no_teams", "season.no_rounds", "season.no_matches",
    "match.final_without_authoritative_stats", "match.missing_provider_id",
})


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Severity
    domain: str
    message: str
    season_id: int | None = None
    match_id: int | None = None
    team_id: int | None = None
    player_id: int | None = None
    expected: object | None = None
    observed: object | None = None
    evidence_source: str | None = None
    remediation: str | None = None


@dataclass(frozen=True, slots=True)
class ReportMetadata:
    requested_season_year: int
    competition_code: str
    competition_provider_id: str
    competition_id: int | None
    competition_season_id: int | None
    competition_season_provider_id: str | None
    generated_at: str
    database: str | None
    filters: dict[str, str]


@dataclass(slots=True)
class SeasonReport:
    metadata: ReportMetadata
    aggregates: dict[str, object]
    findings: list[Finding] = field(default_factory=list)
    status: ReportStatus = ReportStatus.COMPLETE

    @property
    def severity_counts(self) -> dict[str, int]:
        counts = Counter(item.severity.value for item in self.findings)
        return {severity.value: counts[severity.value] for severity in Severity}

    def to_dict(self) -> dict[str, object]:
        return {
            "metadata": asdict(self.metadata), "aggregates": self.aggregates,
            "status": self.status.value, "severity_counts": self.severity_counts,
            "finding_count": len(self.findings),
            "findings": [{**asdict(item), "severity": item.severity.value}
                         for item in self.findings],
        }


def calculate_status(findings: list[Finding]) -> ReportStatus:
    """Apply the explicit status decision table (not generic severity ordering)."""
    if any(item.severity is Severity.ERROR for item in findings):
        return ReportStatus.INVALID
    if any(item.code in INCOMPLETE_CODES and item.severity is not Severity.INFO
           for item in findings):
        return ReportStatus.INCOMPLETE
    if any(item.severity is Severity.WARNING for item in findings):
        return ReportStatus.USABLE_WITH_WARNINGS
    return ReportStatus.COMPLETE


def exit_code(status: ReportStatus) -> int:
    """Warnings remain usable (0); missing/unsafe season data exits 1."""
    return 0 if status in {ReportStatus.COMPLETE, ReportStatus.USABLE_WITH_WARNINGS} else 1


class SeasonCompletenessReporter:
    """Gather set-based evidence and evaluate it without changing the connection."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Callable[[], datetime] | None = None,
                 database: str | Path | None = None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.database = str(database) if database is not None else None

    def report(self, year: int, *, competition_code: str = "AFL",
               competition_provider_id: str = "CD_C014") -> SeasonReport:
        generated = self.clock().astimezone(timezone.utc).isoformat()
        competitions = self.conn.execute(
            "SELECT afl_id,provider_id,code FROM afl_competitions "
            "WHERE code=? OR provider_id=? ORDER BY afl_id", (competition_code, competition_provider_id)
        ).fetchall()
        exact = [row for row in competitions if row["code"] == competition_code
                 and row["provider_id"] == competition_provider_id]
        competition = exact[0] if len(exact) == 1 else None
        seasons = [] if competition is None else self.conn.execute(
            "SELECT afl_id,provider_id FROM afl_seasons WHERE competition_id=? AND year=? "
            "ORDER BY afl_id", (competition["afl_id"], year)
        ).fetchall()
        season = seasons[0] if len(seasons) == 1 else None
        metadata = ReportMetadata(
            year, competition_code, competition_provider_id,
            competition["afl_id"] if competition else None,
            season["afl_id"] if season else None,
            season["provider_id"] if season else None, generated, self.database,
            {"scope": "full_season"},
        )
        result = SeasonReport(metadata, self._empty_aggregates())
        if len(exact) > 1:
            result.findings.append(Finding(
                "competition.duplicate_identity", Severity.ERROR, "foundations",
                "Stable AFL competition identity resolves to multiple records.",
                expected=1, observed=len(exact), evidence_source="afl_competitions(code,provider_id)"))
        elif competition is None:
            result.findings.append(Finding(
                "competition.missing", Severity.ERROR, "foundations",
                "Stable AFL competition identity could not be resolved.", expected=1,
                observed=0, evidence_source="afl_competitions(code,provider_id)",
                remediation="python cli.py --bootstrap-afl-season YEAR"))
        elif len(seasons) > 1:
            result.findings.append(Finding(
                "season.duplicate_identity", Severity.ERROR, "foundations",
                "Competition and year resolve to multiple seasons.", expected=1,
                observed=len(seasons), evidence_source="afl_seasons(competition_id,year)"))
        elif season is None:
            result.findings.append(Finding(
                "season.missing", Severity.WARNING, "foundations",
                "Requested AFL competition season is not persisted.", expected=1,
                observed=0, evidence_source="afl_seasons(competition_id,year)",
                remediation=f"python cli.py --bootstrap-afl-season {year}"))
        else:
            self._gather(result, season["afl_id"])
        result.findings.sort(key=lambda item: (
            item.domain, item.code, item.match_id or -1, item.player_id or -1,
            item.team_id or -1))
        result.status = calculate_status(result.findings)
        return result

    @staticmethod
    def _empty_aggregates() -> dict[str, object]:
        return {
            "teams": 0, "rounds": 0, "matches": 0, "matches_by_status": {},
            "concluded_matches": 0, "concluded_matches_with_authoritative_stats": 0,
            "authoritative_stat_rows": 0, "legacy_stat_rows": 0,
            "season_memberships": 0, "memberships_without_team": 0,
        }

    def _add(self, result: SeasonReport, code: str, severity: Severity, domain: str,
             message: str, **values: Any) -> None:
        result.findings.append(Finding(code, severity, domain, message,
                                       season_id=result.metadata.competition_season_id, **values))

    def _gather(self, result: SeasonReport, season_id: int) -> None:
        teams = self.conn.execute(
            "SELECT COUNT(*) FROM afl_team_seasons ts JOIN afl_teams t ON t.afl_id=ts.team_id "
            "WHERE ts.competition_season_id=? AND t.season_id=?", (season_id, season_id)
        ).fetchone()[0]
        rounds = self.conn.execute("SELECT COUNT(*) FROM rounds WHERE season_id=?", (season_id,)).fetchone()[0]
        match_rows = self.conn.execute(
            "SELECT m.match_id,m.match_provider_id,m.round_id,m.home_team_id,m.away_team_id,"
            "m.start_time_utc,m.venue,m.status,r.season_id AS round_season,"
            "hts.team_id AS valid_home,ats.team_id AS valid_away "
            "FROM matches m LEFT JOIN rounds r ON r.round_id=m.round_id "
            "LEFT JOIN afl_team_seasons hts ON hts.competition_season_id=? AND hts.team_id=m.home_team_id "
            "LEFT JOIN afl_team_seasons ats ON ats.competition_season_id=? AND ats.team_id=m.away_team_id "
            "WHERE m.season_id=? ORDER BY m.match_id", (season_id, season_id, season_id)
        ).fetchall()
        statuses = Counter(normalise_match_status(row["status"]) or "UNKNOWN" for row in match_rows)
        concluded = [row for row in match_rows if normalise_match_status(row["status"]) == "CONCLUDED"]
        provider_ids = [row["match_provider_id"] for row in match_rows if row["match_provider_id"]]
        placeholders = ",".join("?" for _ in provider_ids)
        stats = [] if not provider_ids else self.conn.execute(
            "SELECT match_provider_id,COUNT(*) rows,COUNT(DISTINCT side) sides,"
            "MIN(snapshot_authority) min_authority,MAX(snapshot_authority) max_authority,"
            "SUM(canonical_player_id IS NULL) unresolved FROM cfs_player_stats "
            f"WHERE match_provider_id IN ({placeholders}) GROUP BY match_provider_id", provider_ids
        ).fetchall()
        stats_by_match = {row["match_provider_id"]: row for row in stats}
        authoritative = {key: row for key, row in stats_by_match.items() if row["max_authority"] == 2}
        memberships = self.conn.execute(
            "SELECT COUNT(*) total,SUM(team_id IS NULL) missing FROM competition_season_players "
            "WHERE competition_season_id=?", (season_id,)
        ).fetchone()
        legacy = self.conn.execute(
            "SELECT COUNT(*) FROM player_stats ps JOIN matches m ON m.match_id=ps.match_id "
            "WHERE m.season_id=?", (season_id,)
        ).fetchone()[0]
        result.aggregates.update({
            "teams": teams, "rounds": rounds, "matches": len(match_rows),
            "matches_by_status": dict(sorted(statuses.items())),
            "concluded_matches": len(concluded),
            "concluded_matches_with_authoritative_stats": sum(
                row["match_provider_id"] in authoritative for row in concluded),
            "authoritative_stat_rows": sum(row["rows"] for row in authoritative.values()),
            "legacy_stat_rows": legacy, "season_memberships": memberships["total"],
            "memberships_without_team": memberships["missing"] or 0,
        })
        if not teams:
            self._add(result, "season.no_teams", Severity.WARNING, "foundations",
                      "Season has no valid participating teams.", expected=">0", observed=0,
                      evidence_source="afl_team_seasons JOIN afl_teams")
        if not rounds:
            self._add(result, "season.no_rounds", Severity.WARNING, "foundations",
                      "Season has no rounds.", expected=">0", observed=0, evidence_source="rounds")
        if not match_rows:
            self._add(result, "season.no_matches", Severity.WARNING, "foundations",
                      "Season has no matches.", expected=">0", observed=0, evidence_source="matches")
        for row in match_rows:
            lifecycle = normalise_match_status(row["status"])
            if row["round_season"] != season_id:
                self._add(result, "match.missing_round", Severity.ERROR, "matches",
                          "Match round is absent or belongs to another season.", match_id=row["match_id"],
                          expected=season_id, observed=row["round_season"], evidence_source="matches JOIN rounds")
            if row["valid_home"] is None or row["valid_away"] is None:
                self._add(result, "match.missing_team", Severity.ERROR, "matches",
                          "Home or away team is absent from season participation.", match_id=row["match_id"],
                          expected="two season teams", observed="invalid relationship",
                          evidence_source="matches JOIN afl_team_seasons")
            if not row["match_provider_id"]:
                severity = Severity.WARNING if lifecycle == "CONCLUDED" else Severity.INFO
                self._add(result, "match.missing_provider_id", severity, "matches",
                          "Match has no Champion Data provider identity.", match_id=row["match_id"],
                          expected="CD_M...", observed=None, evidence_source="matches.match_provider_id")
            if not row["start_time_utc"] or not row["venue"]:
                self._add(result, "match.optional_fixture_detail_missing", Severity.INFO, "matches",
                          "Scheduled time or venue is not published.", match_id=row["match_id"],
                          evidence_source="matches(start_time_utc,venue)")
            stat = stats_by_match.get(row["match_provider_id"])
            if lifecycle == "CONCLUDED" and (not stat or stat["max_authority"] != 2):
                self._add(result, "match.final_without_authoritative_stats", Severity.WARNING, "matches",
                          "Concluded match has no authoritative CFS snapshot.", match_id=row["match_id"],
                          expected="snapshot_authority=2", observed=stat["max_authority"] if stat else 0,
                          evidence_source="matches LEFT JOIN cfs_player_stats",
                          remediation=(f"python cli.py --collect-match-player-stats {row['match_provider_id']} "
                                       f"--afl-match-id {row['match_id']}" if row["match_provider_id"] else
                                       f"python cli.py --sync-afl-season {result.metadata.requested_season_year}"))
            elif stat and (stat["min_authority"] != stat["max_authority"] or stat["sides"] < 2):
                self._add(result, "match.partial_authoritative_stats", Severity.WARNING, "statistics",
                          "Statistic snapshot is mixed-authority or one-sided.", match_id=row["match_id"],
                          expected="two sides at one authority", observed={"rows": stat["rows"], "sides": stat["sides"]},
                          evidence_source="cfs_player_stats")
            if lifecycle != "CONCLUDED" and stat and stat["max_authority"] == 2:
                self._add(result, "stats.authority_lifecycle_conflict", Severity.ERROR, "statistics",
                          "A non-concluded match has a concluded-authority snapshot.", match_id=row["match_id"],
                          expected="snapshot_authority=1", observed=2, evidence_source="matches JOIN cfs_player_stats")
        self._identity_checks(result, season_id, provider_ids)
        self._audit_checks(result, concluded, stats_by_match)

    def _identity_checks(self, result: SeasonReport, season_id: int,
                         provider_ids: list[str]) -> None:
        rows = self.conn.execute(
            "SELECT csp.player_id,csp.team_id,cp.display_name,cp.given_name,cp.family_name,"
            "MAX(pp.provider='afl') has_afl,MAX(pp.provider='champion_data') has_cd "
            "FROM competition_season_players csp LEFT JOIN canonical_players cp ON cp.id=csp.player_id "
            "LEFT JOIN player_provider_ids pp ON pp.player_id=csp.player_id "
            "WHERE csp.competition_season_id=? GROUP BY csp.id ORDER BY csp.player_id", (season_id,)
        ).fetchall()
        for row in rows:
            if row["display_name"] is None and not (row["given_name"] or row["family_name"]):
                self._add(result, "player.incomplete_identity", Severity.WARNING, "players",
                          "Canonical player has no usable name.", player_id=row["player_id"],
                          evidence_source="canonical_players")
            for provider, mapping_field, code in (("afl", "has_afl", "player.missing_afl_provider_id"),
                                          ("champion_data", "has_cd", "player.missing_champion_data_provider_id")):
                if not row[mapping_field]:
                    self._add(result, code, Severity.WARNING, "players",
                              f"Season player has no {provider} provider mapping.",
                              player_id=row["player_id"], expected=provider, observed=None,
                              evidence_source="competition_season_players LEFT JOIN player_provider_ids")
            if row["team_id"] is None:
                self._add(result, "membership.missing_team", Severity.INFO, "players",
                          "Season membership has no team; upstream permits unassigned players.",
                          player_id=row["player_id"], evidence_source="competition_season_players.team_id")
        unresolved_sql = ("SELECT s.id,s.canonical_player_id,s.champion_data_player_id,s.match_provider_id,"
                          "pp.player_id AS mapped_player_id FROM cfs_player_stats s LEFT JOIN player_provider_ids pp "
                          "ON pp.provider='champion_data' AND pp.provider_player_id=s.champion_data_player_id")
        params: list[object] = []
        if provider_ids:
            unresolved_sql += f" WHERE s.match_provider_id IN ({','.join('?' for _ in provider_ids)})"
            params.extend(provider_ids)
        else:
            unresolved_sql += " WHERE 0"
        for row in self.conn.execute(unresolved_sql, params):
            if row["canonical_player_id"] is None:
                self._add(result, "stats.unresolved_canonical_player", Severity.WARNING, "statistics",
                          "CFS statistic row has no canonical player crosswalk.",
                          observed=row["champion_data_player_id"], evidence_source="cfs_player_stats.canonical_player_id")
            if row["mapped_player_id"] is None:
                self._add(result, "stats.provider_player_unknown", Severity.WARNING, "statistics",
                          "CFS player identifier is absent from provider mappings.",
                          player_id=row["canonical_player_id"], observed=row["champion_data_player_id"],
                          evidence_source="cfs_player_stats LEFT JOIN player_provider_ids")
            elif (row["canonical_player_id"] is not None
                  and row["mapped_player_id"] != row["canonical_player_id"]):
                self._add(result, "player.provider_mapping_conflict", Severity.ERROR, "players",
                          "Statistic crosswalk contradicts the canonical provider mapping.",
                          player_id=row["canonical_player_id"], expected=row["mapped_player_id"],
                          observed=row["canonical_player_id"], evidence_source="cfs_player_stats JOIN player_provider_ids")
        outside = self.conn.execute(
            "SELECT DISTINCT cps.player_id,s.match_provider_id FROM competition_season_players cps "
            "JOIN cfs_player_stats s ON s.canonical_player_id=cps.player_id "
            "LEFT JOIN matches m ON m.match_provider_id=s.match_provider_id AND m.season_id=? "
            "WHERE cps.competition_season_id=? AND m.match_id IS NULL", (season_id, season_id)
        ).fetchall()
        for row in outside:
            self._add(result, "stats.match_outside_season", Severity.WARNING, "statistics",
                      "A season player's statistic row is attached outside the selected season.",
                      player_id=row["player_id"], observed=row["match_provider_id"],
                      evidence_source="competition_season_players JOIN cfs_player_stats LEFT JOIN matches")

    def _audit_checks(self, result: SeasonReport, concluded: list[sqlite3.Row],
                      stats_by_match: dict[str, sqlite3.Row]) -> None:
        provider_ids = [row["match_provider_id"] for row in concluded if row["match_provider_id"]]
        latest: dict[str, sqlite3.Row] = {}
        if provider_ids:
            audit_rows = self.conn.execute(
                "SELECT target_identifier,status,rows_read,rows_written,started_at FROM scrape_runs "
                f"WHERE target_type='match' AND target_identifier IN ({','.join('?' for _ in provider_ids)}) "
                "AND scrape_type IN ('season_match_player_stats','match_player_stats') "
                "ORDER BY target_identifier,started_at DESC", provider_ids,
            ).fetchall()
            latest = {row["target_identifier"]: row for row in reversed(audit_rows)}
        for match in concluded:
            provider_id = match["match_provider_id"]
            if not provider_id:
                continue
            audit = latest.get(provider_id)
            if audit is None:
                self._add(result, "audit.no_successful_stat_run", Severity.INFO, "audit",
                          "No reliably correlated authoritative collection audit exists.",
                          match_id=match["match_id"], evidence_source="scrape_runs")
            elif audit["status"] in {"failed", "partial", "running"}:
                self._add(result, "audit.latest_run_failed_or_partial", Severity.WARNING, "audit",
                          "Latest correlated authoritative collection did not complete.",
                          match_id=match["match_id"], expected="completed", observed=audit["status"],
                          evidence_source="scrape_runs")
            elif (audit["rows_written"] or 0) > 0 and provider_id not in stats_by_match:
                self._add(result, "audit.success_without_rows", Severity.WARNING, "audit",
                          "Successful audit claims writes but no CFS rows persist.",
                          match_id=match["match_id"], expected=audit["rows_written"], observed=0,
                          evidence_source="scrape_runs LEFT JOIN cfs_player_stats")


def render_human(report: SeasonReport) -> str:
    """Render the same structured findings exposed by :meth:`to_dict`."""
    meta = report.metadata
    lines = [
        f"AFL season completeness report {meta.requested_season_year}: {report.status.value}",
        f"competition: {meta.competition_code} ({meta.competition_provider_id}) "
        f"id={meta.competition_id} season_id={meta.competition_season_id}",
        ("counts: " + " ".join(f"{key}={value}" for key, value in report.aggregates.items()
                               if not isinstance(value, dict))),
        "severity: " + " ".join(f"{key}={value}" for key, value in report.severity_counts.items()),
    ]
    for item in report.findings:
        identifiers = " ".join(f"{key}={value}" for key, value in (
            ("match", item.match_id), ("team", item.team_id), ("player", item.player_id)
        ) if value is not None)
        lines.append(f"[{item.severity.value}] {item.code}" +
                     (f" ({identifiers})" if identifiers else "") + f": {item.message}")
    return "\n".join(lines)
