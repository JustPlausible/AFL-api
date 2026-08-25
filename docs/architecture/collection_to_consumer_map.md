# Collection-to-consumer value mapping (Issue #205)

This is a maintained, reviewable map from upstream AFL/CFS datasets through
persistence to `/api/v1` consumer exposure, and a deliberate judgement of
each row's current usage/value category. It is documentation, not runtime
state: Issue #205 is explicit that "collected and consumed" vs. "collected
and presently unused" must not be inferred purely from request counts, so
this table records a considered classification rather than an automatically
computed one. Analytics data (`docs/analytics_framework.md`) is *evidence*
for keeping this table honest over time, not a substitute for it.

## Value categories

* **consumed** -- collected, exposed via `/api/v1`, and known to have
  demonstrated downstream use.
* **exposed/unused** -- collected and exposed via `/api/v1`, but without
  demonstrated consumer use yet. Not a removal candidate by itself --
  Issue #205 explicitly says low current usage does not prove a collector
  should be removed.
* **future** -- collected (or planned) for a capability that is not yet
  exposed, or exposed but intentionally ahead of adoption.
* **diagnostic-only** -- collected purely to investigate upstream provider
  behaviour; never exposed via `/api/v1` and never intended to be.

## Map

| Upstream dataset | Persistence | Consumer resource | Category | Notes |
| --- | --- | --- | --- | --- |
| Public AFL JSON: competitions/seasons/rounds/teams/matches | `afl_competitions`, `afl_seasons`, `rounds`, `matches`, `afl_teams` | `GET /api/v1`, `/seasons`, `/seasons/{id}/rounds`, `/rounds/{id}`, `/rounds/{id}/matches`, `/matches/{id}` | consumed | The whole `/api/v1` navigation chain (season -> round -> match) is built directly on this data; it is the entry point for every other resource below. |
| CFS `playerStats/match/{id}` | `cfs_player_stats` (+ `cfs_player_stat_history`/`cfs_player_stat_checkpoints`) | `GET /api/v1/matches/{match_id}/player-stats` | consumed | The original, highest-frequency production collector; `?advanced=true` exposes the append-only history/checkpoint data behind a capability gate. |
| Public AFL JSON player ID map + CFS season players | `canonical_players`, `player_provider_ids`, `competition_season_players` | `GET /api/v1/players`, `/players/{id}`, `/players/{id}/seasons` | consumed | Backs player search/lookup and the season/team navigation added by Issue #182. |
| CFS `commentaryFeed/{id}` (production path, Issue #201) | `match_commentary_events`/`match_commentary_polls` | `GET /api/v1/matches/{match_id}/commentary` | exposed/unused | Shipped and correct, but current downstream demonstrated use is not yet established -- a natural first question for the analytics `consumer_summary()` report once real traffic accumulates. |
| CFS `matchInterchange/{id}` (production path, Issue #204) | `match_interchange_state`/`match_interchange_events`/`match_interchange_polls` | `GET /api/v1/matches/{match_id}/interchanges`, `/interchanges/events` | exposed/unused | Same situation as commentary: shipped, correct, usage not yet demonstrated. |
| Rendered AFL HTML injury list | `injuries` (canonical-resolved) | `GET /api/v1/injuries` | consumed | Filterable by `team_id`/`canonical_player_id` (Issue #213). |
| CFS `matchItem` (`score.matchClock.periods`) | Not persisted -- derived on demand (`afl_json/match_period.py`) | Used internally by player-stat finality/checkpoint logic; no direct `/api/v1` route | future | Informational match-period state exists to support internal reconciliation today; a direct consumer-facing period/clock resource would be new scope, not yet built. |
| CFS `matchRosters/round/{id}` | Read-only/database-free (no canonical persistence yet, per `docs/architecture/data_authority_map.md`) | none | future | Explicitly not implemented as canonical persistence; would need its own design before any `/api/v1` exposure. |
| CFS `matchItem` via the `match_clock` diagnostic profile | `match_state_evidence_observations` | none -- never exposed | diagnostic-only | Investigation tooling for quarter/half/full-time transition behaviour (Issue #148); explicitly never a production or consumer data source. |
| CFS `matchInterchange` via the `interchange` diagnostic profile | `match_interchange_evidence_observations` | none -- never exposed | diagnostic-only | Independent of the production interchange path above; kept running for parser-regression evidence and to gather the still-open CONCLUDED-behaviour question. |
| CFS `commentaryFeed` via the `commentary` diagnostic profile | `commentary_evidence_polls`/`commentary_evidence_events` | none -- never exposed | diagnostic-only | Independent of the production commentary path above; kept for the same reasons. |

## One consumer resource backed by multiple sources

`GET /api/v1/matches/{match_id}` itself is the clearest example of this
pattern: it composes public AFL JSON match/round/season identity with
canonical team data, without any single upstream dataset being sufficient
on its own. As further production resources are added, prefer recording
that composition explicitly in this table's Notes column over inventing a
new mapping mechanism.

## Keeping this table honest

Update this table whenever:

* a diagnostic investigation is promoted to a production collector (follow
  the precedent already established for commentary/interchange -- add a
  new row, keep the diagnostic-only row for the underlying diagnostic
  profile, since the two remain independent per
  `docs/diagnostics_framework.md`);
* a new `/api/v1` resource ships;
* analytics evidence (`scripts/report_analytics.py`, or the Admin
  `/analytics` page) changes the honest answer to "does this have
  demonstrated use" for an existing row -- moving a row from
  exposed/unused to consumed (or the reverse, if usage genuinely stops) is
  a deliberate editorial decision informed by that evidence, not an
  automatic recomputation.
