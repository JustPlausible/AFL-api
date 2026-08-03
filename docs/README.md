# AFL-api documentation

This directory contains active project documentation for operating, developing, and reviewing AFL-api.

The current repository version is **0.5.0**, declared authoritatively in
[`version.py`](../version.py). See the [v0.5.0 release notes](releases/v0.5.0.md)
for the reusable release summary and upgrade cautions.

## Operational and development guides

- [Production-like single-instance Docker deployment](operations/docker_deployment.md)
- [AFL-api v0.5.0 operator release, backup, restore, and rollback runbook](operations/release_runbook_v0_5_0.md)
- [v0.5.0 release notes](releases/v0.5.0.md)
- [Operator command selector and supported first-run sequence](cli.md#which-command-should-i-run) — the authoritative answer to “what command should I run?”
- [Operational AFL source policy and source matrix](operational_source_policy.md)
- [AFL data authority and identity map](architecture/data_authority_map.md)

- [Administrator CSRF protection](admin_csrf.md)
- [Admin manual scheduler triggers](admin_manual_triggers.md)
- [API key storage migration](api_key_migration.md)
- [SQLite database migrations](database_migrations.md)
- [Scheduler registry and restart recovery](scheduler_registry.md)
- [Scrape run audit records](scrape_run_audit.md)
- [Public AFL metadata collection](public_afl_metadata.md)
- [Match roster collection](match_rosters.md)
- [Match player-stat collection](match_player_stats.md)
- [Player-stat persistence and authority contract](architecture/player_stats_storage_contract.md)
- [JSON payload fixtures and offline contract regression tests](json_contract_fixtures.md)

## Reviews and planning

- [Post-v0.5.0 engineering status review](architecture/project_status_post_v0_5_0.md) — current baseline and candidate planning themes; not an approved v0.6.0 roadmap
- [Architectural reviews](architecture/README.md)
