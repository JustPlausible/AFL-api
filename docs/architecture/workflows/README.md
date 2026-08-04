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

The scheduler composes the same source-policy and collector boundaries used by
season synchronisation. The season workflow remains the bounded bulk-loading
design; the scheduler workflow governs ongoing, time-sensitive collection.
