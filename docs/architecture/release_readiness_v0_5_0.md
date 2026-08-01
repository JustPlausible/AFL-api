# v0.5.0 final release-readiness review

## 1. Review metadata

| Item | Final review value |
| --- | --- |
| Review date | 31 July 2026 (UTC) |
| Prior review | 30 July 2026, revision `fe59f78`, verdict **Not ready for v0.5.0** |
| Reviewed source revision | `12a730a5e19200161c95eb3b1abd134d5f298be0` |
| Source branch | `work` (the supplied checkout's current/default branch before this documentation branch was created) |
| Review branch | `docs/v0.5.0-final-readiness-review` |
| Python | CPython 3.14.4; pip 26.1 |
| Relevant installed packages | pytest 9.0.3, APScheduler 3.11.3, requests 2.34.2, httpx 0.28.1, Playwright 1.61.0 |
| Declared runtime detail | Playwright is pinned to 1.61.0 in both `requirements.txt` and the Docker base-image argument; most other runtime dependencies are unpinned |
| Full automated suite | **338 passed in 13.62 seconds** |
| Migration range | `0001_v0_3_0_schema` through `0011_partial_scrape_runs` (11 recorded migrations) |
| GitHub state checked | Five open issues (#50, #60, #61, #77, #78); **zero open pull requests** |

The exact revision above is the implementation reviewed. The commit has a 1
August 2026 author/commit date in its original `+0800` timezone even though this
review environment's UTC date was still 31 July; this is not a revision
ambiguity.

## 2. Executive recommendation

**Not ready for v0.5.0.**

The implementation blockers from the original review are resolved: operational
Scheduler/Admin routing is policy-driven, the supported season bootstrap
persists canonical players and memberships, and injury persistence now consumes
that canonical model safely. The full offline suite is green, a clean database
passes migration, foreign-key, integrity and idempotency checks, and a bounded
live public-metadata read succeeded.

At the time of this review, the remaining hold was substantially smaller and
documentation/release-process only. Release-facing documentation contained
obsolete canonical-persistence and operational player-stat source descriptions,
and there was no single complete
operator release/runbook covering bootstrap verification, backup **and restore
rollback**. No repository version declaration or release notes/changelog for
v0.5.0 were found. The smallest path to release is to correct those documents,
declare the version/release notes, run the manual CFS/injury/Docker checks listed
below, and then tag. No application redesign is required.

## 3. Changes since the 30 July review

| Change | Verified effect |
| --- | --- |
| Operational source policy (#73/#85) | `collection/source_policy.py` owns source selection; recurring Scheduler and Admin manual handlers dispatch through it. Selected source, collector, persistence, result and fallback fields are observable. |
| Canonical player persistence (#75/#86) | Migration `0009` adds canonical player/provider/membership tables, and `--bootstrap-afl-season` atomically persists metadata followed by CFS players and season membership. |
| CLI/documentation correction (#76/#87) | `.env.example` exists; README invalid flags were replaced; `docs/cli.md` describes every parser action, identifiers, source and write/read-only behavior; documented examples have contract tests. |
| Injury club identifiers (#88/#89) | Canonical seed aliases cover current editorial image tokens, and migration `0010` propagates the seed refresh to existing databases. |
| Canonical injuries and partial outcomes (#90/#91) | Injuries resolve from canonical current/latest season membership and AFL provider IDs, skip rather than guess unresolved identities, persist safe rows, and report partial counts. Migration `0011` adds the audit `partial` status. |
| Regression growth | The suite increased from the 30 July review and now contains 338 passing tests, including deterministic canonical-injury and partial-audit cases. |

## 4. Disposition of the original three blockers

| Original finding | Implementation now present | Evidence and validation | Status |
| --- | --- | --- | --- |
| Scheduler/Admin production source selection contradicted the JSON-first claim. | Shared `SOURCE_POLICY` selects public JSON for metadata/status, CFS JSON for match statistics, deliberate HTML for injuries and persistent lineups, and no implicit fallback. Scheduler jobs and all Admin manual collectors call `collect_operational`. | Inspected policy, scheduled tasks, match-day scheduling and manual trigger handlers; ran policy, scheduler and Admin tests. Outcomes and Admin enqueue responses expose selected source, persistence and fallback fields. | **Resolved.** |
| Canonical player identity was collection-only and absent from a supported persisted workflow. | Migration `0009`, `persist_player_seasons`, and `--bootstrap-afl-season` persist canonical identity, separate provider mappings, team-season links and competition-season membership in one transaction. | Inspected schema, bootstrap dispatch and persistence adapter; ran player persistence/bootstrap tests and a fresh migration. The attempted live bootstrap correctly rolled back all metadata when the CFS phase was blocked by the environment proxy. | **Resolved.** |
| Fresh-install/CLI documentation was inaccurate or incomplete. | `.env.example`, corrected README commands, grouped CLI help, `docs/cli.md`, and migration/deployment-order documentation now exist. One-operation-at-a-time behavior is explicitly documented. | Compared generated help with README and CLI guide; documentation contract tests pass; clean install and `.env`/Compose references were inspected. However, no complete release/rollback runbook exists and some source-policy/inventory prose is stale. | **Partially resolved; still blocking as a narrow documentation defect.** |

Historical findings above are deliberately phrased in the past tense. The
current verdict is based on the final column, not on the prior review's verdict.

## 5. Architecture consistency

| Principle | Current assessment |
| --- | --- |
| JSON first, domain aware | **Pass.** Public JSON is operational for metadata/status and CFS for match stats. JSON is not falsely treated as universally available. |
| HTML only where deliberate | **Pass with documented debt.** Injuries have no maintained structured source. Persistent lineups intentionally remain HTML because CFS rosters are read-only and lack publication-safe canonical persistence. Club squad/enrichment commands are explicit legacy/manual tools. |
| One operational application boundary | **Pass for Scheduler/Admin.** Both use `collect_operational`; selection does not depend on which of those entry points initiated the run. CLI exposes equivalent preferred collectors plus explicitly named legacy tools rather than silently routing. |
| Persistence compatibility | **Pass within the declared matrix.** A selected operation performs only its documented write: metadata hierarchy, CFS stats, legacy lineups, or canonical injuries. There is no fallback or dual-write that changes tables silently. |
| Canonical identities | **Pass.** Club seed, team metadata, namespaced player provider IDs and player-season-team associations are separate concepts with foreign keys and contradiction checks. |
| Failure/audit semantics | **Pass.** Unavailable CFS resources remain unavailable without HTML fallback; errors fail visibly; injury partial success has a first-class audit state. |

Direct legacy paths remain in `cli.py` and executable scraper modules. The
explicit CLI actions (`--scrape-match`, lineup, round, injury, club and
enrichment tools) are supported exceptions/manual tools and clearly identify
their HTML or legacy table behavior. Direct module `__main__` blocks are
internal diagnostics. The recurring fixture/match/stat and Admin paths were
checked for policy routing; no stale production Scheduler/Admin path that
silently selects the old stat collector was found.

## 6. Source-policy and fallback assessment

| Domain | Operational selection | Persistence and fallback conclusion |
| --- | --- | --- |
| Metadata/fixtures | Public AFL JSON | Persists full canonical metadata hierarchy; no HTML fallback. Both daily fixture and match refresh use this path. |
| Match status | Public AFL JSON detail | Monotonic canonical status reconciliation; no HTML fallback. |
| Match statistics | Champion Data/CFS JSON | Persists `cfs_player_stats`; explicit `--scrape-match` is a separate manual HTML writer to `player_stats`, not fallback. |
| Rosters | Champion Data/CFS JSON | Read-only collector. Unpublished is observable as unavailable. |
| Operational lineups | Rendered HTML | Supported temporary writer to legacy lineup tables; deliberate selection because canonical CFS roster persistence is not implemented. |
| Injuries | Rendered HTML | Supported source and canonical-identity writer; no maintained JSON source exists. |
| Club player scraping/enrichment | HTML/files | Explicit legacy/manual workflow, not a Scheduler/Admin metadata substitute. |

Every `SourcePolicy` currently has `fallback_permitted=False`. Successful
outcomes and logs include `source_family`, `collector`, persistence performed,
status, row counts, `fallback_occurred` and reason; failures log the selection
context. This is appropriately more honest than catch-all HTML fallback.

The CLI matrix matches the implementation. Its public metadata collection is
read-only, season bootstrap is persistent, rosters are read-only, CFS player
stats are persistent, and legacy HTML flags state their target tables. The flat
CLI dispatches only the first action flag supplied. `docs/cli.md` says to invoke
one operation at a time, so this is accurate, although enforcing mutual
exclusion remains a good usability follow-up.

## 7. Canonical identity and persistence assessment

### Supported bootstrap

`--bootstrap-afl-season SEASON` collects/persists public competition, season,
team, team-season, round and match metadata, then collects and persists the CFS
season population. It creates/updates:

* `canonical_players` for person identity;
* `player_provider_ids` with independent `afl` numeric and `champion_data`
  opaque identifiers (neither is derived from the other);
* `competition_season_players` for season membership, club/team, jumper,
  position, photo and source evidence; and
* `afl_team_seasons` to support the composite season/team relationship.

Unavailable/empty player results do not erase existing membership. Contradictory
provider mappings raise rather than reassign identities. Roster selections
remain intentionally read-only and are not represented as persisted season
membership.

### Issue #90 injury implementation

The injury parser builds one `CanonicalInjuryPlayerResolver` snapshot per
scrape. It joins canonical players to competition-season membership, canonical
teams and only the `afl` provider namespace, selects the current/latest season
per canonical club, and then performs deterministic normalised exact-name
matching within that club. Supported nickname and suffix normalization is
bounded. Unknown clubs, missing matches, multiple matches, absent AFL IDs and
non-numeric AFL IDs are explicit unresolved/ambiguous results; none is guessed.

The injury path no longer calls the legacy `players` lookup. Since
`injuries.afl_id` is non-nullable, unresolved rows are skipped with structured
diagnostics while resolved rows persist. A partial run does not expire prior
current records. Summary and audit fields are `rows_parsed`, `rows_resolved`,
`rows_persisted`, `rows_unresolved`, and `rows_ambiguous`.

The validation result supplied with the release work is recorded as operational
evidence, **not as a live test repeated by this review**:

```text
rows_parsed: 155
rows_resolved: 155
rows_persisted: 155
rows_unresolved: 0
rows_ambiguous: 0
status: success
```

The supplied notes also report that constructing one indexed resolver per
scrape reduced the live injury command from approximately 98 seconds to
approximately 2 seconds. The code change and regression coverage were verified,
but those timings were not reproduced under benchmark controls and should not
be presented as a formal performance benchmark.

## 8. CLI and operator workflow assessment

Generated `python cli.py --help` was compared with README, `docs/cli.md`, focused
collector docs, Scheduler/Admin capabilities and the called persistence
functions.

* Every release-facing documented `cli.py` action exists; automated examples
  are parser-validated.
* Read-only metadata and roster collection are distinguished from persistent
  bootstrap, CFS stats, injuries, lineups and legacy operations.
* Public JSON, CFS JSON and rendered HTML behaviors are named accurately in CLI
  help and the operator guide.
* `.env.example` supplies valid development defaults and all three Compose
  services reference the copied `.env`.
* A first run can copy `.env`, build/start Compose, migrate automatically at
  startup or explicitly with `python -m db.migrate`, and run the persistent
  season bootstrap. The pieces exist, but they are split across README,
  `docs/cli.md` and `docs/database_migrations.md` rather than one checked
  release sequence.
* `/healthz` and `/readyz` exist and Compose healthchecks call `/readyz` for API
  and Scheduler.
* Database migration and pre-upgrade backup cautions are documented. A concrete
  rollback/restore sequence, post-bootstrap verification queries, tag/release
  sequence and post-release smoke procedure are not.
* Single-action behavior is documented but not rejected by argparse; this is a
  v0.5.1 usability improvement, not an application release blocker by itself.

The documentation inconsistency identified during this review was corrected by
Issue #93: the operational policy and scraper inventory now describe canonical
player bootstrap persistence and Scheduler/Admin CFS player-stat collection.

## 9. Clean-install and migration validation

A disposable database under `/tmp` was used; no production data was opened.

| Check | Result |
| --- | --- |
| Ordered application | `0001` through `0011` all applied successfully. |
| Migration records | 11 rows, IDs complete and ordered. |
| Canonical club seed | 18 rows in `clubs`; `afl_teams` correctly remains empty until a season bootstrap. |
| Required structures | `canonical_players`, `player_provider_ids`, `competition_season_players` and `scrape_runs` all exist; 22 SQLite tables including internal tables. |
| Foreign keys | `PRAGMA foreign_key_check` returned `[]`. |
| Integrity | `PRAGMA integrity_check` returned `ok`. |
| Idempotency | Second `python -m db.migrate` reported “already up to date.” |
| Partial scrape audit | Migration `0011` rebuilds `scrape_runs` with allowed status `partial`, copies existing records and recreates audit indexes. Focused audit tests pass. |
| Docker packaging | `Dockerfile` uses `COPY . .`, so migrations, canonical club seed and bootstrap code are in build context unless excluded; `.dockerignore` does not exclude them. A real build could not be executed because Docker is absent in this environment. |

## 10. Automated and manual test evidence

### Commands run

| Command | Outcome |
| --- | --- |
| `pytest -q` | **Pass:** 338 passed in 13.62s. |
| `python -m compileall -q afl_json collection db merge scraper cli.py tests` | **Pass:** no output/errors. |
| `python cli.py --help` | **Pass:** complete grouped action help printed. |
| `pytest -q tests/test_migration_runner.py tests/test_operational_source_policy.py tests/test_cli_player_season_bootstrap.py tests/test_afl_player_persistence.py tests/test_canonical_injuries.py tests/test_afl_golden_fixtures.py tests/test_cli_help.py tests/test_scrape_runs.py` | **Pass:** 74 passed in 5.51s. |
| `pytest -q tests/test_health.py tests/test_scheduler_startup.py tests/test_scheduler_registry.py tests/test_admin_manual_triggers.py` | **Pass:** 21 passed in 4.57s. |
| Disposable `DB_PATH`; `python -m db.migrate` twice; SQLite count/FK/integrity queries | **Pass:** all 11 migrations, 18 clubs, required tables, no FK violations, integrity `ok`, idempotent second run. |
| `timeout 90 python cli.py --collect-afl-metadata --afl-season 2026` | **Pass, live read-only:** competition and 2026 season resolved with 30 rounds, 18 teams and 218 matches. No database write was requested. |
| Disposable DB plus `timeout 120 python cli.py --bootstrap-afl-season 2026` | **Environment-limited:** public phase was reachable, but the CFS WMCTok request was rejected by the environment proxy (`403 Forbidden`). The command failed and its encompassing transaction left all metadata/player tables empty, a useful atomicity observation but not a successful live bootstrap. |
| `docker build -t afl-api:v0.5.0-readiness .` | **Not run:** `docker` is not installed in the review environment. |

One initially attempted focused command named a nonexistent
`tests/test_cli_contract.py`; pytest correctly reported no tests. It was a review
command typo, not a product failure, and was replaced by the real
`tests/test_cli_help.py` plus the passing focused suite above.

### Manual/live coverage still required before tagging

Against a disposable or explicitly backed-up non-production database in the
release deployment environment:

1. run the full 2026 metadata/player bootstrap with CFS token access and verify
   counts plus provider namespace separation;
2. run one published CFS roster (read-only) and one CFS match-stat persistence
   operation;
3. run the current injury HTML collection and confirm the structured summary,
   ideally reconfirming the supplied 155/155 result;
4. exercise one Scheduler or Admin manual stats job and inspect selected-source,
   audit and persisted-row diagnostics;
5. start the built release image and verify API and Scheduler `/healthz` and
   `/readyz` against the intended volume; and
6. rehearse backup restoration and the documented rollback commands.

No claim is made that this review performed live CFS, live injury, browser,
Docker, or deployed endpoint validation.

## 11. GitHub release state and remaining risks

The GitHub REST API was queried on the review date. There were no open pull
requests and these five open issues:

| Item | Labels | Classification | Reason |
| --- | --- | --- | --- |
| #78 Upstream API instability | documentation, roadmap, scraper, priority: medium | **Recommended before release only as a smoke gate; otherwise v0.5.1 follow-up.** | Fixtures/raw capture mitigate it; the missing live CFS smoke must be completed before tag, but documenting an undocumented upstream cannot eliminate the risk. |
| #77 Dual player-stat models | documentation, roadmap, priority: medium | **Acceptable v0.5.1 follow-up.** | Policy and CLI now state which explicit operation writes `player_stats` versus `cfs_player_stats`; no silent dual write was found. Some stale source-inventory prose should be fixed now. |
| #61 Collector orchestration/dry-run CLI | enhancement, roadmap, scraper, priority: medium | **Acceptable v0.5.1 follow-up.** | Useful batch UX, not necessary for accurate single-action release workflows. |
| #60 JSON payload fixtures/contract regressions | enhancement, roadmap, scraper, priority: medium | **Acceptable v0.5.1 follow-up.** | Strong fixture coverage exists, but not every proposed state is complete. Live smoke remains the release mitigation. |
| #50 Separate fetch/parse/persistence pilot | roadmap, scraper, priority: medium, refactor | **Unrelated/post-release architectural backlog.** | Broad legacy refactoring is not needed to release the verified policy boundary. |

No open item carried `priority: high`, `bug` or `release risk` at the time of the
query. Open status alone is not treated as blocking. The actual release blocker
found by this review is the narrow stale/incomplete release documentation; a
small documentation issue/PR should update the source policy and inventory and
add one v0.5.0 release/rollback runbook.

Other residual operational risks are bounded but real: CFS is undocumented,
HTML injuries/lineups can change markup, most dependencies are unpinned, and
the example Compose setup is development-oriented (`--reload` and source bind
mounts). These are not reasons for a broad pre-release redesign, but the release
image and production Compose must be verified rather than inferred from the
example.

## 12. v0.5.1 follow-ups

1. Reconcile or retire parallel `player_stats` and `cfs_player_stats` models.
2. Define publication-safe canonical roster/selection persistence; then move
   operational lineups from deliberate HTML to CFS.
3. Enforce CLI action mutual exclusion or introduce grouped subcommands and
   batch/dry-run orchestration.
4. Complete remaining public/CFS contract states and scheduled live contract
   checks without storing credentials.
5. Refactor one still-supported HTML collector into separate acquisition,
   parser, persistence and audit layers.
6. Adopt a dependency update/pinning policy and a production Compose example.

## 13. Final release checklist

The following is the actionable gate. Items marked **required before tag** are
not evidence already completed by this review.

- [ ] **Merge state:** merge this readiness update and the narrow source-policy,
  inventory and release-runbook corrections; confirm zero unintended application
  changes and no other pending release PRs.
- [ ] **Green CI:** require the default-branch CI for the exact release SHA,
  including all 338+ tests and compile checks.
- [ ] **Version declaration:** add/verify the repository's authoritative
  `0.5.0` version declaration; none was found in the reviewed implementation.
- [ ] **Changelog/release notes:** publish concise v0.5.0 notes covering source
  policy, canonical bootstrap/injuries, migrations `0009`–`0011`, known HTML
  boundaries and the two stat tables.
- [ ] **Clean database:** repeat migrations and the full live 2026 bootstrap in
  the deployment environment; verify 18 clubs, player/provider/membership
  counts, foreign keys and integrity.
- [ ] **Database backup:** stop writers/checkpoint SQLite safely, back up the
  database (including WAL state as appropriate), record its path/checksum and
  perform a restore verification before migration.
- [ ] **Docker verification:** build the exact release SHA, confirm Playwright
  1.61.0/browser compatibility and packaged migrations/seed, then start all
  intended services without development source mounts/reload.
- [ ] **Operational smoke:** complete bounded CFS stats/roster, canonical injury,
  Scheduler/Admin routing and API/Scheduler readiness checks on disposable or
  backed-up data.
- [ ] **Tag:** create an annotated `v0.5.0` tag only after all preceding gates
  pass, and push it from the verified default-branch SHA.
- [ ] **GitHub release:** create the release from that tag with release notes,
  migration/backup instructions, known limitations and image digest if used.
- [ ] **Post-release smoke:** verify `/healthz`, `/readyz`, API authentication,
  Scheduler registry, selected-source logs and one safe collection in the
  deployed environment.
- [ ] **Rollback:** document and rehearse stopping all writers, restoring the
  verified pre-upgrade SQLite backup/WAL-consistent snapshot, deploying the
  previous image/SHA, starting services, and rechecking health/readiness. Do not
  attempt to downgrade schema by editing `schema_migrations`.

## 14. Final verdict

**Not ready for v0.5.0.**

The smallest required work is: (1) correct stale operational-source and
canonical-persistence documentation; (2) add one complete v0.5.0 operator
release/backup/restore-rollback runbook plus version/release notes; and (3) run
the environment-dependent CFS, injury and Docker smoke gates. Once those
specific items pass, the verified implementation supports a **Ready for
release** reassessment without further architecture or application changes.
