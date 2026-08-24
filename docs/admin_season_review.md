# Admin Season Review

Open **Data → Season Review** in the authenticated Admin interface and choose a
persisted AFL season by year/name. The selector is populated only from canonical
SQLite persistence; opening the page does not bootstrap a missing season.

Season Review is the HTML presentation of the same structured
`SeasonCompletenessReporter` used by:

```bash
python cli.py --report-afl-season 2026
```

The overall result (`complete`, `usable_with_warnings`, `incomplete`, or
`invalid`), severities, domains, stable check codes, entity identifiers, and
messages therefore retain the CLI report's semantics. Summary counts include
the report's persisted team, round, match/lifecycle, authoritative-statistic,
membership, and audit evidence. Findings are grouped by their existing domain;
the Admin presenter does not add completeness rules. See the
[CLI report guide](cli.md#read-only-season-completeness-report) for the detailed
status and authority interpretation.

This view is strictly observational. Each request opens SQLite in `mode=ro` and
enables `PRAGMA query_only`; it does not invoke an AFL or Champion Data client,
collector, bootstrap, sync, reconciliation, repair, audit writer, or any other
persistence path. Refreshing repeats only the report queries. An incomplete or
invalid result is displayed for operator inspection and is never repaired
automatically. An invalid selection returns a clear error and performs no report
or mutation.
