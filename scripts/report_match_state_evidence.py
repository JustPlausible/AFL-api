"""Operator report over diagnostic matchItem evidence captured for Issue #148.

Reads the (opt-in) ``match_state_evidence_observations`` table and prints,
per match, the sequence of detected transitions so an operator can manually
assess how ``score.matchClock.periods``, ``periodCompleted``, ``periodSeconds``
and ``match.status``/``score.status`` behave around quarter time, half time,
three-quarter time and full time.

This is a read-only report over already-collected evidence: it never talks
to AFL/CFS and never draws conclusions about production quarter-state
scheduling. See docs/ for the AFL-api operator CLI; this script is invoked
directly and is not wired into cli.py because it is diagnostic-only tooling.

Usage:
    python -m scripts.report_match_state_evidence [--match-id ID]
        [--match-provider-id CD_M...] [--transitions-only] [--json]

To backfill period fields on already-captured rows after a parser fix (see
``collection.match_state_evidence.reparse_stored_raw_observations``), which
only affects rows where the full raw matchItem payload was retained
(first-observation and detected-transition rows):

    python -m scripts.report_match_state_evidence --reparse-raw [--dry-run]
        [--match-id ID] [--match-provider-id CD_M...]
"""
from __future__ import annotations

import argparse
import json

from db.connection import get_db_connection, get_read_only_db_connection
from collection.match_state_evidence import evidence_rows, reparse_stored_raw_observations


def _print_match_report(match_provider_id: str, rows: list[dict]) -> None:
    print(f"\n=== {match_provider_id} ({len(rows)} observation(s)) ===")
    print(f"first observed_at={rows[0]['observed_at']} last observed_at={rows[-1]['observed_at']}")
    transitions = [row for row in rows if row["is_transition"]]
    if not transitions:
        print("  (no transitions recorded)")
        return
    for row in transitions:
        print(
            f"  seq={row['poll_sequence']:>4} at={row['observed_at']} "
            f"flags={','.join(row['transition_flags'])} "
            f"match_status={row['match_status']} score_status={row['score_status']} "
            f"latest_period={row['latest_period_number']} "
            f"periodSeconds={row['latest_period_seconds']} "
            f"periodCompleted={row['latest_period_completed']}"
        )


def _run_reparse(args: argparse.Namespace) -> int:
    conn = get_db_connection()
    try:
        results = reparse_stored_raw_observations(
            conn, match_id=args.match_id, match_provider_id=args.match_provider_id, dry_run=args.dry_run
        )
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    changed = [row for row in results if row["changed"]]
    suffix = " (dry run, no changes written)" if args.dry_run else ""
    print(f"Considered {len(results)} row(s) with retained raw evidence; {len(changed)} updated{suffix}.")
    for row in changed:
        before, after = row["before"], row["after"]
        print(
            f"  id={row['id']} match_provider_id={row['match_provider_id']} "
            f"latest_period_number: {before['latest_period_number']} -> {after['latest_period_number']} "
            f"latest_period_seconds: {before['latest_period_seconds']} -> {after['latest_period_seconds']} "
            f"latest_period_completed: {before['latest_period_completed']} -> {after['latest_period_completed']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match-id", type=int, default=None, help="Filter to one internal match_id")
    parser.add_argument("--match-provider-id", default=None, help="Filter to one CD_M... provider ID")
    parser.add_argument("--transitions-only", action="store_true", help="Only print rows where a transition was detected")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a human-readable report")
    parser.add_argument(
        "--reparse-raw", action="store_true",
        help="Re-extract period fields from stored raw_match_item_json using the current parser "
             "and update those rows in place; only affects rows where the raw payload was retained",
    )
    parser.add_argument("--dry-run", action="store_true", help="With --reparse-raw, report changes without writing them")
    args = parser.parse_args(argv)

    if args.reparse_raw:
        return _run_reparse(args)

    conn = get_read_only_db_connection()
    try:
        rows = evidence_rows(
            conn, match_id=args.match_id, match_provider_id=args.match_provider_id,
            transitions_only=args.transitions_only, limit=None,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0

    if not rows:
        print(
            "No match-state evidence has been captured yet. Set "
            "AFL_CAPTURE_MATCH_STATE_EVIDENCE=true and restart the scheduler during a live match."
        )
        return 0

    by_match: dict[str, list[dict]] = {}
    for row in rows:
        by_match.setdefault(row["match_provider_id"], []).append(row)
    for match_provider_id, match_rows in by_match.items():
        _print_match_report(match_provider_id, match_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
