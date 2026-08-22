"""Operator report over diagnostic matchInterchange evidence captured for Issue #193.

Reads the (opt-in) ``match_interchange_evidence_observations`` table and
prints, per match, the sequence of detected transitions so an operator can
manually assess how ``homeInterchange[]``/``awayInterchange[]`` entries,
``interchangeCount``, ``benchReason`` and the team-level interchange-count
totals behave during a live match, around quarter breaks, and around
POSTGAME/CONCLUDED.

By default this suppresses the noisy, continuously-changing
timeOnGround/timeOnBench-only transitions so the report stays readable even
though those fields are still persisted on every poll where they change --
pass --verbose to see every recorded transition flag, including those. Use
``--match-id``/``--match-provider-id`` alongside the existing
``report_match_state_evidence.py`` filters (same options) to correlate
timestamps between the two independent diagnostic streams for the same match.

This is a read-only report over already-collected evidence: it never talks
to AFL/CFS and never draws conclusions about production interchange
semantics -- in particular it never asserts that an interchange-array entry
means a player is on the bench. See docs/ for the AFL-api operator CLI; this
script is invoked directly and is not wired into cli.py because it is
diagnostic-only tooling.

Usage:
    python -m scripts.report_interchange_evidence [--match-id ID]
        [--match-provider-id CD_M...] [--transitions-only] [--verbose] [--json]
"""
from __future__ import annotations

import argparse
import json

from db.connection import get_read_only_db_connection
from collection.match_interchange_evidence import NOISY_TRANSITIONS, evidence_rows


def _meaningful_flags(flags: list[str]) -> list[str]:
    return [flag for flag in flags if flag not in NOISY_TRANSITIONS]


def _print_match_report(match_provider_id: str, rows: list[dict], *, verbose: bool) -> None:
    print(f"\n=== {match_provider_id} ({len(rows)} observation(s)) ===")
    print(f"first observed_at={rows[0]['observed_at']} last observed_at={rows[-1]['observed_at']}")
    transitions = [row for row in rows if row["is_transition"]]
    if not verbose and not transitions:
        print("  (no meaningful transitions recorded)")
        return
    for row in rows:
        flags = row["transition_flags"]
        display_flags = flags if verbose else _meaningful_flags(flags)
        if not display_flags:
            continue
        home_total = row["home_counts"].get("totalInterchangeCount") if row["home_counts"] is not None else "unknown"
        away_total = row["away_counts"].get("totalInterchangeCount") if row["away_counts"] is not None else "unknown"
        home_players = len(row["home_interchange"]) if row["home_interchange"] is not None else "unknown"
        away_players = len(row["away_interchange"]) if row["away_interchange"] is not None else "unknown"
        print(
            f"  seq={row['poll_sequence']:>4} at={row['observed_at']} "
            f"flags={','.join(display_flags)} "
            f"match_status_at_poll={row['match_status_at_poll']} "
            f"home_total={home_total} away_total={away_total} "
            f"home_players={home_players} away_players={away_players}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match-id", type=int, default=None, help="Filter to one internal match_id")
    parser.add_argument("--match-provider-id", default=None, help="Filter to one CD_M... provider ID")
    parser.add_argument(
        "--transitions-only", action="store_true",
        help="Only load rows where at least one meaningful transition was detected",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Also show noisy timeOnGround/timeOnBench-only transitions, suppressed by default",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a human-readable report")
    args = parser.parse_args(argv)

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
            "No match-interchange evidence has been captured yet. Set AFL_DIAGNOSTICS_ENABLED=true "
            "and AFL_DIAGNOSTIC_PROFILES=interchange (or match_clock,interchange), then restart the "
            "scheduler during a live match."
        )
        return 0

    by_match: dict[str, list[dict]] = {}
    for row in rows:
        by_match.setdefault(row["match_provider_id"], []).append(row)
    for match_provider_id, match_rows in by_match.items():
        _print_match_report(match_provider_id, match_rows, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
