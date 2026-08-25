"""Operator report over modular analytics observations (Issue #205).

Reads ``analytics_upstream_polls``/``analytics_consumer_requests`` (bounded
recent detail) plus their daily rollups and prints a polls/changes/errors
table per upstream resource, and a requests/status table per consumer
route -- the first operator-facing report over the analytics framework
described in ``docs/analytics_framework.md``.

This is a read-only report: it never talks to AFL/CFS and never writes to
the database. See that document for the full architecture, and
``analytics/reporting.py`` for the shared reporter class this script and
the optional Admin analytics page both call.

Usage:
    python -m scripts.report_analytics [--since YYYY-MM-DD] [--until YYYY-MM-DD]
        [--resource NAME] [--lifecycle-state LIVE] [--by-lifecycle]
        [--match-id ID] [--json]
"""
from __future__ import annotations

import argparse
import json

from analytics.reporting import AnalyticsReport, AnalyticsReporter, ConsumerRouteSummary, UpstreamResourceSummary
from db.connection import get_read_only_db_connection


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _print_upstream(rows: list[UpstreamResourceSummary]) -> None:
    if not rows:
        print("(no upstream poll observations in range)")
        return
    header = f"{'Resource':<24}{'Lifecycle':<12}{'Polls':>8}{'Changes':>9}{'Unchanged':>11}{'Errors':>8}{'Polls/change':>14}{'Avg ms':>9}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row.display_name:<24}{(row.lifecycle_state or 'ALL'):<12}{row.polls:>8}{row.changed:>9}"
            f"{row.unchanged:>11}{row.failures:>8}{_fmt(row.polls_per_change):>14}{_fmt(row.avg_duration_ms, 1):>9}"
        )


def _print_consumer(rows: list[ConsumerRouteSummary]) -> None:
    if not rows:
        return
    print("\nConsumer API requests")
    header = f"{'Route':<48}{'Requests':>10}{'2xx':>8}{'4xx':>8}{'5xx':>8}{'Avg ms':>9}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row.display_name:<48}{row.requests:>10}{row.status_2xx:>8}{row.status_4xx:>8}"
            f"{row.status_5xx:>8}{_fmt(row.avg_duration_ms, 1):>9}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", dest="since_date", default=None, help="UTC date (YYYY-MM-DD), inclusive")
    parser.add_argument("--until", dest="until_date", default=None, help="UTC date (YYYY-MM-DD), inclusive")
    parser.add_argument("--resource", default=None, help="Filter to one upstream resource identifier")
    parser.add_argument("--lifecycle-state", default=None, help="Filter to one match lifecycle state")
    parser.add_argument("--by-lifecycle", action="store_true", help="Break upstream rows out by lifecycle state")
    parser.add_argument("--match-id", type=int, default=None, help="Report on one match only (raw detail, retention window)")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a human-readable report")
    args = parser.parse_args(argv)

    conn = get_read_only_db_connection()
    try:
        reporter = AnalyticsReporter(conn)
        report: AnalyticsReport = reporter.report(
            since_date=args.since_date, until_date=args.until_date, match_id=args.match_id,
            resource=args.resource, lifecycle_state=args.lifecycle_state, group_by_lifecycle=args.by_lifecycle,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 0

    print(f"Analytics report generated_at={report.generated_at} filters={report.filters or '{}'}")
    _print_upstream(report.upstream)
    _print_consumer(report.consumer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
