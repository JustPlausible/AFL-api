# Production CFS Match-Interchange Persistence and Consumer API Design

**Status:** Implemented (Issue #204)

**Precedes this work:** [Issue #193](https://github.com/JustPlausible/AFL-api/issues/193)
(diagnostic-only `matchInterchange` evidence capture). This design is the
production promotion of that investigation, built against the same evidence
gathered under Issue #193 rather than a fresh endpoint-discovery exercise.
See `docs/investigation/afl-json/ENDPOINT_CATALOG.md` §5 "Update (Issue
#204)" for the full confirmed contract and `docs/diagnostics_framework.md`
for why the Issue #193 diagnostic profile keeps running independently after
this promotion. The architectural pattern (a new, separate production
module rather than promoting the diagnostic collector; the same scheduler
candidate-window shape) follows [Issue #201](https://github.com/JustPlausible/AFL-api/issues/201)'s
commentary promotion precedent.

## 1. Background

`GET {CFS root}/matchInterchange/{match_provider_id}` returns, per team, an
array of interchange entries (`homeInterchange[]`/`awayInterchange[]`) --
each carrying a player identity plus `interchangeCount`, `benchReason`,
`timeOnGround`, `timeOnBench` and `powerRating` -- alongside team-level
`home/awayInterchangeCounts` totals. Issue #193 built a diagnostic-only
evidence capture pathway to observe this endpoint's real behaviour before
committing to a production design. This document records that production
design, including the real Round 24 live evidence that confirms the
array-membership semantic for LIVE play (§2.1) and what still remains
open beyond that.

## 2. Confirmed contract, and the confirmed array-membership semantic

See `docs/investigation/afl-json/ENDPOINT_CATALOG.md` §5 for the complete,
evidence-cited contract. Summary:

* `GET https://api.afl.com.au/cfs/afl/matchInterchange/{match_provider_id}`
  -- authenticated, standard `/cfs/afl` root.
* Response: `{"matchId": "...", "homeInterchange": [...], "awayInterchange": [...], "homeInterchangeCounts": {...}, "awayInterchangeCounts": {...}}`.
* Each entry: `teamId`, `player.playerId` (plus `player.playerName`/
  `player.playerJumperNumber`, deliberately never persisted -- see §3.1),
  `interchangeCount`, `benchReason`, `timeOnGround`, `timeOnBench`,
  `powerRating`.
* A single captured CONCLUDED-match response
  (`tests/fixtures/afl/interchange/match_interchange_8216_concluded.json`)
  confirms the entry-level field shape: five entries per side, each with
  substantial cumulative `timeOnGround`/`timeOnBench`/`interchangeCount`
  and `benchReason="ROTATION"`.

### 2.1 Array-membership semantics: confirmed by real Round 24 live evidence

Issue #204 asked this promotion to establish, from evidence, whether
`homeInterchange[]`/`awayInterchange[]` membership means "the player is
currently off the ground". This PR's first draft could not establish that
from the single CONCLUDED-match fixture available in the repository. Real
Round 24 live diagnostic observations were subsequently supplied (the
human-readable output of `scripts/report_interchange_evidence.py`, run
against a deployment's populated `match_interchange_evidence_observations`
table) and reviewed on PR #206 -- **this does establish it, for LIVE play.**

The evidence covers **7 real Round 24 matches** --
`CD_M20260142401`, `CD_M20260142403`, `CD_M20260142404`, `CD_M20260142405`,
`CD_M20260142406`, `CD_M20260142408`, `CD_M20260142409` -- each polled
roughly every 15 seconds across its full ~3 hour `LIVE` window (e.g.
`CD_M20260142409`: first `observed_at=2026-08-23T09:20:14Z`, last
`observed_at=2026-08-23T12:13:44Z`, `693` observations). The diagnostic
module's `appeared`/`disappeared` transition flags are computed as an exact
Champion Data `playerId` set difference between successive polls
(`collection.match_interchange_evidence._player_set_transitions`,
`set(current) - set(previous)` over indexed `player.playerId` values) --
their presence in real captured data is direct proof, not an inference,
that the specific set of listed players changes during `LIVE` play.

Aggregated across the 7 matches: 442 `player_appeared_home_interchange` and
435 `player_disappeared_home_interchange` events; 435
`player_appeared_away_interchange` and 428 `player_disappeared_away_interchange`
events -- membership changed on a large share of the ~700 polls per match.
Appearances and disappearances are near-perfectly paired within the same
poll (a same-poll appear+disappear on one side, not mere growth), holding
each side's listed player count at a steady 5 throughout every match; the
handful of polls that briefly showed 4 or 6 listed players (e.g.
`CD_M20260142403` seq 349, `CD_M20260142404` seq 196 and seq 479,
`CD_M20260142408` seq 364, `CD_M20260142409` seq 329-332) self-corrected
back to 5 on the immediately following poll(s) -- consistent with catching
an in-progress swap mid-transition at the 15s polling granularity, and
inconsistent with a pool that is ever actually resized or merely reordered
(reordering the same five players would never produce an `appeared`/
`disappeared` flag at all, since those flags are a set-membership diff, not
a positional one). Membership changes are also tightly time-correlated with
each team's own `totalInterchangeCount`/quarter count incrementing
(`home_total_interchange_count_changed`/`away_total_interchange_count_changed`
co-occur with, or immediately follow, the great majority of appear/
disappear events) -- CFS's own aggregate rotation counter moves in lockstep
with array membership changing, exactly as expected if each membership
change is a genuine interchange/rotation event, and hard to explain under
the "fixed, always-listed pool" reading this promotion originally could not
rule out.

**Update: individual round-trip and POSTGAME behaviour now confirmed too.**
A full per-poll (not aggregate-only) evidence export for `CD_M20260142409`
-- all 693 rows, including the raw payload retained on every transition
poll -- was subsequently reviewed (Issue #204 comment, PR #206):

* **Individual player round-trip: confirmed.** Champion Data player
  `CD_I1028561` ("Tom Gross", home side) appears in and disappears from
  `homeInterchange[]` **five separate times** across this one match (poll
  pairs (2,48), (100,149), (230,254), (445,482), (556,590)); 23 distinct
  home-side player ids show the same repeated pattern. The first round trip
  is checked in as real, verbatim raw responses --
  `tests/fixtures/afl/interchange/match_interchange_CD_M20260142409_poll002_appeared.json`,
  `..._poll048_disappeared.json`, `..._poll100_reappeared.json` --
  exercised directly against `parse_match_interchange`/
  `persist_match_interchange` by `tests/test_interchange_round24_real_sequence.py`.
* **POSTGAME behaviour: confirmed to freeze.** The same match's export
  includes 40 `POSTGAME` polls (poll_sequence 654-693, the diagnostic
  profile's full 600s post-live grace window). Every field -- not just
  membership, but each entry's `interchangeCount`/`benchReason`/
  `timeOnGround`/`timeOnBench`/`powerRating`, and the team-level counts --
  is byte-identical across all 40 polls, with zero transition flags
  recorded. `matchInterchange` state freezes exactly at the
  `LIVE` -> `POSTGAME` transition and does not continue updating, at least
  through this ~10 minute window. Checked in as
  `tests/fixtures/afl/interchange/match_interchange_CD_M20260142409_postgame_poll654.json`
  / `..._postgame_poll693.json` (reconstructed from this match's stored
  parsed field values, since the diagnostic module only retains the raw
  HTTP payload on polls with a recorded transition and POSTGAME had none --
  every value in them is still real; see the fixture directory's metadata
  sidecar), exercised by the same test module to confirm persisting both
  produces zero new events.

**One thing remains open: CONCLUDED behaviour.** This match's capture ends
at poll_sequence 693, still `POSTGAME` -- no `CONCLUDED` row was ever
captured, consistent with the diagnostic profile that produced this
evidence having its own post-live grace window elapse before
`matches.status` advanced further. Whether `matchInterchange` stays
queryable/frozen or becomes unavailable once a match reaches `CONCLUDED`
remains unverified. The consumer API makes no claim beyond "the most
recently observed state" (§8.1); production collection itself takes
exactly one reconciliation poll at `POSTGAME` and then stops, never polling
into or waiting for `CONCLUDED` at all (§7).

On the strength of the confirmed findings, the production contract exposes
**`on_bench`** -- a per-player boolean reflecting current
`homeInterchange`/`awayInterchange` array membership as of the most recent
poll, confirmed for LIVE play and confirmed to freeze through at least 10
minutes of POSTGAME -- documented with the evidence above and the one
residual caveat (CONCLUDED) everywhere the field appears (schema comments,
the OpenAPI description, `docs/api_v1_interchange.md`).

### 2.2 `benchReason`

Persisted and returned exactly as CFS supplies it (`"ROTATION"` is the only
value observed to date). Never inferred as injury, substitution, tactical,
or medical reasoning from commentary, timing, or any other field. Absent
when CFS does not supply one.

## 3. Persistence design

### 3.1 Why a new, separate production module

`afl_json/match_interchange.py` and `scheduler/match_interchange_production.py`
are new modules, not a promotion of the diagnostic ones -- mirroring the
Issue #187 (`afl_json/match_period.py`) and Issue #201
(`afl_json/match_commentary.py`) precedents, and required by
`docs/diagnostics_framework.md`'s core rule: diagnostic evidence must never
silently become production source authority. The diagnostic
`match_interchange_evidence_observations` table (migration `0017`) is
completely unaffected. Player identity is resolved through `playerId` only
via the existing `player_provider_ids` crosswalk -- `player.playerName`/
`player.playerJumperNumber` are parsed out of the source payload but never
persisted or used for identity matching.

### 3.2 Two persistence shapes for two different purposes (migration `0021`)

Interchange data has a fundamentally different shape from commentary: a
commentary event is a discrete, immutable fact ("this text was published at
this slot"), while interchange is a *continuously updated per-player state*
(counters tick, membership flips). Three tables reflect that:

**`match_interchange_state`** -- one **current** row per
`(match_provider_id, player_provider_id)`, upserted on every poll that
observes that player:

| Column | Purpose |
|---|---|
| `player_provider_id`, `canonical_player_id` | Source Champion Data player id, and its resolved canonical link. **Re-resolved on every update** (unlike commentary's immutable events -- see below), so a crosswalk added after first observation self-heals a current-state row. |
| `team_provider_id`, `canonical_team_id` | Same, for team identity. |
| `side` | `home`/`away` -- which array this player was last observed in. |
| `on_bench` | Current membership flag, confirmed for LIVE play. See §2.1. |
| `interchange_count`, `bench_reason`, `time_on_ground`, `time_on_bench`, `power_rating` | Latest known values, exactly as supplied. |
| `first_observed_at`, `last_observed_at`, `last_transition_at` | UTC provenance -- see §4. |
| `match_status_at_last_observation` | Local `matches.status` snapshot at the last update, mirroring the diagnostic profile's own convention. |

This is deliberately narrower than the diagnostic table: it does not
persist the team-level `home/awayInterchangeCounts` totals (out of scope
for the "is this player on the bench" consumer question) and never keeps a
player's display name or jumper number.

**`match_interchange_events`** -- append-only, **meaningful-only**
transition history. See §5.

**`match_interchange_polls`** -- lightweight per-match poll bookkeeping
(sequence continuity, outcome, feed entry counts), mirroring
`match_commentary_polls`. Never retains the raw feed payload.

### 3.3 Canonical identity linking: current state re-resolves, history does not

Unlike `match_commentary_events` (an immutable append-only log that
resolves canonical identity once, at first observation, and never
backfills it), `match_interchange_state` is a *current*-state table:
canonical identity is re-resolved on every poll that touches a player's
row. `match_interchange_events` (the transition history) is immutable like
commentary's event log -- each row keeps whatever canonical identity was
resolved at the moment that specific event was detected. This distinction
is deliberate: a "current state" table should reflect the best information
available *now*, while a history table should be a faithful record of what
was known *at that time*.

## 4. Timeline semantics

Every state row and event row carries the UTC time AFL-api's poll observed
it (`observed_at` / `last_observed_at` / `first_observed_at` /
`last_transition_at`). This is **the poll observation time, not an exact
in-game clock instant**: `matchInterchange` supplies no `periodNumber`/
`periodSeconds`, so none is fabricated here (unlike commentary, which gets
genuine match-clock coordinates from its own source feed). Consumers can
still correlate interchange transitions with the production match-period
timeline (Issue #187) and with commentary events approximately, by nearest
UTC `observed_at`, and with the local `match_status_at_poll` snapshot each
row carries (`LIVE`/`POSTGAME`/etc.) -- but this is documented as an
approximate, poll-cadence-limited correlation, never a precise game-clock
join key.

## 5. Meaningful transition history, not a poll-by-poll copy

`match_interchange_events` records exactly these event types, each derived
by diffing the incoming poll against **durable** `match_interchange_state`
(never an in-memory previous-poll object -- see §6):

* `appeared` -- a player's row is newly created, or an existing row
  transitions from `on_bench=false` to `true`.
* `disappeared` -- a previously on-list player is missing from a poll whose
  array for that side was known (not missing/malformed).
* `interchange_count_changed` -- carries `previous_interchange_count`/
  `interchange_count`.
* `bench_reason_changed` -- carries `previous_bench_reason`/`bench_reason`.

`time_on_ground`/`time_on_bench`/`power_rating` are **never** event triggers
-- Issue #204 explicitly prohibits an event row simply because a timer
ticked. They are still always refreshed on the current-state row, and are
carried as non-triggering context snapshots on whichever event *did* fire
(so an `interchange_count_changed` event, for example, also shows what the
timers were at that moment).

## 6. Idempotency and restart safety

Every write diffs the incoming poll against **durable** previously-persisted
state (`match_interchange_state`, loaded fresh from the database at the
start of each `persist_match_interchange` call), never an in-memory
previous-poll object. This means:

* a repeated identical poll produces no new event rows and only touches
  bookkeeping columns on the current-state row;
* a replay of an already-seen payload (e.g. after a container/scheduler
  restart) produces exactly the same result as the original live poll
  would have, because the diff baseline is read from disk, not carried in
  process memory;
* `match_interchange_polls.poll_sequence` is recomputed as `MAX + 1` from
  durable storage on every write, exactly like `match_commentary_polls`.

This is the same durable-diff idempotency strategy the Issue #193
diagnostic module already proved via `load_previous_observation`.

## 7. Scheduler / lifecycle behaviour

**Lifecycle: `LIVE` -> one final `POSTGAME` reconciliation poll -> stop.**
`scheduler/match_interchange_production.py` initially mirrored
`scheduler/match_commentary_production.py`'s candidate-window model exactly
(continuous `POSTGAME` polling plus a bounded post-active grace window).
Once the real `CD_M20260142409` POSTGAME-freeze evidence (§2.1) showed that
design would only ever re-observe an identical payload after the first
POSTGAME poll, the lifecycle was tightened (Issue #204 follow-up comment on
PR #206) to: the union of currently-`LIVE` matches, a bounded pre-kickoff
tolerance window, and currently-`POSTGAME` matches that have **not yet**
received their one reconciliation poll
(`afl_json.match_interchange.pending_postgame_reconciliation_matches`).

* **QT/HT/3QT:** covered automatically -- `matches.status` stays `LIVE`
  through a regulation-time break, so no special-case code is needed.
* **POSTGAME / final reconciliation:** the *first* poll to observe a match
  as `POSTGAME` (checked via a durable, restart-safe read of this module's
  own `match_interchange_polls` -- "has a poll ever recorded
  `match_status_at_poll='POSTGAME'` for this match?", never in-memory state
  or a timer) is that match's one and only reconciliation poll. Every
  subsequent poll cycle excludes it -- no grace window, no repeated
  re-observation.
* **`CONCLUDED`:** not this module's concern at all. It has no code path
  that reads, waits for, or depends on `CONCLUDED` -- that transition, and
  match finality generally, are decided entirely by the normal
  authoritative match-state pipeline.
* **Endpoint-not-yet-available:** `AflJsonResourceUnavailable` maps to a
  `not_published` poll outcome, recorded via `persist_poll_outcome` -- never
  a hard failure. Note this still counts as "reconciled" for a `POSTGAME`
  match (any recorded outcome does, per
  `pending_postgame_reconciliation_matches`'s docstring), so a transient
  failure on the one reconciliation attempt is not retried forever.
* **Retry/auth handling:** reuses the shared `AflJsonClient` and its
  existing token/retry behaviour, identical to every other production CFS
  collector.
* **Restart recovery / idempotency:** see §6 -- the reconciliation check
  itself is restart-safe for the same reason (a durable table read, not a
  timer or in-memory flag).
* Interchange availability **never** determines match finality, and never
  interferes with authoritative player-stat collection -- this module has
  no write path to `matches`, `cfs_player_stats`, or any lease/finality
  table, and never raises out of a poll cycle.

Settings (`AFL_INTERCHANGE_PRODUCTION_*`) default to the same interval/
kickoff-tolerance values already proven for commentary production (20s
interval, 600s kickoff tolerance) rather than guessing different ones.
There is deliberately no `..._POSTGAME_GRACE_SECONDS` setting -- see above.

## 8. Consumer API

### 8.1 `GET /api/v1/matches/{match_id}/interchanges` -- current state

Returns every player observed in either interchange array at any point in
the match (including players who have since left the list, shown with
`on_bench=false` and their last known values, rather than
disappearing from the response). Filters: `side`, `player_id` (canonical),
`on_bench_only`.

### 8.2 `GET /api/v1/matches/{match_id}/interchanges/events` -- transition history

A separate route, not an optional inclusion on the current-state route
(the two other real precedents in this codebase --
`GET /matches/{id}/commentary` and `GET /matches/{id}/player-stats` --
each serve one clear shape; overloading the current-state response with an
optional, potentially large history array would blur that). Returns
`match_interchange_events` chronologically (oldest-first). Filters:
`player_id` (canonical), `event_type`.

Both routes use the normal canonical `match_id` (never a Champion Data
match id), follow the existing `/api/v1` conventions for identifiers,
nullability, UTC timestamps, provider metadata and structured error
responses (`ApplicationErrorResponse`, `404 match_not_found`), and are
documented in the OpenAPI schema via each response model's field
descriptions. See `docs/api_v1_interchange.md` for the consumer-facing
reference.

## 9. Tests and fixtures

`tests/test_match_interchange_production.py` (parsing, canonical
resolution, persistence, idempotency, malformed-response isolation),
`tests/test_match_interchange_production_scheduler.py` (candidate window,
outcome mapping, restart recovery), and `tests/test_api_v1_interchange.py`
(consumer API contract) exercise this design against the same real reduced
Round 24 fixture used by the Issue #193 diagnostic tests
(`tests/fixtures/afl/interchange/match_interchange_8216_concluded.json`)
plus small synthetic payloads for scenarios the single real capture cannot
demonstrate (multi-poll transitions, disappearance, restart replay).

`tests/test_interchange_round24_live_evidence.py` is a separate regression
check over the real Round 24 live evidence behind §2.1's confirmed LIVE-play
semantic: it parses
`tests/fixtures/afl/interchange/round24_live_membership_evidence_excerpt.txt`
(a reduced, real excerpt of `scripts/report_interchange_evidence.py`'s
*transitions-only* report mode, across 7 matches, with full provenance in
the companion `.metadata.json`) and asserts the specific claims cited above
(paired appear/disappear, steady-state-5 with self-correcting transient
blips, correlation with `totalInterchangeCount` incrementing). Its "no
`POSTGAME`/`CONCLUDED` row observed" claim describes that specific
transitions-only report -- POSTGAME polls generally have zero transition
flags and so are invisible to that report mode, not evidence that no
POSTGAME polls exist; see the module docstring for how this was
subsequently resolved for POSTGAME specifically. It does not exercise any
production persistence/parsing code path; it exists purely to make the
aggregate LIVE-play evidence durable and checkable within the repository.

`tests/test_interchange_round24_real_sequence.py` exercises production
`parse_match_interchange`/`persist_match_interchange` directly against the
individual-player-cited, POSTGAME-freeze fixture set described in §2.1
above (a full, non-transitions-only per-poll export for `CD_M20260142409`,
supplied on Issue #204) -- confirming, with real production code and real
data, both a genuine appear/disappear/reappear cycle for one named player
and the field-for-field POSTGAME freeze across a real ~10 minute window.

## 10. Architectural boundaries preserved

* No BBBFFL-specific scoring logic or vocabulary anywhere in this module or
  its consumer API.
* No name/jumper-number identity matching -- `playerId` only.
* No fabricated exact game-clock timestamp (§4).
* No inferred injury/substitution semantics from `bench_reason` (§2.2).
* The diagnostic evidence table is never exposed as the v1 contract.
* No second scheduler subsystem -- this reuses the exact candidate-window
  pattern already proven by commentary production and the diagnostic
  profiles, registered as one more job in `scheduler/scheduled_tasks.py`.
* No per-poll timer event explosion (§5).
* No interchange dependency in authoritative match finality (§7).
