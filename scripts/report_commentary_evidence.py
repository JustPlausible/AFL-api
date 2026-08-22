"""Operator report over diagnostic commentaryFeed evidence captured for Issue #196.

Reads the (opt-in) ``commentary_evidence_polls`` and ``commentary_evidence_events``
tables and prints, per match, a summary plus the evidence an operator actually
wants to eyeball during a live match: quarter start/end markers, score
events, player/team-linked commentary, any detected possible edits, and
endpoint availability/failure transitions.

Because ``commentary_evidence_events`` is already deduplicated (see
``collection.match_commentary_evidence``), the default report never repeats
the accumulated historical feed on every poll -- each captured event is
listed exactly once, at the point it was first observed. Uncategorised
narrative commentary (statistical asides with no quarter/score/player-team
significance) is suppressed by default since it is usually the bulk of the
feed and rarely what an operator is scanning for; pass --all-events to see it.

This is a read-only report over already-collected evidence: it never talks
to AFL/CFS and never draws conclusions about production commentary
semantics -- in particular it never treats siren/quarter commentary text as
an authoritative match-state signal. See docs/ for the AFL-api operator CLI;
this script is invoked directly and is not wired into cli.py because it is
diagnostic-only tooling.

Usage:
    python -m scripts.report_commentary_evidence [--match-id ID]
        [--match-provider-id CD_M...] [--all-events] [--json]
"""
from __future__ import annotations

import argparse
import json

from db.connection import get_read_only_db_connection
from collection.match_commentary_evidence import (
    CATEGORY_QUARTER_END, CATEGORY_QUARTER_START, CATEGORY_SCORE_EVENT,
    event_rows, poll_rows,
)


def _event_line(row: dict) -> str:
    return (
        f"    seq={row['first_observed_poll_sequence']:>4} at={row['first_observed_at']} "
        f"period={row['period_number']}/{row['period_seconds']}s "
        f"player={row['player_id']} team={row['team_id']} scoreEvent={row['score_event']} "
        f"comment={row['comment']!r}"
    )


def _print_match_report(match_provider_id: str, polls: list[dict], events: list[dict], *, all_events: bool) -> None:
    print(f"\n=== {match_provider_id} ===")
    print(f"polls={len(polls)} distinct_events={len(events)}")
    if polls:
        print(f"first poll observed_at={polls[0]['observed_at']} last poll observed_at={polls[-1]['observed_at']}")
        latest_feed_updated = next((p["feed_last_updated"] for p in reversed(polls) if p["feed_last_updated"]), None)
        print(f"latest feed lastUpdated={latest_feed_updated}")

    outcome_transitions = [p for p in polls if p["is_transition"] and "outcome_success" not in p["transition_flags"] and
                            any(flag.startswith("outcome_") for flag in p["transition_flags"])]
    if outcome_transitions:
        print("  -- endpoint availability/outcome transitions --")
        for row in outcome_transitions:
            print(f"    seq={row['poll_sequence']:>4} at={row['observed_at']} outcome={row['outcome']}")

    quarter_events = [e for e in events if e["category"] in (CATEGORY_QUARTER_START, CATEGORY_QUARTER_END)]
    if quarter_events:
        print("  -- quarter markers --")
        for row in quarter_events:
            print(f"    [{row['category']}] " + _event_line(row).strip())

    score_events = [e for e in events if e["category"] == CATEGORY_SCORE_EVENT]
    if score_events:
        print("  -- score events --")
        for row in score_events:
            print(_event_line(row))

    linked_events = [
        e for e in events
        if e["category"] != CATEGORY_SCORE_EVENT and (e["player_id"] is not None or e["team_id"] is not None)
    ]
    if linked_events:
        print("  -- other player/team-linked commentary --")
        for row in linked_events:
            print(_event_line(row))

    edited_events = [e for e in events if e["possible_edit_of_event_id"] is not None]
    if edited_events:
        print("  -- possible edits/changes detected --")
        for row in edited_events:
            print(
                f"    event id={row['id']} possibly replaces event id={row['possible_edit_of_event_id']}: "
                + _event_line(row).strip()
            )

    if all_events:
        other_events = [
            e for e in events
            if e["category"] not in (CATEGORY_QUARTER_START, CATEGORY_QUARTER_END, CATEGORY_SCORE_EVENT)
            and e["player_id"] is None and e["team_id"] is None
        ]
        if other_events:
            print("  -- uncategorised narrative commentary --")
            for row in other_events:
                print(_event_line(row))

    if not (quarter_events or score_events or linked_events or edited_events or outcome_transitions):
        print("  (no quarter markers, score events, linked commentary, or endpoint transitions recorded yet)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match-id", type=int, default=None, help="Filter to one internal match_id")
    parser.add_argument("--match-provider-id", default=None, help="Filter to one CD_M... provider ID")
    parser.add_argument(
        "--all-events", action="store_true",
        help="Also show uncategorised narrative commentary, suppressed by default",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON (polls + events) instead of a human-readable report")
    args = parser.parse_args(argv)

    conn = get_read_only_db_connection()
    try:
        polls = poll_rows(conn, match_id=args.match_id, match_provider_id=args.match_provider_id, limit=None)
        events = event_rows(conn, match_id=args.match_id, match_provider_id=args.match_provider_id, limit=None)
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"polls": polls, "events": events}, indent=2, sort_keys=True))
        return 0

    if not polls and not events:
        print(
            "No commentary evidence has been captured yet. Set AFL_DIAGNOSTICS_ENABLED=true and "
            "AFL_DIAGNOSTIC_PROFILES=commentary (or match_clock,interchange,commentary), then restart "
            "the scheduler during a live match."
        )
        return 0

    by_match: dict[str, dict[str, list[dict]]] = {}
    for row in polls:
        by_match.setdefault(row["match_provider_id"], {"polls": [], "events": []})["polls"].append(row)
    for row in events:
        by_match.setdefault(row["match_provider_id"], {"polls": [], "events": []})["events"].append(row)

    for match_provider_id, data in by_match.items():
        _print_match_report(match_provider_id, data["polls"], data["events"], all_events=args.all_events)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
