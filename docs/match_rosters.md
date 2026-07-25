# Match roster collection

Match rosters are team selections, not evidence of match participation or
player statistics. Collect one Champion Data round through the authenticated
CFS client:

```bash
python cli.py --collect-match-rosters CD_R2026_18 --print-json
```

`--afl-raw-directory PATH` enables the same deterministic, payload-only raw
capture used by the metadata collectors. Captures contain response JSON but no
request headers, CFS token, or credentials.

## Observed fixture shapes

The representative fixtures exercise two observed top-level states:

* a published object with `publicationState`, timestamp/version metadata, and
  a `matchRosters` list; each match contains `teams`, and each team can contain
  `players`, `namedPlayers`, `squad`, `interchange`, or `emergencies` lists;
* an unpublished object with explicit `published: false` and no roster list.

The collector also accepts `matches`/`teamRosters` as provider collection-name
variants. A present empty match list is `empty`; an explicit unpublished or
unavailable signal, or the provider's established `CFSSDS001` 404, is
`unavailable`. These are successful outcomes and do not invalidate credentials.
Malformed collections raise `AflJsonInvalidResponse`; actual HTTP 401 or
`CFSAPI001` responses retain the shared one-refresh authentication behaviour.

## Normalised fields and unresolved data

The confidently mapped fields retain round, match, team and player provider/AFL
IDs; display and team names; abbreviation; jumper number; named/emergency
flags; provider selection state and group; source ordering; and publication,
timestamp and version values. No missing identifiers or selection states are
invented. `provider_fields` retains unresolved values on the match or player,
and team roster `provider_fields` retains team-level values. This avoids copying the
complete round response onto every selection. Fixture-only values such as
`weatherNote`, `coachCode`, `providerRank`, `providerRole`, and their semantics
remain deliberately unresolved pending real-schema investigation.

Selections are sorted and deduplicated by match, team, player and selection
group. Provider IDs are preferred; AFL IDs, then player name and source order,
are documented fallbacks when provider identifiers are absent. `compare_rosters`
reports additions, removals, changed records, and unchanged records. An
unavailable current snapshot has `replacement_safe: false` and produces no
removals, preventing an unpublished response from implying destructive
replacement of a previously published roster.
