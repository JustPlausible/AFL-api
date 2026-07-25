# Match roster collection

Match rosters are team selections and change notices, not evidence of match
participation or player statistics. Collect one Champion Data round through the
existing authenticated CFS client:

```bash
python cli.py --collect-match-rosters CD_R2026_18 --print-json
```

`--afl-raw-directory PATH` enables the existing deterministic raw capture.
Available responses are saved as their original JSON list and unpublished
responses as JSON `null`; neither is wrapped or supplemented with request
headers, CFS tokens, cookies, or credentials.

## Verified live structure

Live investigation against current and completed rounds returned a top-level
list. Every match wrapper exposed these keys:

```text
match
matchRoster
recentMatchScores
teamPlayers
venue
```

`matchRoster` contained `awayTeam`, `competitionId`, `homeTeam`, `lastUpdated`,
`matchId`, `operationHeader`, `recentMatches`, `roundNumber`, `status`, `umpires`,
and `weather`. Both `matchRoster.homeTeam` and `matchRoster.awayTeam` contained
`clubDebuts`, `ins`, `lateChanges`, `matchId`, `milestones`, `outs`, `positions`,
`teamId`, `teamName`, and `teamStatus`. The same wrapper and team structure was
observed for current and completed rounds.

The verified `positions` fixture shape is an ordered list of positional-group
objects, each naming a position and containing a `players` list. Player entries
may be a player object or a wrapper with `player`. The collector retains the
position name as supplied rather than translating it into a speculative enum.
It treats position records as selections. It treats `ins`, `outs`,
`lateChanges`, `clubDebuts`, and `milestones` as distinct change/context records,
not as the selected lineup. Verified `ins[].player` and `outs[].player` fields
include a Champion Data `playerId`, nested given name/surname,
`playerJumperNumber`, and `captain`; their enclosing record may supply `reason`.

At least one future unpublished round returned top-level JSON `null`. This maps
to `unavailable`, is not an authentication or validation failure, and is never
safe to use as a destructive replacement. A top-level empty list maps to
`empty`, but is also conservatively not replacement-safe because its live
meaning has not yet been distinguished from `null`. Genuine HTTP 401 or
`CFSAPI001` responses continue through the shared one-refresh authentication
policy.

## Normalised and retained data

Roster records preserve the requested round provider ID, round number, match
provider/AFL IDs, competition provider ID, match status and last-updated time.
Home and away team records preserve team and match provider IDs, name, status,
side and source order. Player records preserve Champion Data ID, display name,
jumper number, captain flag, team/match/round associations, record kind, source
collection, supplied reason, positional state and source ordering.

Large wrapper values are stored once per roster rather than copied onto every
player. `venue`, `weather`, `umpires`, `operationHeader`, `recentMatches`,
`recentMatchScores`, and `teamPlayers` remain in roster-scoped
`provider_fields`. Unknown match, team, positional-group, change-record, and
player values are retained at their narrowest useful level. `teamPlayers` is
therefore inspected and preserved, but is not used to invent lineup membership
while its relationship to `positions` remains unverified.

Comparison uses match, team, player and record kind as stable identity. The
change collection also participates in identity so an `in`, `out`, or late
change is not flattened into a lineup selection. Position names are mutable
state, allowing a move between position groups to be reported as a changed
record. Repeated responses are sorted and deduplicated deterministically.

## Still unresolved

Further live investigation should establish:

* the complete semantics and possible variants of `positions`;
* how emergencies are represented across competitions and publication stages;
* exact lineup publication timing;
* late-change timing and whether records are replaced or accumulated;
* whether `teamPlayers` is identity, squad, or supplemental lineup data and its
  precise relationship to `positions`;
* whether an empty list has different semantics from `null`;
* whether live/in-progress responses differ from concluded responses.
