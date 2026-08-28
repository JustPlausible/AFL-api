# AFL-api documentation

This directory contains active project documentation for operating, developing, and reviewing AFL-api.

The current repository version is **0.7.0**, declared authoritatively in
[`version.py`](../version.py). See the [v0.7.0 release notes](releases/v0.7.0.md)
for the reusable release summary and upgrade cautions.

## Operational and development guides

- [Production-like single-instance Docker deployment](operations/docker_deployment.md)
- [Reusable release, backup, restore, and rollback runbook](operations/release_runbook.md)
- [v0.7.0 release notes](releases/v0.7.0.md)
- [v0.7.0 pre-tag checklist](operations/v0.7.0-pre-tag-checklist.md)
- [Operator command selector and supported first-run sequence](cli.md#which-command-should-i-run) — the authoritative answer to “what command should I run?”
- [Operational AFL source policy and source matrix](operational_source_policy.md)
- [AFL data authority and identity map](architecture/data_authority_map.md)

- [Administrator CSRF protection](admin_csrf.md)
- [Admin manual scheduler triggers](admin_manual_triggers.md)
- [Admin Season Review](admin_season_review.md)
- [Admin Operations Dashboard](admin_operations_dashboard.md) — read-only system health, dataset completeness, and scheduler activity overview (Issue #225)
- [Admin AFL Data Explorer](admin_data_explorer.md) — read-only Season → Round → Match → Player inspection interface, with a shared completeness/state vocabulary (Issue #226)
- [API key storage migration](api_key_migration.md)
- [SQLite database migrations](database_migrations.md)
- [Scheduler registry and restart recovery](scheduler_registry.md)
- [Scheduler health and diagnostics endpoint](scheduler_health.md)
- [Scrape run audit records](scrape_run_audit.md)
- [Public AFL metadata collection](public_afl_metadata.md)
- [Match roster collection](match_rosters.md) — CFS `matchRosters` collector, canonical production persistence, replacement-safety rules, and the legacy HTML `lineups` authority boundary (Issue #219)
- [Match player-stat collection](match_player_stats.md)
- [Reviewed match data exceptions](match-data-exceptions.md) — candidate detection, auditable operator review, completeness, and revocation workflow
- [Player-stat persistence and authority contract](architecture/player_stats_storage_contract.md)
- [Consumer API workflow design](architecture/workflows/consumer_api_design.md) — human-led target for the complete versioned consumer surface
- [Canonical CFS player-stat read API design](architecture/api/player_stats_api_design.md) — endpoint-specific implementation design for the first versioned player-stat resource
- [Canonical player-stat API (`/api/v1`) — consumer reference](api_v1_player_stats.md) — `GET /api/v1/matches/{match_id}/player-stats`, Stage 1 of the design above
- [API v1 discovery and canonical seasons — consumer reference](api_v1_seasons.md) — start at `GET /api/v1` and discover persisted seasons through `GET /api/v1/seasons`
- [Canonical v1 rounds — consumer reference](api_v1_rounds.md) — navigate persisted season rounds and typed bye-team context
- [Canonical v1 matches — consumer reference](api_v1_matches.md) — complete the `/api/v1` season → round → match → player-stats navigation chain
- [Canonical v1 player lookup and search — consumer reference](api_v1_players.md) — resolve a canonical player ID to identity, provider crosswalks, and current-season team context; discover a `canonical_player_id` via `GET /api/v1/players?search=`; or navigate a player's season/team history via `GET /api/v1/players/{canonical_player_id}/seasons`
- [Production CFS match-commentary persistence and consumer API design](architecture/api/commentary_api_design.md) — event-identity/dedup, canonical linking, score-review preservation, and production scheduler lifecycle for `commentaryFeed` (Issue #201)
- [Canonical v1 match commentary — consumer reference](api_v1_commentary.md) — `GET /api/v1/matches/{match_id}/commentary`, chronological, canonically-linked commentary events
- [Injury collector pipeline architecture](architecture/injury_collector_pipeline.md) — acquisition/parser/resolver/persistence/orchestration boundaries, observed-team-authority persistence semantics, and canonical identity persistence (Issue #213)
- [Canonical v1 current injuries — consumer reference](api_v1_injuries.md) — `GET /api/v1/injuries`, filterable by `team_id`/`canonical_player_id` (Issue #213)
- [Canonical v1 match rosters — consumer reference](api_v1_rosters.md) — `GET /api/v1/matches/{match_id}/rosters`, selection/change-context separation, and why a selection is never participation evidence (Issue #219)
- [JSON payload fixtures and offline contract regression tests](json_contract_fixtures.md)
- [Modular analytics/telemetry framework](analytics_framework.md) — upstream-collection and consumer-API telemetry, storage/retention, privacy rules, and how to add a new analytics module (Issue #205)
- [Collection-to-consumer value mapping](architecture/collection_to_consumer_map.md) — the maintained map from upstream datasets through persistence to `/api/v1` exposure and usage classification (Issue #205)

## Reviews and planning

- [Post-v0.5.0 engineering status review](architecture/project_status_post_v0_5_0.md) — current baseline and candidate planning themes; not an approved v0.6.0 roadmap
- [Architectural reviews](architecture/README.md)
