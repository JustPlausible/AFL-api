# Admin Operations Dashboard

Open **Operations** in the authenticated Admin interface (`GET /operations`) for
a single, read-only page that answers: *is the AFL API healthy, is expected
collection occurring, and where should I look if not?*

This is **operational visibility, not infrastructure monitoring**. It says
nothing about host, container, or database-server health -- use your normal
infrastructure monitoring for that. It is strictly **read-only**: opening the
page never collects, reconciles, repairs, triggers a job, or changes
configuration. There are no retry/restart/repair controls on this page, by
design (Issue #225).

## What the page shows

1. **System overview** -- database reachability, scheduler health (reusing the
   same validated contract as the Scheduling page's "Scheduler health" card,
   see [scheduler health and diagnostics](scheduler_health.md)), the
   configured/current AFL season and round, the most recent successful
   collection timestamp across every scheduler job type and the durable
   player-statistics collection plan, and a single overall state.
2. **Attention** -- a bounded (at most 25), severity-sorted list of findings
   that need a look, each linking to the existing Admin page that can explain
   it further (Season Review, Scheduling, or a data table). This is an
   investigation summary, not a replacement for those pages.
3. **Dataset health** -- one row per major supported dataset: seasons, teams,
   rounds & fixtures, matches, player statistics, canonical players & season
   memberships, match rosters/lineups, commentary, interchange, and injuries.
4. **Scheduler & collector activity** -- job-type activity from the persisted
   scheduler job registry, the durable per-match player-statistics collection
   plan (`match_stat_windows`), and diagnostic evidence-capture profile status.

## Health states

Every dataset and the overall system state is one of:

| State | Meaning |
| --- | --- |
| `healthy` | Present, fresh, and consistent with the dataset's current lifecycle expectation. |
| `upcoming` | Not yet expected -- e.g. an unstarted match has no commentary yet, or a round's lineups have not reached their scheduled collection time. |
| `attention` | Present but incomplete, inconsistent, or a scheduled job is failing/skipped; worth a look. |
| `partial` | Some but not all of a season-scoped dataset is complete (reserved for future finer-grained rules; the current season-scoped datasets resolve to `healthy`/`attention`/`upcoming` directly). |
| `stale` | Present, but older than expected for a dataset with no strict lifecycle gate (currently only injuries). |
| `missing` | Expected but absent, or too old to be useful (e.g. injuries never collected, or not refreshed in a very long time). |
| `failed` | An authoritative source (a scheduler job, the season report, or the scheduler health contract) reports a hard failure. |
| `unknown` | The repository does not yet have a sufficiently authoritative rule to classify this -- a deliberately conservative default, never guessed. |

There is **no invented percentage-based health score**. States are derived
from a small, explicit precedence table (see
`OperationsDashboardReporter._overall_state` in
`operations/dashboard.py`), the same style already used by
`afl_json.season_report.calculate_status`.

### Missing vs. not-yet-expected

This is the distinction the issue asked to get right, and it is made using
data the system already has, not a new invented rule:

- A **match's lifecycle status** (`SCHEDULED`/`LIVE`/`POSTGAME`/`CONCLUDED`,
  via `afl_json.match_status.normalise_match_status`) decides whether
  commentary/interchange data is expected yet for that match: a scheduled
  match with no commentary is `upcoming`, not `attention`; a *concluded*
  match with no commentary is `attention`.
- **Roster/lineup** expectation is read directly from the persisted scheduler
  job registry (`scheduler_job_registry`, `job_type='lineup'`) for the
  current round's jobs, rather than a newly invented "hours before bounce"
  rule -- the scheduler (`scheduler/schedule_lineup_scrapes.py`) already
  encodes when a roster is expected to be collected.
- **Player statistics** reuse the exact same authoritative-snapshot rules as
  Season Review (`afl_json.season_report`): a concluded match without an
  authoritative two-sided CFS snapshot is `attention`; a season with no
  concluded matches yet is `upcoming`.
- **Injuries** are not lifecycle-gated (they are a standing auxiliary feed),
  so their state is a straightforward freshness check plus the daily
  scheduler job's own reported success/failure.

## What is reused vs. what is new

Reused as-is, unchanged:

- `afl_json.season_report.SeasonCompletenessReporter` -- the same
  season-scoped report and finding codes shown on
  [Admin Season Review](admin_season_review.md), scoped to the
  configured/current season only.
- `afl_seasons.is_current` / `afl_seasons.current_round_number`, populated by
  season-sync persistence (see the [seasons API](api_v1_seasons.md)) --
  never independently recalculated.
- The scheduler health contract (`admin._fetch_scheduler_health`,
  [scheduler health and diagnostics](scheduler_health.md)) -- fetched once
  per page load, exactly as the Scheduling page already does.
- Diagnostic evidence-capture profile registration and `.status()`
  (`diagnostics/framework.py`).

New, small, bounded additions introduced for this page:

- `scheduler.registry.job_type_activity_summary` -- a single `GROUP BY
  job_type` aggregate over the scheduler job registry.
- `scheduler.match_windows.status_summary` -- a single `GROUP BY status`
  aggregate over the durable player-statistics collection plan
  (`match_stat_windows`), optionally scoped to one season.
- A handful of round-scoped aggregate queries in `operations/dashboard.py`
  for rosters, commentary, and interchange, and a global freshness check for
  injuries.

## Performance and isolation

The whole page runs on **one read-only SQLite connection**
(`db.connection.get_read_only_db_connection`, `PRAGMA query_only = ON`), so a
slow or failing dashboard request can never write to, lock, or otherwise
interfere with ingestion. Every query is either:

- a season-scoped report (bounded to the current season, the same cost as
  loading Season Review for that season), or
- a single bounded aggregate (`GROUP BY`) over a scheduler-owned table, or
- a query scoped to the single current/most relevant round's matches (at
  most a handful of rows, never a per-match loop).

Nothing on this page scans the full match/statistics history, and nothing
here is on the ingestion write path.

## Known `unknown` states (first-pass scope)

A few things are deliberately left `unknown` rather than guessed, because the
repository does not yet have an authoritative rule for them:

- Round-scoped datasets (rosters, commentary, interchange) when no current
  round can be resolved at all (no season marked current, or a resolved
  round has no persisted matches).
- Roster/lineup state for a round with no scheduler registry evidence, even
  though a round is "in progress" or "recently concluded" -- absence of a
  registry row is not treated as proof of failure, since the registry may
  predate a match or have been pruned.
- Live/postgame (not-yet-concluded) commentary/interchange rows with zero
  observations so far are `unknown`, not `attention`: partway through a live
  match, "nothing yet" is not distinguishable from "not collecting" without a
  stricter per-match SLA than currently exists.

## Follow-up ideas (not in this PR)

- A future **AFL Data Explorer** (Season → Round → Match → Player/dataset
  navigation) is the natural richer drill-down target for this dashboard's
  links; today they point at the existing Season Review, Scheduling, and
  table-browser pages.
- A precise, provider-confirmed roster-availability policy (rather than
  reading it off scheduler job outcomes) could replace the current
  registry-based roster state once one exists.
- Provider-specific/optional-feed nuance (e.g. distinguishing "this provider
  never supplies this dataset for this competition" from "not observed yet")
  is not modelled; everything not covered above stays `unknown` rather than
  guessing provider behaviour.
