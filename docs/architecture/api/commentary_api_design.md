# Production CFS Match-Commentary Persistence and Consumer API Design

**Status:** Implemented (Issue #201)

**Precedes this work:** [Issue #196](https://github.com/JustPlausible/AFL-api/issues/196)
(diagnostic-only `commentaryFeed` evidence capture), whose evidence -- plus new
real Round 24 captures for `CD_M20260142409` and the combined diagnostic
report covering the rest of that weekend -- this design is built from. See
`docs/investigation/afl-json/ENDPOINT_CATALOG.md` §5 "Update (Issue #201)"
for the full confirmed endpoint contract and `docs/diagnostics_framework.md`
for why the Issue #196 diagnostic profile keeps running independently after
this promotion.

## 1. Background

`GET {CFS root}/commentaryFeed/{match_provider_id}` returns an accumulated,
newest-first array of commentary events for one match -- goals/behinds,
quarter markers, injury/interchange notes, and general narrative -- with no
upstream event identifier. Issue #196 built a diagnostic-only evidence
capture pathway to observe this endpoint's real behaviour before committing
to a production design. This document records that production design.

## 2. Confirmed production contract

See `docs/investigation/afl-json/ENDPOINT_CATALOG.md` §5 for the complete,
evidence-cited contract. Summary:

* `GET https://api.afl.com.au/cfs/commentaryFeed/{match_provider_id}` --
  authenticated (`x-media-mis-token`, reused from the existing CFS
  client/token provider), one directory above the `/cfs/afl` root.
* Response: `{"matchId": "...", "lastUpdated": "...", "commentaryEvent": [...]}`.
* Each event: `comment`, `periodNumber`, `periodSeconds`, `playerId`,
  `teamId`, `scoreEvent`. No additional structured scoring fields exist.
* Accumulated, newest-first, no event id. Multiple events can share one
  `(periodNumber, periodSeconds)` pair. `scoreEvent=true` events can have a
  null `playerId` (team-only, e.g. a rushed behind).
* `lastUpdated` can advance with no new event content -- never a substitute
  for fingerprint-based dedup.
* A genuine official score-review reversal was observed in the Round 24
  evidence set (a different match, `CD_M20260142406`, from the same capture
  run as the supplied `CD_M20260142409` evidence): an initial `GOAL` was
  followed by a `BEHIND` at the identical
  `(periodNumber, periodSeconds, playerId, teamId, scoreEvent)` slot. The
  original entry was never removed or rewritten.
* The feed remains queryable and stable well into POSTGAME/CONCLUDED.

## 3. Persistence and event-identity design

### 3.1 Why a new, separate production module

`afl_json/match_commentary.py` and `scheduler/match_commentary_production.py`
are new modules, not a promotion of the diagnostic ones. This mirrors the
Issue #187 precedent (`afl_json/match_period.py` sitting alongside
`collection/match_state_evidence.py` without touching it) and is required by
`docs/diagnostics_framework.md`'s core rule: diagnostic evidence must never
silently become production source authority. The diagnostic
`commentary_evidence_polls`/`commentary_evidence_events` tables (migration
`0018`) are completely unaffected by this work.

### 3.2 Schema (migration `0019`)

`match_commentary_events` -- one row per unique event, the table the consumer
API reads:

| Column | Purpose |
|---|---|
| `id` | AFL-api-generated stable event identity (see §3.3). Never a Champion Data id. |
| `match_id` | Canonical/internal match identity (FK `matches.match_id`). |
| `match_provider_id` | Source `matchId`. |
| `period_number`, `period_seconds` | Source match-clock coordinates. |
| `comment` | Original commentary text, verbatim. |
| `score_event` | Source `scoreEvent`, persisted exactly. |
| `player_provider_id`, `canonical_player_id` | Source Champion Data player id, and its resolved canonical link (nullable, never guessed). |
| `team_provider_id`, `canonical_team_id` | Source Champion Data team id, and its resolved canonical link (nullable, never guessed). |
| `category` | Best-effort, non-authoritative `quarter_start`/`quarter_end`/`score_event` label. |
| `source_index` | Position in the source array at first observation (ordering tiebreaker; see §5.2). |
| `possible_edit_of_event_id` | Heuristic, non-destructive link to an earlier event this one likely republishes/corrects (see §4). |
| `first_observed_at`, `last_observed_at` | UTC observation timestamps. |
| `source_feed_last_updated`, `last_seen_feed_last_updated` | Source `lastUpdated` at first/most-recent observation. |
| `source` | Constant provenance marker (`cfs_commentary_feed`). |
| `raw_event_json`, `collector_version` | Per-event raw payload (retained once, at first observation) and collector version for replay/debugging. |

`match_commentary_polls` -- lightweight per-match poll bookkeeping (sequence
continuity, endpoint outcome, feed metadata). Deliberately **never** stores a
raw payload -- unlike the diagnostic poll table, this is bookkeeping only,
to avoid duplicating the diagnostic evidence infrastructure.

### 3.3 Event identity and deduplication

The endpoint supplies no event id. This module reuses the diagnostic
module's proven, real-evidence-validated approach as an **independent
re-implementation** (not a shared import, per §3.1):

* **Fingerprint** (dedup key): SHA-256 over
  `(period_number, period_seconds, player_provider_id, team_provider_id,
  score_event, comment)`. An already-known fingerprint is never re-inserted
  -- only its `last_observed_*` bookkeeping is touched. This is what makes
  polling the accumulated feed idempotent: the same ~90-130 events returned
  on every poll of a live match never grow the table past actual event
  count.
* **Slot key** (edit-linkage key): the same tuple without `comment`. A new
  event whose fingerprint is genuinely new, but whose slot key matches an
  existing event, is linked via `possible_edit_of_event_id` to the most
  recently first-observed match -- **only** when the new event carries a
  non-null `player_provider_id`. This restriction is deliberate: unrelated
  narrative comments frequently share a null-player slot, so linking those
  would produce constant false "edit" signals; a player-attributed slot
  (typically scoring or interchange/injury commentary) is a much more
  precise anchor. This is exactly what the real `CD_M20260142406` review
  case validated (see §4).

Documented assumptions and limitations:

* two genuinely distinct events that are byte-identical across every
  fingerprinted field would collide and be treated as one event;
* event *removal* is not detected -- no evidence of removal has been
  observed in any capture to date, live or concluded;
* sub-second real-world ordering between two events sharing one
  `(period_number, period_seconds)` pair is inferred only from original
  array position (`source_index`), never confirmed by an independent clock
  (see §5.2);
* an event's canonical player/team link is resolved once, at first
  observation, and is not silently backfilled later if a crosswalk appears
  afterward -- a later crosswalk write is not evidence about the event's
  original resolution context; a deliberate backfill is a separate concern
  if ever needed.
* restart/replay safety follows directly from fingerprint idempotency and
  `poll_sequence` being recomputed from durable storage on every write --
  there is no in-memory state to lose or recover.

## 4. Canonical player/team linking

* **Match:** `match_provider_id` -> `matches.match_id` via the existing
  `matches.match_provider_id` column (the production scheduler already has
  both from its own match-window candidate query;
  `resolve_canonical_match_id` is provided for standalone/replay callers).
* **Player:** `playerId` -> canonical player via the existing
  `player_provider_ids` crosswalk (`provider='champion_data'`), the same
  crosswalk `afl_json.player_stats.upsert_player_stats` already reads.
* **Team:** `teamId` -> canonical team via the existing
  `afl_teams.provider_id` column, the same column
  `afl_json.player_persistence` already reads.
* Unresolved provider ids stay `NULL` in both persistence and the API
  response. Nothing in this module ever parses a player or team name out of
  the free-text `comment`.

## 5. Score-review / reversal preservation

### 5.1 The real evidence

Diagnostic capture across Round 24 recorded, for match `CD_M20260142406`:
`"GOAL - Bulldogs (Cody Weightman)"` at `period=3, seconds=839`, then on a
later poll `"BEHIND - Bulldogs (Cody Weightman)"` at the identical
`(period_number, period_seconds, playerId, teamId, scoreEvent)` slot. The
original `GOAL` entry was never removed or rewritten -- both remained in the
accumulated feed. `CD_M20260142409` itself (the match in the two files
supplied for this issue) shows no such sequence in either supplied file; see
`tests/fixtures/afl/commentary/commentary_CD_M20260142406_score_review.metadata.json`
for the full, honestly-labelled provenance of the reconstructed fixture used
to test this.

### 5.2 How it is preserved

* Both events persist as **separate rows** -- the fingerprint differs
  (different `comment`), so this is not a dedup collision.
* The later row's `possible_edit_of_event_id` points at the earlier row via
  the slot-key heuristic (§3.3).
* Neither row is ever updated, hidden, or deleted because of this link.
* The consumer API returns both, in chronological order (`period_number`
  then `period_seconds` ascending; within one clock second, `source_index`
  descending -- larger source index means an earlier position in a
  newest-first array, i.e. observed further back in time -- then `id`
  ascending as a final deterministic tiebreaker), with
  `possible_edit_of_event_id` exposed so a consumer can recognise and, if it
  chooses, present the correction.

This is a **heuristic, non-authoritative** link, named "possible" for the
same reason the diagnostic evidence is: two independent, unrelated events
could in principle collide on the same slot. It is surfaced, never used to
merge or hide data.

## 6. Production scheduler lifecycle

`scheduler/match_commentary_production.py`, registered unconditionally in
`scheduler/scheduled_tasks.py` (an `IntervalTrigger` job, same pattern as
the existing `player_stat_polling_planner`), gated only by its own
`AFL_COMMENTARY_PRODUCTION_ENABLED` flag -- **never** by
`AFL_DIAGNOSTICS_ENABLED`/`AFL_DIAGNOSTIC_PROFILES`.

* **Auth/CFS client:** the existing `AflJsonClient`/`WMCTokenProvider`
  (`afl_json/client.py`) -- same retry/backoff and single-token-refresh-on-401
  behaviour as every other CFS collector.
* **Candidate window:** reuses the same lightweight, stateless,
  self-terminating pattern already proven by the diagnostic profiles
  (`scheduler.match_state_capture._live_matches`/`_kickoff_tolerance_matches`,
  reused unmodified), extended with a `_postgame_matches` read and a
  self-contained post-active grace window
  (`recently_active_match_provider_ids`, reading this module's own
  `match_commentary_polls`). Candidates = `LIVE` ∪ `POSTGAME` ∪ bounded
  pre-kickoff tolerance ∪ bounded post-active grace. `POSTGAME` is always
  included (not just a grace window) because the real review-reversal
  evidence (§5.1) demonstrates commentary can still change after a match
  leaves `LIVE` but before it is finalised.
* **Why not the `match_stat_windows` lease system:** a deliberate, scoped
  choice -- see the module docstring for the full rationale. In short:
  commentary is explicitly non-authoritative and persistence is idempotent
  by fingerprint, so there is no correctness risk from overlapping polls
  (unlike authoritative player-stat writes); this process runs a single
  sequential poll loop, so there is no concurrent-worker scenario to
  arbitrate; and introducing a second lease-table consumer purely for a
  best-effort stream would be the "generic event-bus abstraction... without
  a strong present-day architectural reason" this issue's constraints
  explicitly ask to avoid.
* **Concurrency/write-lane:** `scheduler.write_lane.write_lane.execute(...)`
  -- the same serial write lane every other scheduler write goes through.
* **Retry/error handling:** identical outcome mapping to the diagnostic
  capture module (`not_published`, `auth_error`, `transport_error`,
  `invalid_response`, `http_error`, `malformed_payload`) -- one match's
  failure never blocks any other match in the same poll cycle, and the
  whole poll cycle never raises.
* **Cadence:** defaults to 20s (`AFL_COMMENTARY_PRODUCTION_INTERVAL_SECONDS`),
  slightly relaxed from the diagnostic profile's proven 15s -- Round 24
  evidence shows commentary events arrive far less often than every 15s, so
  this keeps consumer-visible latency low without polling meaningfully more
  often than the feed actually changes.
* **Restart/recovery:** no lease/audit machinery needed -- restart safety
  comes from fingerprint idempotency and durable `poll_sequence`
  recomputation, exactly like the diagnostic module.
* **Isolation from authoritative collection:** commentary availability or
  failure never affects match finality, player statistics, or any other
  production collection path -- this collector only ever reads `matches`
  and only ever writes its own two tables.

## 7. Consumer API contract

`GET /api/v1/matches/{match_id}/commentary` (`api/routes_v1.py`), using the
same canonical `match_id` convention as every other `/api/v1/matches/...`
route -- consumers never need a Champion Data match id. Authenticated via
the standard `authenticate_api_key` dependency (no extra capability
required, consistent with the base player-stats response).

Response:

```json
{
  "match": {"match_id": 9101, "match_provider_id": "CD_M20260142409", "round_id": 1, "season_id": 85, "status": "POSTGAME"},
  "events": [
    {
      "id": 42,
      "match_id": 9101,
      "period_number": 1,
      "period_seconds": 59,
      "comment": "GOAL - Hawks (Jack Gunston)",
      "score_event": true,
      "player": {"id": 501, "name": "Jack Gunston", "provider_id": "CD_I291351"},
      "team": {"id": 80, "name": "Hawthorn", "provider_id": "CD_T80"},
      "observed_at": "2026-08-23T09:21:29.024399+00:00",
      "possible_edit_of_event_id": null
    }
  ]
}
```

* **Ordering:** chronological, oldest-first by default (`period_number` then
  `period_seconds` ascending), even though the source feed is newest-first
  -- see §5.2 for the same-second tiebreak rule.
* **Nullability:** `player`/`team` are `null` when the source event carries
  no id, or when that provider id has no known canonical crosswalk yet --
  never guessed from `comment`.
* **Filters:** `period` (`period_number`), `player_id` (canonical),
  `team_id` (canonical), `score_events_only`. Chosen deliberately narrow --
  every one is efficiently backed by an index on `match_commentary_events`
  (see migration `0019`) and matches a concrete use case named in Issue
  #201; nothing broader was added to keep the first stable contract simple.
* **404:** unknown `match_id` returns the standard v1 structured error
  (`match_not_found`).
* **Not exposed:** diagnostic poll rows, repeated accumulated-feed
  observations, or raw provider payloads.

### 7.1 Input/output boundary

There is no public write endpoint. The only supported pathway is:

```text
input:  CFS commentaryFeed -> scheduler.match_commentary_production -> AFL-api persistence
output: AFL-api persistence -> GET /api/v1/matches/{match_id}/commentary -> consumers
```

`scripts/import_commentary_capture.py` is the explicitly-scoped internal/dev
replay mechanism Issue #201 asks for as the alternative: a CLI script,
never wired into `cli.py` or any HTTP route, requiring an explicit
`--source-label` provenance string for every import. It reuses the same
idempotent `persist_commentary_feed`, so replaying a fixture twice produces
zero new rows the second time.

## 8. Architecture constraints preserved

* Commentary text/`scoreEvent` never determine match finality, lifecycle,
  or authoritative player statistics -- those remain sourced from
  `matches.status`, `afl_json.match_status`, and `cfs_player_stats`
  respectively. This collector only ever reads `matches.status` (to record
  `match_status_at_poll` and select candidates) and never writes to it.
* Source ids are always preserved (`player_provider_id`, `team_provider_id`,
  `match_provider_id`); canonical identities are linked, never used to
  overwrite or discard the source id.
* No NLP/AI interpretation of `comment` is performed anywhere in this path
  -- `categorise_event`'s `quarter_start`/`quarter_end`/`score_event` labels
  are narrow, explicitly-anchored, non-authoritative convenience only, and
  `scoreEvent`/`playerId`/`teamId` are read structurally, never parsed from
  prose.
* The diagnostic evidence tables were not reused as the consumer API's
  backing store, even though they already existed -- see §3.1.
* No generic event-bus abstraction was introduced -- see §6's lease-system
  rationale.

## 9. Known limitations / follow-ups

Raised here rather than guessed around, per Issue #201:

* The supplied `CD_M20260142409` evidence (both files) shows **no**
  score-review sequence for that specific match -- the real reversal used
  to validate §5 came from a different match, `CD_M20260142406`, in the
  same capture set, reconstructed from the diagnostic text report's
  structured fields (not a raw array capture -- see the fixture metadata's
  provenance section). A raw, verbatim before/after array capture of an
  actual review sequence would be a stronger regression fixture than the
  current reconstruction, if one becomes available from a future match.
* `source_index`-based same-second ordering (§5.2) is a documented
  best-effort inference, not an independently confirmed sub-second clock.
  If a future capture ever contradicts the "lower array index is more
  recent" assumption, this ordering rule will need revisiting.
* Event *removal* has never been observed and is not handled -- if a future
  capture demonstrates the feed can shrink, that is new evidence requiring
  a deliberate follow-up decision, not something this design guesses at.
* `possible_edit_of_event_id` linking is restricted to player-attributed
  events. A team-only score-review reversal (e.g. correcting a rushed
  behind's team attribution) would not be linked by the current heuristic;
  no such case has been observed in the supplied evidence.
* Whether `commentaryFeed` first becomes available at a materially
  different time than `matchItem`/`matchInterchange` (relevant to how early
  the kickoff-tolerance window needs to start) was not independently
  re-measured for this issue -- the production scheduler reuses the same
  600s default the diagnostic profiles already validated live.
