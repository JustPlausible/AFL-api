# Diagnostic evidence-capture framework

## Purpose

Upstream AFL/CFS payload behaviour is sometimes uncertain -- a field's real
shape, an enum's real values, or how a status transitions around a specific
moment (quarter time, a lineup change, a new season's opening round) -- and
the only reliable way to resolve that uncertainty is to poll the live
endpoint and retain evidence for manual inspection. The diagnostics
framework exists to make that kind of investigation cheap and safe to run
repeatedly, without inventing new scheduler, persistence, and reporting
machinery every time.

It grew out of Issue #148's `match_clock` investigation into
`score.matchClock.periods` / `periodCompleted` / `periodSeconds` and
`match.status` / `score.status` behaviour around quarter/half/three-quarter/
full time (see PR #175). `match_clock` is now the framework's first checked-in
**diagnostic profile**, and the reference implementation for anyone adding a
new one.

## Diagnostics vs. production collectors

This is the one rule the whole framework exists to enforce:

**Diagnostic evidence must never silently become production source
authority.** A diagnostic profile:

* is explicitly opt-in (`AFL_DIAGNOSTICS_ENABLED=true` and the profile named
  in `AFL_DIAGNOSTIC_PROFILES`), off by default;
* polls a candidate list computed independently of production scheduling
  state, and never writes to production tables (`matches`, `players`, stat
  tables, etc.) -- only to its own diagnostic table(s);
* is never read by scheduler planning, lease/finality machinery, or any
  consumer-facing API response;
* uses endpoint definitions that are kept out of the maintained
  `afl_json.contracts.ENDPOINTS` registry until (and unless) a separate,
  deliberate decision promotes a verified field into a production
  collector. A diagnostic profile observing a field is evidence for that
  future decision, not the decision itself.

If a diagnostic investigation confirms a field is reliable enough to use in
production, that is a *new, separate* piece of work: add or extend a
verified `EndpointDefinition` in `afl_json.contracts`, wire it into a real
collector, and only then consider retiring the diagnostic profile that
produced the supporting evidence.

## Architecture

```
diagnostics/
    framework.py            generic: profile contract, registry, enablement,
                             APScheduler registration, restart-safe re-registration,
                             shutdown
    profiles/
        __init__.py          imports + registers every checked-in profile
        match_clock.py        Issue #148 profile (thin adapter)

collection/match_state_evidence.py   match_clock: endpoint def, parsing, transition
                                      detection, persistence (unchanged from PR #175)
scheduler/match_state_capture.py     match_clock: candidate selection, settings,
                                      poll-cycle orchestration (unchanged from PR #175)
```

The framework owns everything that is common to *any* diagnostic
investigation:

* **profile registration** -- `diagnostics.framework.register_profile`, called
  once per checked-in profile from `diagnostics/profiles/__init__.py`;
* **configuration/enablement** -- one global switch
  (`AFL_DIAGNOSTICS_ENABLED`) and one profile-selection list
  (`AFL_DIAGNOSTIC_PROFILES`), checked by `is_profile_selected()`;
* **APScheduler registration** -- one interval job per *enabled* profile
  (`diagnostic_<profile_name>`), never a shared master tick, registered
  through the same `add_registered_job` wrapper as every other scheduler
  job so it gets the same persisted registry row, status tracking, and
  logging as production jobs;
* **execution isolation** -- each profile is its own APScheduler job, so one
  profile's uncaught exception cannot prevent another profile's job from
  running; `register_diagnostic_profiles` also isolates *registration*
  failures per profile the same way;
* **restart-safe operation** -- `register_diagnostic_profiles(scheduler)` is
  called unconditionally on every scheduler startup (`scheduler/start.py`),
  the same way the other interval jobs are; a profile's *evidence* is
  recovered from its own durable table, not from in-memory scheduler state
  (see match_clock's `poll_sequence` continuity across restarts);
* **shutdown** -- `shutdown_diagnostic_profiles()` calls each profile's
  `shutdown()` (default a no-op) so pooled resources like an HTTP client
  close cleanly.

A profile owns everything that is specific to its investigation:

* the endpoint(s) it polls and their (typically unverified)
  `EndpointDefinition`;
* candidate/entity selection (which matches/entities to poll, and when);
* payload parsing and transition/change detection;
* its default and configurable polling interval;
* its persistence and raw-response retention policy;
* how its own status/report should be interpreted.

## The profile contract

`diagnostics.framework.DiagnosticProfile` is deliberately small:

```python
class DiagnosticProfile(ABC):
    name: str

    @abstractmethod
    def interval_seconds(self) -> int: ...

    @abstractmethod
    def run(self, *, now: datetime) -> list[dict[str, Any]]: ...

    def status(self) -> dict[str, Any]: ...   # optional override
    def shutdown(self) -> None: ...            # optional override
```

`run()` must not raise for one candidate's failure -- catch and log
per-candidate the same way `match_clock`'s `capture_live_match_state` does,
so one bad match/entity never aborts the rest of that profile's poll. The
framework separately guarantees that one *profile* failing never affects
another, because each is its own scheduler job.

This is intentionally not a generic plugin system: profiles are checked-in
Python classes imported explicitly from `diagnostics/profiles/__init__.py`,
never discovered from configuration, a directory scan, or a URL.

## Configuration

```env
AFL_DIAGNOSTICS_ENABLED=true
AFL_DIAGNOSTIC_PROFILES=match_clock
```

`AFL_DIAGNOSTIC_PROFILES` is a comma-separated list of *already checked-in*
profile names -- it selects among code that exists in the repository, it is
never a mechanism for supplying arbitrary URLs, JSON paths, or code through
`.env`. Enabling a profile requires both the global switch and the profile
being named; either alone is a no-op.

Profile-specific *safe* settings (an interval, a tolerance window) remain
individually configurable, following the profile's own name:

```env
AFL_DIAGNOSTIC_MATCH_CLOCK_INTERVAL_SECONDS=15
AFL_DIAGNOSTIC_MATCH_CLOCK_KICKOFF_TOLERANCE_SECONDS=600
AFL_DIAGNOSTIC_MATCH_CLOCK_POST_LIVE_GRACE_SECONDS=600
```

## Operator endpoints

* `GET /scheduler/diagnostics` -- generic, framework-level status: whether
  diagnostics are globally enabled, and each checked-in profile's
  selected/enabled state and interval.
* `GET /scheduler/match-state-evidence` -- `match_clock`-specific evidence
  and settings (unchanged from PR #175); a future profile that needs its own
  detailed reporting adds its own endpoint or report script the same way
  (see `scripts/report_match_state_evidence.py`).

## Persistence: why `match_state_evidence_observations` stays as-is

This version of the framework deliberately **keeps `match_clock`'s existing
`match_state_evidence_observations` table** rather than introducing a
generic `diagnostic_capture_sessions` / `diagnostic_observations` /
`diagnostic_events` schema. Reasoning:

* There is exactly one profile today. A generic schema's payoff is
  amortising persistence work across *multiple* profiles; with one profile,
  a generic schema is pure abstraction cost with no concrete benefit yet.
* PR #175 is the stable implementation used for live Issue #148 testing.
  Migrating its table now would put already-captured live evidence and an
  active investigation at risk for no behavioural gain.
* `match_clock`'s table shape (typed columns for `periods_json`,
  `latest_period_number`, `latest_period_seconds`, `latest_period_completed`,
  `transition_flags_json`, selective `raw_match_item_json`) is specific
  enough to its own investigation that a one-size-fits-all generic
  `observations` table would likely need a similar amount of profile-specific
  structure anyway (a JSON blob per profile, or per-profile columns), without
  clearly saving implementation effort.

No migration/backfill was needed for this change: migration `0016` and the
`match_state_evidence_observations` table are untouched. All previously
captured Issue #148 evidence remains queryable exactly as before, through
`collection.match_state_evidence.evidence_rows` and
`scripts/report_match_state_evidence.py`, unchanged.

**When to revisit this:** once a second profile needs its own durable
per-poll table, look at what actually duplicates between the two before
generalising. A reasonable trigger for a shared schema is three or more
profiles with materially the same shape (poll sequence, observed-at,
per-poll fields, transition flags, selective raw retention) -- at that point
extracting a shared `diagnostic_observations`-style table (with each
profile owning its own typed "extra fields" column, or a profile-specific
companion table for anything that needs real columns) becomes a genuine
simplification rather than premature architecture. Any such migration should
be additive (new table, backfill by copying/mapping existing rows, dual-read
period, then retire the old table) so an in-progress investigation is never
interrupted.

## Adding a new diagnostic profile

Using a hypothetical `match_roster_contract` investigation as an example:

1. **Write the investigation-specific logic**, following `match_clock`'s
   split: pure parsing/transition-detection + persistence in one module
   (e.g. `collection/match_roster_contract_evidence.py`), candidate selection
   and poll-cycle orchestration in another (e.g.
   `scheduler/match_roster_contract_capture.py`). Define its endpoint as an
   unverified, diagnostic-local `EndpointDefinition` -- do not add it to
   `afl_json.contracts.ENDPOINTS`.
2. **Add persistence** -- either its own small migration + table (following
   `0016_match_state_evidence.py`'s pattern) if it needs typed, queryable
   columns, or reuse existing tooling if genuinely applicable. Decide raw
   retention policy deliberately (e.g. only on first observation and on
   detected changes, as `match_clock` does).
3. **Add config** for its safe settings, e.g.
   `AFL_DIAGNOSTIC_MATCH_ROSTER_CONTRACT_INTERVAL_SECONDS`.
4. **Implement the profile adapter** in
   `diagnostics/profiles/match_roster_contract.py`:

   ```python
   class MatchRosterContractProfile(DiagnosticProfile):
       name = "match_roster_contract"

       def interval_seconds(self) -> int:
           return config.AFL_DIAGNOSTIC_MATCH_ROSTER_CONTRACT_INTERVAL_SECONDS

       def run(self, *, now: datetime) -> list[dict[str, Any]]:
           return capture_match_roster_contract(clock=lambda: now)
   ```

5. **Register it**: add one import + `register_profile(...)` line to
   `diagnostics/profiles/__init__.py`. No scheduler, persistence-lifecycle,
   or restart code is needed -- the framework already provides all of that.
6. **Enable it for a round**: set `AFL_DIAGNOSTICS_ENABLED=true` and
   `AFL_DIAGNOSTIC_PROFILES=match_roster_contract` (or
   `match_clock,match_roster_contract` to run both), restart the scheduler,
   and disable it again afterwards by removing it from
   `AFL_DIAGNOSTIC_PROFILES` -- production collection behaviour never
   changes based on this switch.
7. **Add tests** mirroring `tests/test_match_state_evidence.py` and
   `tests/test_match_state_capture.py`, plus registering the new profile in
   any framework-level compatibility tests.

This is the whole workflow a 2027-season contract check (`cfs_contract_2027`
or similar) would follow: add a checked-in profile, enable it for the
opening round, capture selected endpoint responses, compare observed
structure against known expectations by hand (or with a future schema-diff
tool -- not built here), and disable it again once the investigation is
done.
