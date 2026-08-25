"""Read-only analytics reporting (Issue #205).

Shared, side-effect-free query logic used by both
``scripts/report_analytics.py`` (CLI) and the optional Admin analytics page
-- mirroring how ``afl_json.season_report.SeasonCompletenessReporter``
backs both ``cli.py --report-afl-season`` and Admin's Season Review page, so
neither surface duplicates the SQL.

Two data sources are combined transparently for resource/route-level
reports: the bounded raw tables (``analytics_upstream_polls`` /
``analytics_consumer_requests``, recent detail only -- see
``analytics/rollup.py``) and the daily rollups that survive after raw rows
age out. Match-level reports are raw-only by necessity: rollups intentionally
do not retain ``match_id`` (see the migration docstring), so a match's detail
is only queryable within the retention window. This is a deliberate
retention trade-off, not an oversight -- document it to a caller rather than
silently returning an empty match report once data has rolled off.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from analytics.contracts import RESOURCE_REGISTRY, ROUTE_REGISTRY


@dataclass(frozen=True, slots=True)
class UpstreamResourceSummary:
    resource: str
    lifecycle_state: str | None
    polls: int
    successes: int
    changed: int
    unchanged: int
    failures: int
    avg_duration_ms: float | None
    polls_per_change: float | None

    @property
    def display_name(self) -> str:
        info = RESOURCE_REGISTRY.get(self.resource)
        return info.display_name if info else self.resource


@dataclass(frozen=True, slots=True)
class ConsumerRouteSummary:
    route: str
    requests: int
    status_2xx: int
    status_4xx: int
    status_5xx: int
    avg_duration_ms: float | None

    @property
    def display_name(self) -> str:
        info = ROUTE_REGISTRY.get(self.route)
        return info.display_name if info else self.route


@dataclass(slots=True)
class AnalyticsReport:
    generated_at: str
    filters: dict[str, str]
    upstream: list[UpstreamResourceSummary] = field(default_factory=list)
    consumer: list[ConsumerRouteSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at, "filters": self.filters,
            "upstream": [asdict(row) for row in self.upstream],
            "consumer": [asdict(row) for row in self.consumer],
        }


def _row_to_upstream_summary(row: sqlite3.Row) -> UpstreamResourceSummary:
    polls = row["polls"] or 0
    changed = row["changed"] or 0
    total_duration_ms = row["total_duration_ms"] or 0
    return UpstreamResourceSummary(
        resource=row["resource"], lifecycle_state=row["lifecycle_state"], polls=polls,
        successes=row["successes"] or 0, changed=changed, unchanged=row["unchanged"] or 0,
        failures=row["failures"] or 0,
        avg_duration_ms=(total_duration_ms / polls) if polls else None,
        polls_per_change=(polls / changed) if changed else None,
    )


class AnalyticsReporter:
    """Gather aggregated analytics evidence without mutating the connection."""

    def __init__(self, conn: sqlite3.Connection, *, clock=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def resource_summary(self, *, since_date: str | None = None, until_date: str | None = None,
                         resource: str | None = None, lifecycle_state: str | None = None,
                         group_by_lifecycle: bool = False) -> list[UpstreamResourceSummary]:
        """Aggregate upstream polls by resource (optionally also by lifecycle state).

        Combines bounded raw detail with older daily rollups so the result is
        correct regardless of whether the window falls inside or outside the
        retention period -- see module docstring.
        """
        lifecycle_column = "lifecycle_state" if group_by_lifecycle else "'ALL' AS lifecycle_state"
        group_clause = "resource, lifecycle_state" if group_by_lifecycle else "resource"
        query = f"""
            WITH combined AS (
                SELECT resource, lifecycle_state,
                       1 AS polls,
                       CASE WHEN outcome='success' THEN 1 ELSE 0 END AS successes,
                       CASE WHEN outcome='success' AND changed=1 THEN 1 ELSE 0 END AS changed,
                       CASE WHEN outcome='success' AND changed=0 THEN 1 ELSE 0 END AS unchanged,
                       CASE WHEN outcome!='success' THEN 1 ELSE 0 END AS failures,
                       duration_ms AS total_duration_ms
                FROM analytics_upstream_polls
                WHERE observation_date >= COALESCE(:since, observation_date)
                  AND observation_date <= COALESCE(:until, observation_date)
                  AND (:resource IS NULL OR resource = :resource)
                  AND (:lifecycle IS NULL OR lifecycle_state = :lifecycle)
                UNION ALL
                SELECT resource, lifecycle_state, polls, successes, changed, unchanged, failures, total_duration_ms
                FROM analytics_upstream_daily_rollups
                WHERE observation_date >= COALESCE(:since, observation_date)
                  AND observation_date <= COALESCE(:until, observation_date)
                  AND (:resource IS NULL OR resource = :resource)
                  AND (:lifecycle IS NULL OR lifecycle_state = :lifecycle)
            )
            SELECT resource, {lifecycle_column},
                   SUM(polls) AS polls, SUM(successes) AS successes, SUM(changed) AS changed,
                   SUM(unchanged) AS unchanged, SUM(failures) AS failures,
                   SUM(total_duration_ms) AS total_duration_ms
            FROM combined
            GROUP BY {group_clause}
            ORDER BY resource, lifecycle_state
        """
        rows = self.conn.execute(
            query,
            {"since": since_date, "until": until_date, "resource": resource, "lifecycle": lifecycle_state},
        ).fetchall()
        return [_row_to_upstream_summary(row) for row in rows]

    def match_summary(self, match_id: int) -> list[UpstreamResourceSummary]:
        """Per-resource summary for one match. Raw detail only -- see module docstring."""
        rows = self.conn.execute(
            """
            SELECT resource, 'ALL' AS lifecycle_state,
                   COUNT(*) AS polls,
                   SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS successes,
                   SUM(CASE WHEN outcome='success' AND changed=1 THEN 1 ELSE 0 END) AS changed,
                   SUM(CASE WHEN outcome='success' AND changed=0 THEN 1 ELSE 0 END) AS unchanged,
                   SUM(CASE WHEN outcome!='success' THEN 1 ELSE 0 END) AS failures,
                   SUM(duration_ms) AS total_duration_ms
            FROM analytics_upstream_polls
            WHERE match_id = ?
            GROUP BY resource
            ORDER BY resource
            """,
            (match_id,),
        ).fetchall()
        return [_row_to_upstream_summary(row) for row in rows]

    def consumer_summary(self, *, since_date: str | None = None,
                         until_date: str | None = None) -> list[ConsumerRouteSummary]:
        query = """
            WITH combined AS (
                SELECT route, 1 AS requests,
                       CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END AS status_2xx,
                       CASE WHEN status_code BETWEEN 400 AND 499 THEN 1 ELSE 0 END AS status_4xx,
                       CASE WHEN status_code >= 500 THEN 1 ELSE 0 END AS status_5xx,
                       duration_ms AS total_duration_ms
                FROM analytics_consumer_requests
                WHERE observation_date >= COALESCE(:since, observation_date)
                  AND observation_date <= COALESCE(:until, observation_date)
                UNION ALL
                SELECT route, requests, status_2xx, status_4xx, status_5xx, total_duration_ms
                FROM analytics_consumer_daily_rollups
                WHERE observation_date >= COALESCE(:since, observation_date)
                  AND observation_date <= COALESCE(:until, observation_date)
            )
            SELECT route, SUM(requests) AS requests, SUM(status_2xx) AS status_2xx,
                   SUM(status_4xx) AS status_4xx, SUM(status_5xx) AS status_5xx,
                   SUM(total_duration_ms) AS total_duration_ms
            FROM combined
            GROUP BY route
            ORDER BY route
        """
        rows = self.conn.execute(query, {"since": since_date, "until": until_date}).fetchall()
        summaries = []
        for row in rows:
            requests = row["requests"] or 0
            total_duration_ms = row["total_duration_ms"] or 0
            summaries.append(ConsumerRouteSummary(
                route=row["route"], requests=requests, status_2xx=row["status_2xx"] or 0,
                status_4xx=row["status_4xx"] or 0, status_5xx=row["status_5xx"] or 0,
                avg_duration_ms=(total_duration_ms / requests) if requests else None,
            ))
        return summaries

    def report(self, *, since_date: str | None = None, until_date: str | None = None,
              match_id: int | None = None, resource: str | None = None,
              lifecycle_state: str | None = None, group_by_lifecycle: bool = False) -> AnalyticsReport:
        generated_at = self.clock().astimezone(timezone.utc).isoformat()
        filters = {
            k: v for k, v in {
                "since_date": since_date, "until_date": until_date, "match_id": str(match_id) if match_id else None,
                "resource": resource, "lifecycle_state": lifecycle_state,
            }.items() if v is not None
        }
        upstream = (self.match_summary(match_id) if match_id is not None else
                   self.resource_summary(since_date=since_date, until_date=until_date, resource=resource,
                                         lifecycle_state=lifecycle_state, group_by_lifecycle=group_by_lifecycle))
        consumer = self.consumer_summary(since_date=since_date, until_date=until_date) if match_id is None else []
        return AnalyticsReport(generated_at=generated_at, filters=filters, upstream=upstream, consumer=consumer)
