# Admin AFL Data Explorer

Open **Data → Data Explorer** in the authenticated Admin interface to inspect
the AFL data AFL-api has already persisted, using the natural AFL hierarchy:

```
Season → Round → Match → Player / dataset detail
```

## Routes

| Page | Route |
|---|---|
| Season list | `GET /data-explorer` |
| Season detail (rounds) | `GET /data-explorer/seasons/{season_id}` |
| Round detail (fixture, matches) | `GET /data-explorer/seasons/{season_id}/rounds/{round_id}` |
| Match detail | `GET /data-explorer/matches/{match_id}` |
| Player detail | `GET /data-explorer/players/{player_id}` |

Every route is a stable, deep-linkable GET keyed on the same canonical
identifiers already used elsewhere in Admin (`afl_seasons.afl_id`,
`rounds.round_id`, `matches.match_id`, `canonical_players.id`) and the
`/api/v1` consumer surface. An unknown identifier returns `404`. This makes
the explorer a natural deep-link target for the
[Admin Operations Dashboard](admin_operations_dashboard.md)'s per-season/round/
match findings, without requiring any change to how that dashboard computes
its own findings.

## Read-only, always

Like [Season Review](admin_season_review.md) and the
[Operations Dashboard](admin_operations_dashboard.md), every explorer page
opens a read-only SQLite connection and renders only already-persisted data.
Viewing any explorer page never invokes an AFL/Champion Data client,
collector, bootstrap, sync, reconciliation, or scheduler trigger. There is no
edit, delete, or manual-collection action anywhere in the explorer.

## Completeness and availability states

The explorer never invents a second definition of "is this data healthy?".
Every state shown reuses the same building blocks already used elsewhere in
Admin:

* **Season status** (`complete` / `usable_with_warnings` / `incomplete` /
  `invalid`) is exactly `afl_json.season_report.SeasonCompletenessReporter`'s
  report status for that season — the same report shown on Season Review and
  summarised on the Operations Dashboard.
* **Round status** reduces that same season report's findings, filtered to
  the matches in that round, through `operations.dashboard.state_from_findings`
  — the identical severity → state decision the Operations Dashboard applies.
* **Per-match dataset state** (rosters, player statistics, commentary,
  interchange) reuses `operations.dashboard.dataset_presence_state`, the same
  lifecycle-aware rule already applied at round granularity by the Operations
  Dashboard, generalised to one match. Player-statistics finality specifically
  reuses `afl_json.season_report.evaluate_authoritative_stats_finality` (CFS
  `snapshot_authority`/coverage rules), the same predicate the `/api/v1`
  player-stats resource uses for its `final`/`partial`/`not_available`
  lifecycle.

Templates only ever render an already-determined `state` and `state_summary`
— they never re-derive completeness from raw counts.

The states shown are:

| State | Meaning |
|---|---|
| **Complete** | Concluded, and the dataset is present and satisfies the same finality/coverage rule used elsewhere (e.g. two-sided authoritative player stats). |
| **Partial** | Some data is present but does not yet satisfy that rule (e.g. one-sided or mixed-authority statistics, or a live match still being collected). |
| **Missing** | The match has concluded and the dataset is still absent — a genuine gap, not a timing issue. |
| **Upcoming** | The match has not started yet, so the dataset is **not yet expected** — never shown as a failure. |
| **Needs attention** | A live/in-progress match's dataset is empty so far — ambiguous, not yet a confirmed problem. |
| **Unsupported / unknown** | No authoritative rule exists yet to judge this dataset (or it is present only as secondary diagnostic evidence). |

This is the key distinction the explorer is designed to make clear: a future
match with no commentary or player statistics reads as **Upcoming**, not
**Missing** — those are different states, never conflated.

## What each page shows

* **Season list** — one card per persisted season: year/name, current-season
  flag, round/match/team counts, and the season's completeness status.
* **Season detail** — the season's rounds in fixture form (round label, date
  range, match/lifecycle counts, compact round state); selecting a round
  drills into its matches.
* **Round detail** — the round's matches as cards (teams, lifecycle badge,
  score, venue, compact overall match state) and any bye team.
* **Match detail** — the primary inspection page, organised into sections:
  dataset completeness (one card per dataset with its state and summary),
  rosters/lineups (selections plus ins/outs/late changes/club debuts/
  milestones per side), player statistics (canonical stat columns, linked to
  each player), commentary (the most recent events plus a total count —
  never the full table), interchange (current per-player bench state), and a
  secondary "Provider identifiers & evidence" section (Champion Data match
  ID, latest correlated collection run, and — only when present — diagnostic
  evidence-capture counts). Quarter/period coverage is summarised from
  `cfs_player_stat_checkpoints` (players observed at each `QT`/`HT`/`3QT`/`FT`
  marker) rather than rendered as a raw per-poll table.
* **Player detail** — canonical identity, known AFL/Champion Data provider
  IDs, season/team memberships, and recent match involvement, each match
  linking back to its match detail page. This is inspection/traceability
  only — no analytics or fantasy scoring.

Player names are navigable links to the player detail page wherever they
appear in roster, statistic, or commentary views. Canonical AFL names, teams,
rounds, and formatted dates/times are the primary presentation; provider IDs
and raw evidence stay secondary and are grouped under "Provider identifiers &
evidence".

## Query shape

Every page is scoped to the identifiers in its URL — a season page never
scans another season's matches, a match page never loads a whole evidence
table. Commentary and interchange reads are already bounded to one match's
rows (each is a small, indexed, per-match table); the match page additionally
caps the rendered commentary preview to the most recent 20 events alongside
the true total count. Season/round-level completeness reuses the single
bounded `SeasonCompletenessReporter` query set for that season rather than
issuing a query per match.

## Implementation

The reporting/query logic lives in `operations/data_explorer.py`
(`DataExplorerReporter`), which composes existing repository/service code
rather than duplicating it: `afl_json.season_report`, `afl_json.rosters`,
`afl_json.match_commentary`, `afl_json.match_interchange`, and the row
projection helpers already written for `api/routes_v1.py` (team/player name
resolution, provider-ID crosswalks, season memberships, canonical round/bye
projection). `operations/dashboard.py` gained two small, previously-inline
pure functions (`state_from_findings`, `dataset_presence_state`) extracted so
this module and the Operations Dashboard share one completeness vocabulary.
