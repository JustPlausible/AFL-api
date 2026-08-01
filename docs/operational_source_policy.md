# Operational AFL source policy

Issue #73 establishes `collection/source_policy.py` as the source-selection boundary shared by scheduler jobs and Admin-triggered jobs. An entry point asks for a domain and internal target; it does not import an HTML or JSON collector. The CLI remains a mixture of explicit legacy tools and structured-source tools, and its help labels that distinction.

## Source matrix

“Before” describes the entry points immediately before Issue #73. “After” is the supported operational policy. Persistence means canonical application database persistence, not optional raw-response capture.

| Domain | Architecturally preferred source | Current operational source | Persistence boundary | Intentional legacy / permitted fallback |
| --- | --- | --- | --- | --- |
| Competitions and competition seasons | Public JSON | Public JSON season refresh | Persists canonical hierarchy | Fixture HTML retained only for explicit diagnostics; not an automatic fallback |
| Clubs/teams | Public JSON for canonical metadata | Public JSON metadata; HTML for player enrichment | Public team metadata persists; the season bootstrap persists CFS player listings into the canonical player model | HTML is intentional for enrichment gaps, not a metadata fallback |
| Rounds | Public JSON | Public JSON season refresh | Persists rounds and corrections | Match/fixture HTML remains explicit diagnostic only |
| Fixtures/matches | Public JSON | Public JSON season refresh daily; public match detail during match day | Full metadata refresh persists times, venue, round relationships, new/corrected fixtures, status and scores; targeted detail reconciles lifecycle status | HTML is not an automatic fallback |
| Match status | Public match-detail JSON | Public match-detail JSON | Monotonic status persistence; complete metadata remains covered by scheduled season refresh | HTML monitor remains diagnostic only |
| Player identity / season players | Public ID map plus CFS season players | CLI `--bootstrap-afl-season`; no scheduled/Admin player-bootstrap operation | Persists canonical players, AFL/Champion Data provider mappings, team links where resolvable, and competition-season membership transactionally | HTML remains intentional for enrichment/historical gaps |
| Match rosters / selections | CFS JSON | **HTML lineup collector for scheduler/Admin persistence**; CFS remains a CLI/read-only diagnostic | HTML writes `lineups`; CFS rosters are **read-only** | HTML is an intentional temporary operational source, not fallback; canonical CFS persistence is Issue #77 |
| Match player statistics | CFS JSON | CFS JSON for Scheduler/Admin and `--collect-match-player-stats` | Persists `cfs_player_stats` and reconciles match status; explicit legacy `--scrape-match` writes the separate `player_stats` table | Match-centre HTML remains an explicit legacy CLI operation, not fallback or dual-writing |
| Injuries | HTML (no proven structured source) | HTML for Scheduler, Admin, and CLI `--scrape-injuries` | Resolves scraped names to canonical AFL player IDs; persists resolved current/history records to `injuries` and reports unresolved or ambiguous rows without inventing identities | Intentional HTML; no fallback source |
| Player enrichment / listings | HTML until structured parity exists | HTML leaderboard/club tooling | Existing legacy persistence/export only | Intentional HTML |


## Fallback and failure rules

There is no automatic JSON-to-HTML fallback in the initial policy. A CFS resource that is explicitly not published is returned as `unavailable`, with `fallback_occurred=false`; it does not run HTML and does not perform a destructive write. Public/CFS authentication failures, malformed payloads, transport failures after bounded retries, programming errors and database errors fail the run. They never authorize HTML collection.

Legacy HTML collectors remain in the repository for explicit diagnostics, historical gaps and domains where HTML is authoritative. Enabling a future fallback requires a per-domain policy change, a recognised availability state, proven persistence compatibility and tests. Catch-all fallback and silent dual writes are prohibited.

## Observability

Every policy run writes the existing scrape-run audit with domain, target, trigger, success/failure and row counts. The `operational_collection` logger also emits a structured JSON diagnostic containing the requested domain and target, trigger source, source family and collector, persistence, status, fallback outcome and row counts. Failures log the same selection context without request headers, cookies or CFS tokens. Operators can inspect scheduler logs and the Admin scrape-run view to correlate the selected source with the registered job/correlation identifier.

## Explicit release boundaries

Canonical player identity and player-season persistence are implemented by the
CLI season bootstrap: `--bootstrap-afl-season` first persists public metadata,
then persists the collected CFS players, provider mappings, team links where
available, and season membership. This is a bootstrap operation, not a scheduled
or Admin player refresh, and it does not imply that CFS match-roster persistence
exists. CFS roster collection remains visibly read-only, while scheduled and
Admin lineup operations deliberately retain the HTML writer so lineup
persistence is not lost. The separate legacy `player_stats` and operational
`cfs_player_stats` representations are not reconciled or dual-written by this
policy.

## Fixture and lineup regression boundary

Before Issue #73, the scheduled lineup subprocess acquired HTML records but its module entry point did not call `save_lineups_to_db`; only the unified CLI performed that write. The shared operational path now deliberately runs the same HTML acquisition and the existing lineup writer, so scheduler and Admin jobs report success only after the records have been stored. CFS roster collection remains separately available as read-only evidence and is not represented as a persistent operation.

The old recurring match-card HTML scraper could update broad fixture fields. Public match-detail reconciliation intentionally updates lifecycle status only. It therefore does not replace broad fixture maintenance by itself: both daily fixture and daily match jobs run the public JSON season collector and persistence adapter, covering newly published and corrected fixtures, scheduled times, venues, round/team relationships, status and scores. Five-minute match-day work uses the narrower public detail/status reconciler between those complete refreshes.
