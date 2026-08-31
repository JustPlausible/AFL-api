# Match roster collection

## Manual persistent season backfill and repair

Operators can deliberately repair canonical CFS roster state for one persisted
season without changing the normal production schedule:

```bash
# one round
python cli.py --sync-match-rosters 2026 --round 5

# inclusive range
python cli.py --sync-match-rosters 2026 --round-from 1 --round-to 9

# whole season
python cli.py --sync-match-rosters 2026
```

CFS roster collection is round-scoped, so the command makes exactly one AFL/CFS
request for each selected canonical round, using its existing `CD_R...`
provider ID. Historical and concluded rounds are intentionally eligible. A
failure or unavailable response for one round is reported but does not prevent
later selected rounds from being attempted; human and `--print-json` reports
include aggregate row counts and actionable per-round outcomes.

The production scheduler remains responsible for normal current/future roster
collection. This command is only a manual historical backfill/repair path. It
uses `MatchRosterCollector` and `persist_match_rosters` to write the canonical
`cfs_match_rosters`, `cfs_match_roster_selections`, and
`cfs_match_roster_context` tables. It never invokes or writes the legacy HTML
`lineups`/`player_lineups` subsystem.

Match rosters are team selections and change notices, not evidence of match
participation or player statistics. Collect one Champion Data round through the
existing authenticated CFS client:

```bash
python cli.py --collect-match-rosters CD_R202601421 --print-json
```

The argument is an opaque Champion Data round provider ID and must start with
`CD_R`. Numeric AFL round or season identifiers are rejected locally before
authentication or network access. For example:

```bash
python -m cli --collect-match-rosters CD_R202601421
```

CLI outcomes are distinct:

* an invalid identifier exits with code 2 and explains the required `CD_R...`
  form without a traceback;
* `unavailable` means the valid round is not yet published;
* `empty` means the valid round returned a published empty list whose semantics
  remain conservatively read-only;
* `published` reports the number of selections collected; and
* a genuine CFS authentication failure remains an authentication error and is
  never relabelled as invalid input or permission to run HTML.

The structured output identifies `source_family=cfs_json`,
`collector=MatchRosterCollector`, a null `persistence_target`, and
`persistence_performed=false`. **This CLI command remains deliberately
read-only** — it inspects one round without writing to the database, even
though canonical persistence now exists (see "Canonical persistence
(Issue #219)" below). Use
`python cli.py --collect-scheduled-jobs`/the production scheduler, or
`collection.source_policy.collect_operational(OperationalDomain.MATCH_ROSTERS,
target_id=<round_id>)` for an internal round id, to persist.

## Canonical persistence (Issue #219)

The collector above remains the single source of normalisation; production
persistence reuses its output unchanged rather than re-parsing the raw CFS
payload. `afl_json.rosters.persist_match_rosters` upserts three tables
(migration `0024_match_roster_production.py`, which documents the full
schema/safety rationale in its module docstring):

* **`cfs_match_rosters`** — one row per `(match_provider_id,
  team_provider_id)`, holding source `teamStatus`, the roster-level
  `matchRoster.status`/`lastUpdated` observed with it, and canonical
  match/team identity where resolvable.
* **`cfs_match_roster_selections`** — current selected positional-lineup
  membership, one row per `(match_provider_id, team_provider_id,
  player_provider_id)` observed in `positions`.
* **`cfs_match_roster_context`** — current `ins`/`outs`/`lateChanges`/
  `clubDebuts`/`milestones` records, kept in a table separate from
  selections so a change/context record can never be read as lineup
  membership.

**A CFS roster is a team selection, not proof of participation.** Nothing
persisted here, and nothing derived from it (including the
[`/api/v1/matches/{match_id}/rosters`](api_v1_rosters.md) consumer resource),
implies a selected player took the field — see
[`docs/api_v1_player_stats.md`](api_v1_player_stats.md) for actual
participation and statistics.

### Current-state, not append-only history

There is no separate roster event-history table. The live evidence already
recorded below (a pre-bounce and a LIVE capture of the same match differing
only in the roster timestamp) does not establish that selections change
often enough in observed practice to justify one; `compare_rosters`'s
existing diff already treats each valid published response as the complete
current selection/context state for a team, and persistence mirrors that
model unchanged. A player's row is *updated* in place when e.g. their
position changes (`first_observed_at` preserved, `last_observed_at`/
`position` refreshed) rather than duplicated.

### Replacement and supersession safety (Issue #219)

`persist_match_rosters` reuses `compare_rosters`'s existing
`replacement_safe` gate unchanged: only a `RosterStatus.PUBLISHED`
observation is replacement-safe. Concretely:

| Observation | Effect on a previously persisted roster |
| --- | --- |
| Top-level `null` (`RosterStatus.UNAVAILABLE`) | No effect — `persist_match_rosters` returns immediately without touching any table. |
| Top-level empty list `[]` (`RosterStatus.EMPTY`) | No effect — same immediate no-op. Live evidence has not yet established a distinct destructive meaning for an empty list (see "Still unresolved" below), so it is conservatively treated exactly like `null`. |
| Malformed/partial payload | Never reaches persistence at all — `MatchRosterCollector.collect` raises before returning a result, so the caller (the production scheduler, or a manual `collect_operational` trigger) never calls `persist_match_rosters` for that observation. |
| A genuine later published update | Replaces the affected team's selection/context rows in place: a player no longer present is removed, a newly present player is added, an existing player's changed fields (e.g. `position`) are updated. |
| An unchanged repeated publish | Idempotent — the same rows are re-upserted with the same content; no duplicates, no spurious change. |
| Provider array reordering | Never a change — identity is keyed by `(match_provider_id, team_provider_id, player_provider_id)`/`context_type`, never by array position. `group_order`/`player_order` are retained only as non-authoritative presentation metadata. |

### Identity resolution

* **Match** — `matches.match_provider_id` (existing canonical match
  relationship). An unresolved match is skipped for that match only; other
  matches in the same round observation are unaffected.
* **Team** — `afl_teams.provider_id` (the same crosswalk used by
  `afl_json.match_interchange.resolve_canonical_team`). An unresolved team
  still persists its Champion Data team id; `canonical_team_id` stays `NULL`
  rather than guessed.
* **Player** — the existing `player_provider_ids` crosswalk
  (`provider='champion_data'`), exactly as used by
  `afl_json.player_stats`/`afl_json.match_interchange`/
  `afl_json.match_commentary`. Never resolved by display name or jumper
  number. Canonical identity is re-resolved on every valid observation (a
  current-state table), so a crosswalk added after a player's first roster
  observation self-heals the persisted row on the next valid publish — no
  separate repair path is required.

A selection or context record with no Champion Data player id at all (not
observed in any committed evidence, but structurally possible per the
source's own optional shapes) is conservatively skipped rather than
persisted under an invented identity.

### Production collection integration

Promoted through the existing scheduler/source-policy architecture, not a
parallel orchestration system:

* `collection.source_policy.OperationalDomain.MATCH_ROSTERS` now persists
  (`SourcePolicy.persists=True`) for manual/admin one-off triggering via
  `collect_operational`.
* `scheduler.match_roster_production.poll_match_rosters` is the recurring
  production poller, registered in `scheduler/scheduled_tasks.py` alongside
  the other production collectors, gated by its own
  `AFL_ROSTER_PRODUCTION_ENABLED` flag. It polls **rounds**, not individual
  matches — one `matchRosters/round/{round_provider_id}` request refreshes
  every match in that round at once.
* **Cadence is deliberately conservative**: 900 seconds (15 minutes) by
  default (`AFL_ROSTER_PRODUCTION_INTERVAL_SECONDS`), far slower than
  commentary/interchange's 20-second live cadence. The evidence below (a
  pre-bounce and LIVE capture of the same match differing only in
  timestamp) does not show selection churn fast enough to justify
  higher-frequency polling.
* A round becomes a candidate once any of its matches is `LIVE`, or within
  `AFL_ROSTER_PRODUCTION_PRE_ROUND_WINDOW_SECONDS` (default 24h, mirroring
  the legacy HTML lineup scheduler's "T-1 day" trigger) of its scheduled
  start, or within `AFL_ROSTER_PRODUCTION_KICKOFF_TOLERANCE_SECONDS`
  (default 600s) after a scheduled start that has not yet locally flipped to
  `LIVE`. A round with every match `CONCLUDED` is never polled.
* See `scheduler/match_roster_production.py`'s module docstring for the full
  candidate-selection and failure-isolation rationale.

### Legacy `lineups` boundary

Canonical CFS roster persistence is a **distinct authority** from the
pre-existing rendered-HTML `lineups` table/routes
(`OperationalDomain.LINEUPS`, `scraper.scrape_afl_lineups`). Issue #219 does
not repoint, rewrite, or migrate that path:

* the `/api/v1/matches/{match_id}/rosters` consumer resource reads only the
  canonical CFS tables above, never `lineups`;
* the legacy HTML `lineups` routes/persistence remain unchanged and continue
  to operate exactly as before;
* neither domain falls back to the other, and neither is dual-written merely
  for compatibility.

See [`docs/architecture/data_authority_map.md`](architecture/data_authority_map.md)
for the maintained authority-boundary statement.

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
It treats position records as selections. Where they are lists, it treats
`ins`, `outs`, `lateChanges`, `clubDebuts`, and `milestones` as distinct
change/context records, not as the selected lineup. Verified `ins[].player` and `outs[].player` fields
include a Champion Data `playerId`, nested given name/surname,
`playerJumperNumber`, and `captain`; their enclosing record may supply `reason`.

At least one future unpublished round returned top-level JSON `null`. This maps
to `unavailable`, is not an authentication or validation failure, and is never
safe to use as a destructive replacement. A top-level empty list maps to
`empty`, but is also conservatively not replacement-safe because its live
meaning has not yet been distinguished from `null`. Genuine HTTP 401 or
`CFSAPI001` responses continue through the shared one-refresh authentication
policy.

Live current-round verification also found `lateChanges` represented by an
empty object rather than a list. Its object semantics remain unresolved, so it
is retained unchanged at team scope and does not create player/change records.
All six optional team fields (`positions`, `ins`, `outs`, `lateChanges`,
`clubDebuts`, and `milestones`) follow the same defensive rule: lists are
normalised using the supported record shapes, `null` is absent, and objects or
other unresolved values are preserved at team scope without invalidating the
otherwise usable roster.

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
record. Provider array indexes are retained as diagnostic `source_order`, but
are deliberately excluded from identity and change equality: reordering the
positions array or players within a position is therefore unchanged. Repeated
responses are sorted and deduplicated deterministically by stable identifiers.

Live validation found the endpoint available both before and during matches,
with status transitioning from `UNCONFIRMED_TEAMS` to `LIVE`. Selections may
change shortly before first bounce. A pre-bounce and live capture differed only
in the roster timestamp, and the live roster then appeared stable. This is
evidence that publication is effectively frozen near first bounce, but remains
a provider observation rather than a persistence rule enforced by the collector.
This same pre-bounce/LIVE stability evidence is the basis for the production
scheduler's conservative 15-minute polling cadence — see "Production
collection integration" above.

## Still unresolved

Every item below remains conservatively handled by both the collector and
production persistence: an unresolved shape is retained at collector scope
(if a list) or skipped (if it cannot be resolved to a Champion Data player
id), never guessed into a canonical meaning.

Further live investigation should establish:

* the complete semantics and possible variants of `positions`;
* how emergencies are represented across competitions and publication stages;
* whether publication can change after first bounce in exceptional cases;
* late-change timing and whether records are replaced or accumulated before bounce;
* whether `teamPlayers` is identity, squad, or supplemental lineup data and its
  precise relationship to `positions`;
* whether an empty list has different semantics from `null`;
* whether concluded responses can differ from the frozen live roster.
