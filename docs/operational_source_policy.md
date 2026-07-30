# Operational AFL source policy

Issue #73 establishes `collection/source_policy.py` as the source-selection boundary shared by scheduler jobs and Admin-triggered jobs. An entry point asks for a domain and internal target; it does not import an HTML or JSON collector. The CLI remains a mixture of explicit legacy tools and structured-source tools, and its help labels that distinction.

## Source matrix

“Before” describes the entry points immediately before Issue #73. “After” is the supported operational policy. Persistence means canonical application database persistence, not optional raw-response capture.

| Domain | Architecturally preferred source | Current operational source | Persistence boundary | Intentional legacy / permitted fallback |
| --- | --- | --- | --- | --- |
| Competitions and competition seasons | Public JSON | Public JSON season refresh | Persists canonical hierarchy | Fixture HTML retained only for explicit diagnostics; not an automatic fallback |
| Clubs/teams | Public JSON for canonical metadata | Public JSON metadata; HTML for player enrichment | Public team metadata persists; CFS player listings are read-only | HTML is intentional for enrichment gaps, not a metadata fallback |
| Rounds | Public JSON | Public JSON season refresh | Persists rounds and corrections | Match/fixture HTML remains explicit diagnostic only |
| Fixtures/matches | Public JSON | Public JSON season refresh daily; public match detail during match day | Full metadata refresh persists times, venue, round relationships, new/corrected fixtures, status and scores; targeted detail reconciles lifecycle status | HTML is not an automatic fallback |
| Match status | Public match-detail JSON | Public match-detail JSON | Monotonic status persistence; complete metadata remains covered by scheduled season refresh | HTML monitor remains diagnostic only |
| Player identity / season players | Public ID map plus CFS season players | No scheduled/Admin operation | **Read-only**; persistence is Issue #75 | HTML remains intentional for enrichment/historical gaps |
| Match rosters / selections | CFS JSON | **HTML lineup collector for scheduler/Admin persistence**; CFS remains a CLI/read-only diagnostic | HTML writes `lineups`; CFS rosters are **read-only** | HTML is an intentional temporary operational source, not fallback; canonical CFS persistence is Issue #77 |
| Match player statistics | CFS JSON | CFS JSON | Persists `cfs_player_stats` and reconciles match status | Match-centre HTML remains an explicit legacy CLI diagnostic, not fallback |
| Injuries | HTML (no proven structured source) | HTML | Persists injuries | Intentional HTML; no fallback source |
| Player enrichment / listings | HTML until structured parity exists | HTML leaderboard/club tooling | Existing legacy persistence/export only | Intentional HTML |


## Fallback and failure rules

There is no automatic JSON-to-HTML fallback in the initial policy. A CFS resource that is explicitly not published is returned as `unavailable`, with `fallback_occurred=false`; it does not run HTML and does not perform a destructive write. Public/CFS authentication failures, malformed payloads, transport failures after bounded retries, programming errors and database errors fail the run. They never authorize HTML collection.

Legacy HTML collectors remain in the repository for explicit diagnostics, historical gaps and domains where HTML is authoritative. Enabling a future fallback requires a per-domain policy change, a recognised availability state, proven persistence compatibility and tests. Catch-all fallback and silent dual writes are prohibited.

## Observability

Every policy run writes the existing scrape-run audit with domain, target, trigger, success/failure and row counts. The `operational_collection` logger also emits a structured JSON diagnostic containing the requested domain and target, trigger source, source family and collector, persistence, status, fallback outcome and row counts. Failures log the same selection context without request headers, cookies or CFS tokens. Operators can inspect scheduler logs and the Admin scrape-run view to correlate the selected source with the registered job/correlation identifier.

## Explicit release boundaries

Canonical player identity and player-season persistence remain Issue #75. Canonical CFS roster persistence is not invented here: CFS roster collection remains visibly read-only, while scheduled and Admin lineup operations deliberately retain the HTML writer so lineup persistence is not lost. Broader CLI restructuring and fresh-install documentation remain Issue #76. Roster persistence/parity and any later controlled HTML fallback remain Issue #77. The separate legacy and CFS player-stat representations are not reconciled or dual-written by this policy.

## Fixture and lineup regression boundary

Before Issue #73, the scheduled lineup subprocess acquired HTML records but its module entry point did not call `save_lineups_to_db`; only the unified CLI performed that write. The shared operational path now deliberately runs the same HTML acquisition and the existing lineup writer, so scheduler and Admin jobs report success only after the records have been stored. CFS roster collection remains separately available as read-only evidence and is not represented as a persistent operation.

The old recurring match-card HTML scraper could update broad fixture fields. Public match-detail reconciliation intentionally updates lifecycle status only. It therefore does not replace broad fixture maintenance by itself: both daily fixture and daily match jobs run the public JSON season collector and persistence adapter, covering newly published and corrected fixtures, scheduled times, venues, round/team relationships, status and scores. Five-minute match-day work uses the narrower public detail/status reconciler between those complete refreshes.
