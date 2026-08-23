"""Internal/dev tool: replay a captured CFS commentaryFeed JSON response into
production persistence (Issue #201).

This is the explicitly-scoped "import/replay" mechanism Issue #201 asks for
as an *internal* alternative to a public write endpoint: AFL-api never
exposes a consumer-facing way to POST commentary into the production,
CFS-backed store (see ``docs/architecture/api/commentary_api_design.md``,
"Input/output boundary"). This script is invoked directly by an operator/
developer from a checkout, is not wired into ``cli.py`` or any HTTP route,
and requires an explicit, human-supplied provenance label for every import so
a replayed capture is never mistaken for a live poll in
``match_commentary_polls``/``match_commentary_events``.

Typical uses:

* replaying a fixture (e.g. ``tests/fixtures/afl/commentary/commentary_CD_M20260142409_reduced.json``)
  into a local development database to exercise the consumer API by hand;
* confirming a parser/schema change against a previously captured real
  response without waiting for another live match;
* investigating whether an apparent event mutation originated upstream, by
  replaying an earlier raw capture alongside the currently persisted rows.

Persistence is idempotent (fingerprint-based dedup -- see
``afl_json.match_commentary``), so replaying the same file twice is safe and
produces zero new rows the second time.

Usage:
    python -m scripts.import_commentary_capture \\
        --file tests/fixtures/afl/commentary/commentary_CD_M20260142409_reduced.json \\
        --source-label "manual replay of CD_M20260142409 Bruno capture" \\
        [--match-id 123] [--match-provider-id CD_M20260142409] \\
        [--observed-at 2026-08-23T12:15:40+00:00] [--match-status CONCLUDED]

``--match-id``/``--match-provider-id`` are optional overrides; when omitted,
the match is resolved from the payload's own top-level ``matchId`` via the
same canonical crosswalk production polling uses
(``afl_json.match_commentary.resolve_canonical_match_id``). If the match
cannot be resolved, this script refuses to guess and exits with an error --
consistent with Issue #201's "never guess an unresolved identity" rule.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from afl_json.match_commentary import (
    MatchCommentaryError, parse_commentary_feed, persist_commentary_feed, resolve_canonical_match_id,
)
from db.connection import get_db_connection


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, type=Path, help="Path to a captured commentaryFeed JSON response.")
    parser.add_argument(
        "--source-label", required=True,
        help="Required human-readable provenance for this import, e.g. 'manual replay of <fixture>'.",
    )
    parser.add_argument("--match-id", type=int, default=None, help="Canonical match id override.")
    parser.add_argument("--match-provider-id", default=None, help="Champion Data match id override.")
    parser.add_argument(
        "--observed-at", default=None,
        help="ISO 8601 UTC timestamp to record as this import's observation time (default: now).",
    )
    parser.add_argument(
        "--match-status", default=None,
        help="Local match status to record on the poll row (e.g. CONCLUDED). Informational only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        payload = json.loads(args.file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read/parse {args.file}: {exc}", file=sys.stderr)
        return 2

    match_provider_id = args.match_provider_id or payload.get("matchId")
    if not isinstance(match_provider_id, str) or not match_provider_id:
        print("Could not determine match_provider_id from --match-provider-id or the payload's matchId.", file=sys.stderr)
        return 2

    observed_at = args.observed_at or datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        match_id = args.match_id
        if match_id is None:
            match_id = resolve_canonical_match_id(conn, match_provider_id)
        if match_id is None:
            print(
                f"Could not resolve match_provider_id={match_provider_id!r} to a canonical match_id. "
                "Pass --match-id explicitly if this match is not yet persisted, or import will not "
                "guess an identity.",
                file=sys.stderr,
            )
            return 2

        try:
            feed = parse_commentary_feed(
                payload, match_id=match_id, match_provider_id=match_provider_id,
                observed_at=observed_at, match_status_at_poll=args.match_status,
            )
        except MatchCommentaryError as exc:
            print(f"Payload could not be parsed: {exc}", file=sys.stderr)
            return 2

        result = persist_commentary_feed(conn, feed)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(
        f"Imported {match_provider_id} (match_id={match_id}) from {args.file} "
        f"[source={args.source_label!r}]: "
        f"new_events={result['new_event_count']} possible_edits={len(result['possible_edits'])} "
        f"event_count_in_feed={result['event_count_in_feed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
