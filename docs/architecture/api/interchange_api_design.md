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
design, and is explicit about where the available evidence stops short of
answering the question Issue #204 asked it to answer.

## 2. Confirmed contract, and what remains genuinely unconfirmed

See `docs/investigation/afl-json/ENDPOINT_CATALOG.md` §5 for the complete,
evidence-cited contract. Summary:

* `GET https://api.afl.com.au/cfs/afl/matchInterchange/{match_provider_id}`
  -- authenticated, standard `/cfs/afl` root.
* Response: `{"matchId": "...", "homeInterchange": [...], "awayInterchange": [...], "homeInterchangeCounts": {...}, "awayInterchangeCounts": {...}}`.
* Each entry: `teamId`, `player.playerId` (plus `player.playerName`/
  `player.playerJumperNumber`, deliberately never persisted -- see §3.1),
  `interchangeCount`, `benchReason`, `timeOnGround`, `timeOnBench`,
  `powerRating`.
* **The only real evidence available is one captured CONCLUDED-match
  response** (`tests/fixtures/afl/interchange/match_interchange_8216_concluded.json`):
  five entries per side, each with substantial cumulative
  `timeOnGround`/`timeOnBench`/`interchangeCount` and `benchReason="ROTATION"`
  throughout. There is **no captured live poll-to-poll sequence** showing
  entries actually appearing/disappearing as players rotate during play --
  unlike commentary's Issue #201 promotion, which had a genuine live-poll
  capture plus a confirmed real scoring-outcome change to promote against.

### 2.1 Array-membership semantics: the open question this promotion does not close

Issue #204 asked this promotion to establish, from evidence, whether
`homeInterchange[]`/`awayInterchange[]` membership means "the player is
currently off the ground". **The available evidence cannot establish this
either way.** The single concluded snapshot is consistent with at least two
readings:

1. These are the players who happened to be sitting on the bench right at
   full-time, each carrying their whole-match rotation tally (supports an
   "on the bench right now" reading).
2. This is simply the team's fixed interchange/bench player pool for the
   entire match -- always listed, with only each entry's counters changing
   in place (would make membership itself uninformative about "right now").

Per Issue #204's explicit instruction to expose source-derived state
conservatively rather than promote a diagnostic hypothesis to an
authoritative semantic, the production contract exposes
**`on_interchange_list`** -- a plain, conservatively-worded, source-array-
membership fact, refreshed every poll -- rather than a claimed `on_bench` /
off-ground semantic. Every place this field appears (schema comments, the
OpenAPI description, `docs/api_v1_interchange.md`) carries the same caveat.
This should be revisited, and the field/semantic potentially strengthened
(with a migration if the contract needs to change), once a live round with
actually-observed membership transitions has been captured -- likely via
the still-running `interchange` diagnostic profile, which is exactly the
tool built to gather that evidence.

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
| `on_interchange_list` | Current membership flag. See §2.1. |
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
  transitions from `on_interchange_list=false` to `true`.
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

`scheduler/match_interchange_production.py` mirrors
`scheduler/match_commentary_production.py`'s candidate-window model exactly
(see that module's docstring for the full rationale): the union of
currently-`LIVE` matches, currently-`POSTGAME` matches, a bounded
pre-kickoff tolerance window, and a bounded post-active grace window
computed from this module's own `match_interchange_polls` bookkeeping.

* **QT/HT/3QT:** covered automatically -- `matches.status` stays `LIVE`
  through a regulation-time break, so no special-case code is needed.
* **POSTGAME:** always polled explicitly (`_postgame_matches`).
* **CONCLUDED / final reconciliation:** the post-active grace window
  (`AFL_INTERCHANGE_PRODUCTION_POSTGAME_GRACE_SECONDS`, default 1800s)
  keeps polling for a bounded period after the last `LIVE`/`POSTGAME` poll,
  which covers the `POSTGAME -> CONCLUDED` transition and gives a final
  settled-state poll without a separate reconciliation pass.
* **Endpoint-not-yet-available:** `AflJsonResourceUnavailable` maps to a
  `not_published` poll outcome, recorded via `persist_poll_outcome` -- never
  a hard failure.
* **Retry/auth handling:** reuses the shared `AflJsonClient` and its
  existing token/retry behaviour, identical to every other production CFS
  collector.
* **Restart recovery / idempotency:** see §6.
* Interchange availability **never** determines match finality, and never
  interferes with authoritative player-stat collection -- this module has
  no write path to `matches`, `cfs_player_stats`, or any lease/finality
  table, and never raises out of a poll cycle.

Settings (`AFL_INTERCHANGE_PRODUCTION_*`) default to the same values already
proven for commentary production (20s interval, 600s kickoff tolerance,
1800s postgame grace) rather than guessing a different cadence, since no
captured live interchange poll-cadence evidence exists to tune against.

## 8. Consumer API

### 8.1 `GET /api/v1/matches/{match_id}/interchanges` -- current state

Returns every player observed in either interchange array at any point in
the match (including players who have since left the list, shown with
`on_interchange_list=false` and their last known values, rather than
disappearing from the response). Filters: `side`, `player_id` (canonical),
`on_interchange_list_only`.

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
