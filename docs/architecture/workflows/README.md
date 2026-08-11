# Architecture workflow designs

Workflow documents define intended orchestration behaviour across entry points,
services, collectors, persistence, and operator controls. They are
human-authored implementation targets from which focused Issues and pull
requests can be derived; they do not by themselves describe shipped behaviour.

## Available workflows

- [Season synchronisation workflow](season_sync_design.md) defines discovery,
  planning, persistence, and reporting for whole-season synchronisation. It also
  contains the proposed guided first-run workflow; there is no separate
  first-run design document.
- [Scheduler workflow](scheduler_workflow_design.md) defines the state-aware
  scheduling target, including match-domain planning, lifecycle transitions,
  recovery, observability, and the operator experience.
- [Consumer API workflow](consumer_api_design.md) defines the human-led target
  for the complete read-only consumer surface, including contract boundaries,
  resource navigation, response conventions, security, finality, legacy
  migration, and the incremental v1 roadmap.

The endpoint-specific [Canonical CFS player-stat read API design](../api/player_stats_api_design.md)
is retained under `architecture/api/` as the implementation design for the
first versioned player-stat resource. Stage 1,
`GET /api/v1/matches/{match_id}/player-stats`, is implemented; see the
[consumer reference](../../api_v1_player_stats.md) for shipped behaviour.
The foundational navigation stage begins with the authenticated discovery root
and canonical seasons resource; see the
[v1 discovery and seasons consumer reference](../../api_v1_seasons.md).
Canonical round navigation is also implemented; see the
[v1 rounds consumer reference](../../api_v1_rounds.md) for the shipped typed
bye-team contract.

The scheduler composes the same source-policy and collector boundaries used by
season synchronisation. The season workflow remains the bounded bulk-loading
design; the scheduler workflow governs ongoing, time-sensitive collection; the
consumer API composes the same authoritative persistence and finality logic as
a read-only entry point, without introducing new orchestration, persistence,
or scheduling behaviour.
