# AFL data authority and identity map

Use this map to choose a source, persistence model and identifier namespace. It
records implemented authority; a preferred future concept is not permission to
persist, fall back or dual-write.

## Domain authority map

| Domain | Preferred source | Authoritative persistence | Legacy/diagnostic model | Primary identifiers | Current caveat |
| --- | --- | --- | --- | --- | --- |
| Competitions and seasons | Public AFL JSON | `afl_competitions`, `afl_seasons` | Database-free metadata collection is read-only | Numeric AFL `afl_id`; opaque `provider_id` | API coverage is incomplete. |
| Clubs and teams | Canonical club seed for editorial club identity; Public AFL JSON for participating teams | `clubs`; `afl_teams` and `afl_team_seasons` | Legacy player/profile club fields and HTML enrichment remain separate | Canonical `clubs.code`; numeric team `afl_id`; opaque team `provider_id` | A club and a provider team/season entry are related concepts, not interchangeable identities. |
| Rounds and matches | Public AFL JSON | `rounds`, `matches` | HTML fixture/match collection is explicit legacy or diagnostic work, never automatic fallback | Numeric `round_id`/`match_id`; opaque round `provider_id` and `matches.match_provider_id` | Preserve numeric and opaque IDs; do not guess across dialects. |
| Players and season membership | CFS season players plus the public AFL player ID map | `canonical_players`, `player_provider_ids`, `competition_season_players`; team links through `afl_team_seasons` | `players` is the legacy profile/enrichment model | Internal canonical player `id`; namespaced AFL numeric and Champion Data player IDs | Bootstrap is CLI-only; `team_id` may remain null when unresolved. |
| Player statistics | Authenticated CFS JSON | `cfs_player_stats` (current/final); `cfs_player_stat_history` (append-only observed field transitions) and `cfs_player_stat_checkpoints` (sparse period/finality snapshots) are additive, derived-from-the-same-write-path records, never an alternate authority | HTML `player_stats` is compatibility/diagnostic storage | Opaque `match_provider_id` and `champion_data_player_id`; optional `canonical_player_id`; supplied numeric `afl_match_id` | New reads must not silently use `player_stats`; the current API still exposes that compatibility model. History/checkpoints record observed transitions only -- see [player-stat persistence and authority contract](player_stats_storage_contract.md#append-only-history-and-period-checkpoints-issue-195). |
| Rosters (match selections) | Authenticated CFS JSON `matchRosters/round` | `cfs_match_rosters` (per-team status/metadata) / `cfs_match_roster_selections` (current positional lineup) / `cfs_match_roster_context` (current `ins`/`outs`/`lateChanges`/`clubDebuts`/`milestones`), migration `0024` | Database-free batch output (`afl_json.orchestration`) remains a separate diagnostic/inspection path | `matches.match_provider_id`; `afl_teams.provider_id`; opaque Champion Data `playerId`; optional `canonical_player_id`/`canonical_team_id` | A CFS roster is a **team selection, not proof of participation** — never treated as authoritative for match finality, lifecycle, or player statistics (Issue #219). Current-state persistence only (no separate history table); replacement requires a `RosterStatus.PUBLISHED` observation — see [match roster collection](../match_rosters.md#replacement-and-supersession-safety-issue-219). This is a distinct authority from the Lineups row below; neither falls back to the other. |
| Lineups (legacy) | Rendered AFL HTML for the operational persistent path | `lineups` | CFS roster/lineup-shaped output is file-only/read-only | Legacy `match_id`, numeric `afl_id`, team text and optional `champion_id` | Unchanged by Issue #219: this remains a separate legacy/compatibility model, not canonical roster history. No automatic migration, fallback, or dual-write exists between this row and the Rosters row above — see [match roster collection](../match_rosters.md#legacy-lineups-boundary). |
| Injuries | Rendered AFL HTML | Canonical-resolved current/history rows in `injuries` | Parse and identity-resolution diagnostics retain unsafe rows without persisting them | Numeric AFL player `afl_id`; canonical club code during resolution | Only resolved identities persist; unresolved or ambiguous identities are never guessed. |
| Audit, scheduler and diagnostic data | Operational orchestration and scheduler registry | `scrape_runs`, `scheduler_job_registry` | `scrape_log` and `scrape_summary` are older audit models; database-free file summaries are diagnostic | Run/correlation IDs; scheduler `job_id`; explicit target identifiers | Read-only/database-free collection intentionally does not write an audit row. |
| Match period (quarter/break) state | CFS `matchItem` `score.matchClock.periods` (regulation periods 1-4 only) | **None** -- derived on demand, not persisted | `match_clock` diagnostic evidence capture (`collection/match_state_evidence.py`) is the separate, ongoing raw-evidence table | `matches.match_provider_id`; internal `MatchPeriodState` (`Q1, QT, Q2, HT, Q3, 3QT, Q4, FT, UNKNOWN`) | Informational only (`afl_json/match_period.py`); never a scheduler finality/lease/recovery authority, and never itself implies `CONCLUDED`. `matches.status` remains sole lifecycle authority. Extra time/suspended/abandoned matches are unverified and degrade to `UNKNOWN`. |
| Match commentary | Authenticated CFS `commentaryFeed` | `match_commentary_events` / `match_commentary_polls` (migration `0019`) | `commentary` diagnostic evidence capture (`collection/match_commentary_evidence.py`) is the separate, ongoing raw-evidence table | `matches.match_provider_id`; opaque Champion Data `playerId`/`teamId`; optional `canonical_player_id`/`canonical_team_id` | Non-authoritative for match finality, lifecycle, or player statistics (Issue #201). `possible_edit_of_event_id` is a heuristic, non-destructive link, never a rewrite of the earlier row. |
| Match interchange (bench) state | Authenticated CFS `matchInterchange` | `match_interchange_state` (current) / `match_interchange_events` (transition history) / `match_interchange_polls` (migration `0021`) | `interchange` diagnostic evidence capture (`collection/match_interchange_evidence.py`) is the separate, ongoing raw-evidence table | `matches.match_provider_id`; opaque Champion Data `playerId`/`teamId`; optional `canonical_player_id`/`canonical_team_id` | Non-authoritative for match finality, lifecycle, or player statistics (Issue #204). `on_bench` is confirmed against real Round 24 live evidence for LIVE play (array membership changes, correlated with team rotation counts, individually cited for a named player's repeated round trips) and confirmed to freeze byte-for-byte through POSTGAME; only CONCLUDED behaviour remains open -- see `docs/api_v1_interchange.md`. `bench_reason` is persisted verbatim, never inferred. |

## Identifier guide

- **Internal SQLite row IDs** identify rows only inside the relevant model. For
  example, `canonical_players.id` is not an AFL or Champion Data identifier.
- **Numeric AFL identifiers** identify AFL entities such as players, teams,
  competitions, seasons, rounds and matches. Keep the entity and namespace
  explicit; equal-looking numbers do not establish a relationship.
- **Opaque Champion Data/CFS identifiers** (for example `CD_M...` matches and
  `CD_I...` players) are provider values and must remain text. Do not parse a
  numeric identity out of them.
- **Canonical player IDs** are internal `canonical_players.id` values.
  `player_provider_ids` is the only validated player crosswalk: query it by
  `provider` and `provider_player_id`, and leave identity unresolved when no
  mapping exists.
- **Canonical club codes** come from the versioned club seed and identify the
  editorial club record. Editorial aliases support controlled name resolution;
  they are not provider IDs or join keys. Provider team IDs identify an
  `afl_teams` participation record and remain separate from club codes.
- **Competition and season IDs** retain numeric AFL `afl_id` values and opaque
  `provider_id` values in distinct columns. Use the namespace required by the
  source or foreign-key contract.

Never infer equivalence between namespaces. Preserve both numeric and opaque
match identifiers when a source supplies both; do not join canonical and legacy
tables by player or team names alone; do not treat compatibility statistics
tables as authority for new reads; and do not invent mappings for unresolved
identities.

## When adding a feature

Answer these questions before choosing a collector, table or join:

1. Which source is authoritative for the domain?
2. Is the operation persistent, read-only or diagnostic?
3. Which identifier namespace does its contract require?
4. Does a validated canonical crosswalk already exist?
5. Is the target table current authority or compatibility-only?
6. Is fallback explicitly authorised?

Automatic fallback and dual writes are not the default policy. They require an
explicit per-domain contract; collection failure does not authorise either.

## Detailed contracts

- [Operational AFL source policy](../operational_source_policy.md)
- [Player-stat persistence and authority contract](player_stats_storage_contract.md)
- [Public AFL metadata collection](../public_afl_metadata.md) — canonical season
  bootstrap and player identity
- [AFL scraper source inventory and page contracts](../scraper_source_inventory.md)
- [Injury collector pipeline and reference boundaries](injury_collector_pipeline.md)
- [SQLite database migrations](../database_migrations.md)
- [Post-v0.5.0 engineering status review](project_status_post_v0_5_0.md) — the
  active current-state review; the v0.5.0 architectural review is a pre-release
  historical assessment
