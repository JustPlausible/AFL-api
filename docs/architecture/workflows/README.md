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
- [Canonical CFS player-stat read API design](player_stats_api_design.md)
  defines the first versioned, consumer-facing read surface over authoritative
  CFS player statistics, including identifier semantics, authoritative joins,
  lifecycle/finality semantics, the stable-field boundary around
  `extra_stats_json`/`raw_player_json`, and a staged implementation plan.
  Stage 1, `GET /api/v1/matches/{match_id}/player-stats`, is implemented; see
  the [consumer reference](../../api_v1_player_stats.md) for the shipped
  endpoint.

The scheduler composes the same source-policy and collector boundaries used by
season synchronisation. The season workflow remains the bounded bulk-loading
design; the scheduler workflow governs ongoing, time-sensitive collection; the
player-stat API design composes the same authoritative persistence and
finality logic as a new read-only entry point, without introducing new
orchestration, persistence, or scheduling behaviour.
