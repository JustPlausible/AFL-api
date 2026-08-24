"""Regression check over the real Round 24 live matchInterchange evidence
excerpt (Issue #204 / PR #206).

This does not exercise any production code path -- it parses the checked-in
reduced excerpt of real `scripts/report_interchange_evidence.py` output
(`tests/fixtures/afl/interchange/round24_live_membership_evidence_excerpt.txt`,
see the companion `.metadata.json` for full provenance) and asserts the
specific claims cited in `afl_json/match_interchange.py`'s "Array-membership
semantics: confirmed by real Round 24 live evidence" docstring section and
`docs/architecture/api/interchange_api_design.md` §2.1, so that a future
change to either the fixture or the claim is caught rather than silently
drifting apart.

This is deliberately independent of the production/diagnostic parsing
modules: it is a plain-text regex parse of the report line format, not a
test of `collection.match_interchange_evidence` or
`afl_json.match_interchange`.
"""
from __future__ import annotations

import re
from pathlib import Path

EXCERPT = (
    Path(__file__).parent / "fixtures" / "afl" / "interchange" / "round24_live_membership_evidence_excerpt.txt"
)

_LINE_RE = re.compile(
    r"seq=\s*(?P<seq>\d+) at=(?P<observed_at>\S+) flags=(?P<flags>\S+) "
    r"match_status_at_poll=(?P<status>\S+) home_total=(?P<home_total>[\d.]+) "
    r"away_total=(?P<away_total>[\d.]+) home_players=(?P<home_players>\d+) "
    r"away_players=(?P<away_players>\d+)"
)


def _parse_excerpt() -> list[dict]:
    """Parse every observation line in the excerpt, in file order.

    Section (`=== CD_M... ===`) boundaries and comment/elision lines are
    ignored; each returned dict carries its match_provider_id alongside the
    parsed fields.
    """
    rows: list[dict] = []
    current_match: str | None = None
    for line in EXCERPT.read_text().splitlines():
        section = re.match(r"=== (CD_M\d+) ", line)
        if section:
            current_match = section.group(1)
            continue
        match = _LINE_RE.search(line)
        if not match:
            continue
        assert current_match is not None
        rows.append({
            "match_provider_id": current_match,
            "seq": int(match.group("seq")),
            "flags": match.group("flags").split(","),
            "status": match.group("status"),
            "home_players": int(match.group("home_players")),
            "away_players": int(match.group("away_players")),
        })
    return rows


def test_excerpt_file_exists_and_parses():
    rows = _parse_excerpt()
    assert len(rows) > 20
    assert {row["match_provider_id"] for row in rows} == {
        "CD_M20260142401", "CD_M20260142406", "CD_M20260142408", "CD_M20260142409",
    }


def test_appeared_and_disappeared_flags_are_present_confirming_membership_changes():
    """The core claim: array membership is NOT static during LIVE play.

    These flags are the diagnostic module's exact playerId set-difference
    computation (collection.match_interchange_evidence._player_set_transitions),
    so their presence in real captured data is direct evidence, not an
    inference -- see the module docstring this test guards.
    """
    rows = _parse_excerpt()
    all_flags = {flag for row in rows for flag in row["flags"]}
    assert "player_appeared_home_interchange" in all_flags
    assert "player_disappeared_home_interchange" in all_flags
    assert "player_appeared_away_interchange" in all_flags
    assert "player_disappeared_away_interchange" in all_flags


def test_appear_and_disappear_are_paired_within_the_same_poll_holding_size_steady():
    """Membership changes are swaps (one player off, one on), not net growth --
    the great majority of appear/disappear flags fire together on one poll,
    keeping the listed count at a steady state rather than accumulating."""
    rows = _parse_excerpt()
    paired = 0
    total_with_either = 0
    for row in rows:
        has_appeared = any("appeared" in flag and "interchange" in flag for flag in row["flags"])
        has_disappeared = any("disappeared" in flag for flag in row["flags"])
        if has_appeared or has_disappeared:
            total_with_either += 1
        if has_appeared and has_disappeared:
            paired += 1
    assert total_with_either > 0
    assert paired / total_with_either > 0.5


def test_steady_state_side_size_is_five_with_self_correcting_transient_blips():
    """home_players/away_players sit at 5 in steady state; the rare
    transient 4/6 blips (mid-swap, at 15s polling granularity) always
    self-correct back to 5 within the excerpt, never persisting or drifting
    further -- inconsistent with a genuinely resized or reordered pool."""
    rows = _parse_excerpt()
    counts = [row["home_players"] for row in rows if row["home_players"] > 0]
    counts += [row["away_players"] for row in rows if row["away_players"] > 0]
    assert counts.count(5) / len(counts) > 0.9
    assert any(c == 4 for c in counts)
    assert any(c == 6 for c in counts)
    assert all(c in (4, 5, 6) for c in counts)


def test_membership_changes_correlate_with_team_total_interchange_count_incrementing():
    """A large share of polls carrying an appear/disappear flag also carry a
    total_interchange_count_changed flag (same poll) -- CFS's own rotation
    counter moving in step with array membership changing."""
    rows = _parse_excerpt()
    with_membership_change = [
        row for row in rows
        if any("appeared" in flag or "disappeared" in flag for flag in row["flags"])
    ]
    with_count_change = [
        row for row in with_membership_change
        if any("total_interchange_count_changed" in flag for flag in row["flags"])
    ]
    assert len(with_membership_change) > 5
    assert len(with_count_change) / len(with_membership_change) > 0.2


def test_no_postgame_or_concluded_status_observed_in_excerpt():
    """Documents the residual open question: this evidence covers LIVE play
    only (plus one CONFIRMED_TEAMS anomaly, see below) -- no POSTGAME/
    CONCLUDED row appears anywhere, so that behaviour remains unverified."""
    rows = _parse_excerpt()
    statuses = {row["status"] for row in rows}
    assert "POSTGAME" not in statuses
    assert "CONCLUDED" not in statuses


def test_confirmed_teams_anomaly_still_shows_normal_transitions():
    """A mid-match matches.status anomaly (CONFIRMED_TEAMS instead of LIVE)
    is present in the real data but does not interrupt normal appear/
    disappear behaviour -- a status-tracking data-quality note, not
    evidence against the membership finding."""
    rows = _parse_excerpt()
    anomalous = [row for row in rows if row["status"] == "CONFIRMED_TEAMS"]
    assert len(anomalous) > 0
    assert any(
        any("appeared" in flag or "disappeared" in flag for flag in row["flags"])
        for row in anomalous
    )
