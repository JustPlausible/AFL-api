# Architectural reviews

This directory contains historical engineering and architectural assessments for AFL-api releases.

Active design areas:

- [Workflow designs](workflows/README.md) define human-led, cross-component
  behaviour and implementation targets.
- [API implementation designs](api/README.md) translate the consumer API
  workflow into bounded endpoint and interface specifications.

- [AFL data authority and identity map](data_authority_map.md) — concise active
  contributor reference for source, persistence and identifier choices.

- [Engineering status and scheduler-readiness review](project_status_scheduler_readiness.md)
  — current `main` assessment, scheduler gap analysis, staged collection model,
  and recommended next milestone boundary.
- [Post-v0.5.0 engineering status review](project_status_post_v0_5_0.md) — the
  historical baseline after completion of the recorded v0.5.0 backlog,
  including the injury pipeline refactor. Recommendations are planning
  candidates, not an approved next-release roadmap.
- [Injury collector pipeline and reference boundaries](injury_collector_pipeline.md)
- [v0.5.0 release-readiness review](release_readiness_v0_5_0.md)
- [v0.5.0 pre-release engineering and architectural review](architectural_review_v0_5_0.md)
- [v0.4.0 architectural review](architectural_review_v0_4_0.md)
