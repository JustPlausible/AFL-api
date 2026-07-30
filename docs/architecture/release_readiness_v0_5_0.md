# v0.5.0 release-readiness review

**Review date:** 30 July 2026

**Reviewed revision:** `fe59f78` (`work`)

**Intended release:** first stable release of the current AFL JSON architecture

## Recommendation

**Not ready for v0.5.0.**

The JSON subsystem is well designed, modular, and strongly tested, and the
database migration path works on a clean database. The repository is close to a
release candidate. It is not yet accurate to describe the application as a
stable, consistently JSON-first architecture, however: scheduled production
collection still selects legacy HTML collectors for domains that already have
preferred JSON collectors, canonical player identity is collection-only, and
the documented first-run path cannot be followed as written.

These are integration and release-contract blockers, not reasons for an
architectural redesign.

## Must fix before release

1. **Make production source selection match the JSON-first claim, or narrow the
   release claim explicitly.** Public JSON is implemented for metadata and match
   status, authenticated CFS JSON is implemented for rosters and match player
   statistics, and the source inventory recommends those structured sources.
   Nevertheless, scheduler and Admin flows still invoke the legacy fixture,
   lineup, and HTML player-stat paths directly. Before a release advertised as
   the first stable JSON architecture, route supported domains through the JSON
   collectors with deliberate, observable fallback, or state prominently that
   v0.5.0 only introduces a *foundation* and that scheduled operations remain
   legacy. Silent dual writing or catch-all fallback is not required and should
   not be added for the release.

2. **Complete the supported canonical-identity workflow.** The player ID map and
   CFS season-player collector correctly keep AFL numeric IDs and Champion Data
   IDs separate and return player/season records plus diagnostics. No CLI path,
   persistence adapter, or player-season table consumes that result. The season
   bootstrap persists competition, season, teams, rounds, and matches only;
   `players` remains the legacy enriched-file model. Either expose and persist
   the canonical identity/season association as part of the supported bootstrap,
   or explicitly remove canonical player identity from the stable v0.5.0
   persistence contract. The former is the better match for the intended
   release.

3. **Repair the documented fresh-install and CLI contract.** The README tells a
   new operator to copy `.env.example`, but that file is absent and Compose
   requires `.env`. Its leading CLI examples also use nonexistent `--all`,
   `--scrape`, and `--enrich` options. Replace them with the implemented flags,
   add a safe example environment file (or make it genuinely optional), and add
   one short v0.5.0 runbook covering migration, canonical club seed, season
   bootstrap, service start, readiness verification, backup, and rollback.

## Architecture consistency

| Principle | Finding | Release assessment |
| --- | --- | --- |
| JSON-first data collection | Public and CFS contracts, bounded HTTP transport, raw capture, normalisers, and persistence for metadata/stats exist. Runtime scheduling remains primarily HTML-based even where JSON alternatives exist. | **Partial; blocker for an unqualified JSON-first release claim.** |
| Scraper fallback only where necessary | The source inventory makes sensible domain-specific recommendations: retain HTML for injuries and unproven enrichment; prefer JSON for metadata, status, rosters, identity, and match stats after parity. There is no shared runtime source policy or observable fallback decision. | **Policy documented, not operationally enforced.** |
| Modular collectors | `afl_json` separates endpoint contracts, client/authentication, public collectors, roster normalisation, match status, player stats, and persistence adapters. Legacy scrapers still combine browser acquisition, parsing, persistence, and audit concerns. | **Pass for the new architecture; legacy debt is acceptable if bounded.** |
| Canonical identity model | Provider IDs are opaque, independently cross-walked, validated, and never derived from AFL IDs. The collection model separates identity from season association. It is not exposed or persisted by supported workflows. | **Correct model, incomplete integration; blocker if claimed stable end to end.** |
| Canonical club bootstrap | `bootstrap/clubs.json` is versioned and validated; migration `0008` idempotently seeds all 18 clubs. Consumers now use the shared seed rather than a generated runtime file. | **Pass.** |

## CLI coverage

Implemented JSON collector coverage is uneven:

- `--collect-afl-metadata` covers read-only competition, season, team, round,
  and match collection; `--bootstrap-afl-season` persists that hierarchy.
- `--collect-match-rosters` exposes roster collection and diagnostics but has no
  canonical persistence path.
- `--collect-match-player-stats` collects and persists CFS stats and reconciles
  match status.
- There is **no CLI command for `player_id_map`, `season_players`, or
  `collect_players`**, and no command that runs the complete supported JSON
  pipeline.
- Direct public match-detail/status collection is available only indirectly
  through the player-stat reconciliation flow.
- The single `if`/`elif` dispatch means flags are silently mutually exclusive;
  argparse does not communicate that constraint with a mutually exclusive group
  or subcommands.

The missing player-identity/bootstrap path is the significant v0.5.0 gap. Roster
persistence, batch orchestration, explicit endpoint-family selection, and a
subcommand redesign can wait for v0.5.1 if their read-only status is documented.

## Database bootstrap and fresh install

The programmatic clean-install check succeeded: migrations `0001` through
`0008` applied, 18 tables were present (including SQLite's internal sequence
table), all 18 canonical clubs were seeded, `PRAGMA foreign_key_check` returned
no rows, and `PRAGMA integrity_check` returned `ok`. Application and scheduler
startup also invoke the migration runner, and migrations are transactionally
recorded.

Remaining risks:

- The human-facing Compose quick start fails before startup because `.env` is
  mandatory but the documented `.env.example` does not exist.
- Public season bootstrap is a separate manual network operation and is not
  part of Compose startup. That is a reasonable safety choice, but the runbook
  must say when to run it and how to verify the selected season/database.
- Migration `0008` reads a repository asset at migration time. Packaging must
  continue to include `bootstrap/clubs.json`; the current Docker `COPY . .`
  does.
- Multiple services can attempt first-start migrations against the same SQLite
  volume. SQLite serialization and the migration tests reduce the risk, but the
  operator runbook should recommend a one-time migration before bringing all
  services up for a release upgrade.

## Documentation consistency

- The focused metadata, roster, player-stat, database-migration, source
  inventory, and architecture documents accurately describe most current
  implementation boundaries.
- The README quick start and first CLI examples are materially stale, as noted
  above.
- The existing v0.5.0 architectural review records revision `4273e61`, 217
  tests, and database-path defects that subsequent merged work fixed. Keep it as
  a historical review, but link this document as the final readiness delta so
  readers do not mistake old findings for current state.
- CLI help accurately lists implemented flags, but it does not label JSON versus
  legacy commands, persistence versus read-only behavior consistently, or the
  effective mutual exclusivity of actions.

## High-priority v0.5.1 follow-ups

These should be tracked but need not delay v0.5.0 after the blockers above are
resolved or explicitly scoped out:

1. Persist canonical rosters/selections with publication-safe replacement and
   provenance semantics.
2. Introduce one domain source-policy/application layer shared by CLI,
   scheduler, and Admin; migrate one domain at a time.
3. Reconcile or retire the parallel legacy `player_stats` and
   `cfs_player_stats` representations.
4. Add season membership/history for teams and players instead of storing a
   single current season/club relationship.
5. Add parity/golden cases for valid-empty, unpublished, malformed, and partial
   states across remaining HTML fallbacks, especially injuries and lineups.
6. Replace flat action flags with subcommands or, minimally, enforce one action
   through argparse and provide aggregate batch failure semantics.

## Debt to document rather than delay release

- Legacy Playwright collectors remain intentionally necessary for injuries and
  some editorial/player enrichment until a maintained structured source or
  proven parity exists.
- Plain dictionaries are the canonical metadata model; typed domain records can
  wait until upstream contracts stabilize.
- Raw capture, diagnostics, retry behavior, and audit provenance are stronger in
  the JSON path than in legacy HTML paths.
- The flat repository/module layout, direct concrete imports in orchestration,
  and archived/manual scraper entry points are maintainability debt, not release
  blockers.
- CFS depends on an unofficial/undocumented upstream authentication and payload
  contract; sanitized fixtures, bounded retries, raw capture, and explicit
  unavailable states mitigate but cannot eliminate that operational risk.

## Release gate

After resolving the three blockers, rerun the full offline suite, CLI-help
contract tests, a clean-database migration/integrity check, and one controlled
current-season live smoke test for metadata bootstrap plus a representative CFS
collection. Record the chosen production source matrix and whether rosters and
player-season associations are intentionally read-only.

At that point the expected recommendation is **Ready with minor follow-up
issues**. On the reviewed revision, the recommendation remains **Not ready**.
